"""Foundry simulado, para desenvolvimento local e testes (não use em produção).

Imita, num único host, as partes do Azure que o gateway usa:

- Entra ID: `POST /<tenant>/oauth2/v2.0/token` (client credentials);
- inferência v1: `POST /openai/v1/chat/completions`, `/embeddings`, `/responses`,
  `GET /openai/v1/models` (com `api-key` ou `Authorization: Bearer`);
- formato clássico: `POST /openai/deployments/<nome>/chat/completions?api-version=...`;
- descoberta: `GET /api/projects/<projeto>/deployments?api-version=v1` e
  `GET /subscriptions/.../accounts/<conta>/deployments` (Azure Resource Manager).

As respostas são determinísticas: o modelo devolve um eco da última mensagem.
Gatilhos no texto da última mensagem para exercitar o gateway:
`#429` (sempre 429), `#429-uma-vez` (429 na 1ª tentativa), `#500`, `#filtro`
(erro de filtro de conteúdo), `#cache-prompt` (metade do prompt vem do cache do Azure),
`#corta` (o stream cai no meio).

Variáveis: FAKE_API_KEY, FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_DEPLOYMENTS
(`nome:modelo:tipo,...`), FAKE_STREAM_DELAY_MS, FAKE_PORT e FAKE_USAGE_SCALE
(multiplica os tokens informados, para o custo da demonstração ter ordem de grandeza
realista; o texto devolvido continua curto).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections import Counter
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

DEFAULT_DEPLOYMENTS = (
    "gpt-4o-mini:gpt-4o-mini:chat,"
    "gpt-4.1:gpt-4.1:chat,"
    "o4-mini:o4-mini:chat,"
    "text-embedding-3-small:text-embedding-3-small:embeddings,"
    "dall-e-3:dall-e-3:image"
)


def _deployments(raw: str) -> dict[str, dict[str, str]]:
    out = {}
    for item in raw.split(","):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) == 3 and parts[0]:
            out[parts[0]] = {"model": parts[1], "kind": parts[2]}
    return out


def _tokens(text: str) -> int:
    return max(1, len(text.split()))


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "type": "invalid_request_error"}},
        status_code=status,
        headers=headers,
    )


def create_app(
    *,
    api_key: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    deployments: str | None = None,
    stream_delay_ms: int | None = None,
    usage_scale: int | None = None,
) -> FastAPI:
    app = FastAPI(title="Foundry simulado")
    cfg = {
        "api_key": api_key or os.environ.get("FAKE_API_KEY", "chave-falsa"),
        "client_id": client_id or os.environ.get("FAKE_CLIENT_ID", "cliente-falso"),
        "client_secret": client_secret or os.environ.get("FAKE_CLIENT_SECRET", "segredo-falso"),
        "deployments": _deployments(
            deployments or os.environ.get("FAKE_DEPLOYMENTS", DEFAULT_DEPLOYMENTS)
        ),
        "scale": max(1, usage_scale or int(os.environ.get("FAKE_USAGE_SCALE", "1"))),
        "delay": (
            stream_delay_ms
            if stream_delay_ms is not None
            else int(os.environ.get("FAKE_STREAM_DELAY_MS", "25"))
        )
        / 1000,
    }
    app.state.cfg = cfg
    app.state.calls = Counter()  # chamadas por (rota, deployment)
    app.state.attempts = Counter()  # tentativas por hash de mensagem (#429-uma-vez)
    app.state.requests: list[dict[str, Any]] = []
    tokens: dict[str, str] = {}

    def authorized(request: Request) -> bool:
        if request.headers.get("api-key") == cfg["api_key"]:
            return True
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            value = auth[7:].strip()
            return value == cfg["api_key"] or value in tokens
        return False

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    # ------------------------------------------------------------------ Entra ID
    @app.post("/{tenant}/oauth2/v2.0/token")
    async def token(tenant: str, request: Request):
        from urllib.parse import parse_qs

        raw = parse_qs((await request.body()).decode())
        form = {k: v[0] for k, v in raw.items()}
        if (
            form.get("client_id") != cfg["client_id"]
            or form.get("client_secret") != cfg["client_secret"]
        ):
            return JSONResponse(
                {
                    "error": "invalid_client",
                    "error_description": "AADSTS7000215: Invalid client secret provided.",
                },
                status_code=401,
            )
        value = f"token-{uuid.uuid4().hex}"
        tokens[value] = str(form.get("scope"))
        return {"token_type": "Bearer", "expires_in": 3599, "access_token": value}

    # ------------------------------------------------------------------ descoberta
    def project_items() -> list[dict[str, Any]]:
        items = []
        for name, d in cfg["deployments"].items():
            caps = {
                "chat": {"chat_completion": "true", "responses": "true"},
                "embeddings": {"embeddings": "true"},
                "image": {"image_generation": "true"},
            }.get(d["kind"], {})
            items.append(
                {
                    "type": "ModelDeployment",
                    "name": name,
                    "modelName": d["model"],
                    "modelVersion": "2025-01-01",
                    "modelPublisher": "OpenAI",
                    "capabilities": caps,
                    "sku": {"name": "GlobalStandard", "capacity": 100},
                    "connectionName": None,
                }
            )
        return items

    @app.get("/api/projects/{project}/deployments")
    async def project_deployments(project: str, request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or auth[7:] not in tokens:
            return _error(401, "PermissionDenied", "token ausente")
        if tokens[auth[7:]] != "https://ai.azure.com/.default":
            return _error(401, "InvalidAudience", "escopo errado para o projeto")
        return {"value": project_items()}

    @app.get(
        "/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{account}/deployments"
    )
    async def arm_deployments(sub: str, rg: str, account: str, request: Request):
        auth = request.headers.get("authorization", "")
        if (
            not auth.lower().startswith("bearer ")
            or tokens.get(auth[7:]) != "https://management.azure.com/.default"
        ):
            return _error(403, "AuthorizationFailed", "sem permissão no recurso")
        value = []
        for item in project_items():
            caps = {
                k.replace("_completion", "Completion").replace(
                    "image_generation", "imageGenerations"
                ): v
                for k, v in item["capabilities"].items()
            }
            value.append(
                {
                    "id": f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{account}/deployments/{item['name']}",
                    "name": item["name"],
                    "type": "Microsoft.CognitiveServices/accounts/deployments",
                    "sku": item["sku"],
                    "properties": {
                        "model": {
                            "format": "OpenAI",
                            "name": item["modelName"],
                            "version": item["modelVersion"],
                        },
                        "capabilities": caps,
                        "provisioningState": "Succeeded",
                    },
                }
            )
        return {"value": value}

    @app.get("/openai/v1/models")
    async def models(request: Request):
        if not authorized(request):
            return _error(
                401, "401", "Access denied due to invalid subscription key or wrong API endpoint."
            )
        return {
            "object": "list",
            "data": [{"id": d["model"], "object": "model"} for d in cfg["deployments"].values()],
        }

    # ------------------------------------------------------------------ inferência
    def last_text(body: dict[str, Any]) -> str:
        if "messages" in body:
            for msg in reversed(body.get("messages") or []):
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        value = body.get("input")
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(v if isinstance(v, str) else json.dumps(v) for v in value)
        return ""

    def prompt_text(body: dict[str, Any]) -> str:
        if "messages" in body:
            return " ".join(
                m.get("content")
                if isinstance(m.get("content"), str)
                else json.dumps(m.get("content"))
                for m in body["messages"]
            )
        return last_text(body)

    async def guard(request: Request, deployment: str | None, body: dict[str, Any], kind: str):
        if not authorized(request):
            return _error(
                401, "401", "Access denied due to invalid subscription key or wrong API endpoint."
            )
        dep = cfg["deployments"].get(deployment or "")
        if dep is None:
            return _error(
                404,
                "DeploymentNotFound",
                f"The API deployment for this resource does not exist: {deployment}",
            )
        if dep["kind"] != kind:
            return _error(
                400,
                "OperationNotSupported",
                f"The {kind} operation does not work with the specified model.",
            )
        text = last_text(body)
        key = hashlib.sha256(text.encode()).hexdigest()
        app.state.attempts[key] += 1
        if "#429-uma-vez" in text and app.state.attempts[key] == 1:
            return _error(
                429,
                "429",
                "Rate limit is exceeded. Try again in 1 seconds.",
                {"retry-after-ms": "50"},
            )
        if "#429" in text and "#429-uma-vez" not in text:
            return _error(
                429,
                "429",
                "Rate limit is exceeded. Try again in 60 seconds.",
                {"retry-after": "60"},
            )
        if "#500" in text:
            return _error(500, "InternalServerError", "falha simulada")
        if "#filtro" in text:
            return JSONResponse(
                {
                    "error": {
                        "code": "content_filter",
                        "message": "The response was filtered due to the prompt triggering content management policy.",
                        "param": "prompt",
                        "status": 400,
                    }
                },
                status_code=400,
            )
        return None

    def answer_for(body: dict[str, Any], model: str) -> str:
        text = last_text(body)
        return f"Resposta simulada de {model}: {text}".strip()

    def chat_usage(body: dict[str, Any], completion: str) -> dict[str, Any]:
        prompt = _tokens(prompt_text(body)) * cfg["scale"]
        out = _tokens(completion) * cfg["scale"]
        cached = prompt // 2 if "#cache-prompt" in prompt_text(body) else 0
        reasoning = 8 if body.get("model", "").startswith("o") else 0
        return {
            "prompt_tokens": prompt,
            "completion_tokens": out + reasoning,
            "total_tokens": prompt + out + reasoning,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        }

    async def chat(request: Request, deployment: str | None):
        body = await request.json()
        app.state.requests.append(
            {"path": request.url.path, "body": body, "headers": dict(request.headers)}
        )
        deployment = deployment or body.get("model")
        blocked = await guard(request, deployment, body, "chat")
        if blocked is not None:
            return blocked
        app.state.calls[("chat", deployment)] += 1
        model = cfg["deployments"][deployment]["model"]
        content = answer_for(body, model)
        usage = chat_usage({**body, "model": model}, content)
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        if not body.get("stream"):
            return {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": f"{model}-2025-01-01",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
                "prompt_filter_results": [{"prompt_index": 0, "content_filter_results": {}}],
            }
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        cut = "#corta" in last_text(body)

        async def events():
            base = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": f"{model}-2025-01-01",
            }
            yield (
                "data: "
                + json.dumps(
                    {
                        **base,
                        "choices": [],
                        "prompt_filter_results": [
                            {"prompt_index": 0, "content_filter_results": {}}
                        ],
                    }
                )
                + "\n\n"
            )
            yield (
                "data: "
                + json.dumps(
                    {
                        **base,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                + "\n\n"
            )
            words = content.split(" ")
            for i, word in enumerate(words):
                if cut and i == len(words) // 2:
                    raise RuntimeError("conexão interrompida (simulada)")
                piece = word if i == 0 else " " + word
                await asyncio.sleep(cfg["delay"])
                yield (
                    "data: "
                    + json.dumps(
                        {
                            **base,
                            "choices": [
                                {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                            ],
                        }
                    )
                    + "\n\n"
                )
            yield (
                "data: "
                + json.dumps(
                    {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                )
                + "\n\n"
            )
            if include_usage:
                yield "data: " + json.dumps({**base, "choices": [], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/openai/v1/chat/completions")
    async def chat_v1(request: Request):
        return await chat(request, None)

    @app.post("/openai/deployments/{deployment}/chat/completions")
    async def chat_classic(deployment: str, request: Request):
        if not request.query_params.get("api-version"):
            return _error(404, "404", "Resource not found")
        return await chat(request, deployment)

    async def embed(request: Request, deployment: str | None):
        body = await request.json()
        app.state.requests.append(
            {"path": request.url.path, "body": body, "headers": dict(request.headers)}
        )
        deployment = deployment or body.get("model")
        blocked = await guard(request, deployment, body, "embeddings")
        if blocked is not None:
            return blocked
        app.state.calls[("embeddings", deployment)] += 1
        inputs = body.get("input")
        items = inputs if isinstance(inputs, list) else [inputs]
        data = []
        total = 0
        for i, item in enumerate(items):
            text = item if isinstance(item, str) else json.dumps(item)
            total += _tokens(text) * cfg["scale"]
            digest = hashlib.sha256(text.encode()).digest()
            vector = [round((b - 128) / 128, 4) for b in digest[:8]]
            data.append({"object": "embedding", "index": i, "embedding": vector})
        model = cfg["deployments"][deployment]["model"]
        return {
            "object": "list",
            "data": data,
            "model": model,
            "usage": {"prompt_tokens": total, "total_tokens": total},
        }

    @app.post("/openai/v1/embeddings")
    async def embeddings_v1(request: Request):
        return await embed(request, None)

    @app.post("/openai/deployments/{deployment}/embeddings")
    async def embeddings_classic(deployment: str, request: Request):
        return await embed(request, deployment)

    @app.post("/openai/v1/responses")
    async def responses(request: Request):
        body = await request.json()
        app.state.requests.append(
            {"path": request.url.path, "body": body, "headers": dict(request.headers)}
        )
        deployment = body.get("model")
        blocked = await guard(request, deployment, body, "chat")
        if blocked is not None:
            return blocked
        app.state.calls[("responses", deployment)] += 1
        model = cfg["deployments"][deployment]["model"]
        content = answer_for(body, model)
        prompt = (_tokens(last_text(body)) + _tokens(body.get("instructions") or "")) * cfg["scale"]
        out = _tokens(content) * cfg["scale"]
        usage = {
            "input_tokens": prompt,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": out,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": prompt + out,
        }
        rid = f"resp_{uuid.uuid4().hex}"
        response = {
            "id": rid,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output": [
                {
                    "id": f"msg_{uuid.uuid4().hex}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": content, "annotations": []}],
                }
            ],
            "usage": usage,
        }
        if not body.get("stream"):
            return response

        async def events():
            seq = 0

            def ev(kind: str, data: dict[str, Any]) -> str:
                nonlocal seq
                seq += 1
                return f"event: {kind}\ndata: {json.dumps({'type': kind, 'sequence_number': seq, **data})}\n\n"

            yield ev(
                "response.created",
                {"response": {**response, "status": "in_progress", "output": [], "usage": None}},
            )
            for i, word in enumerate(content.split(" ")):
                await asyncio.sleep(cfg["delay"])
                yield ev(
                    "response.output_text.delta",
                    {
                        "item_id": response["output"][0]["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": word if i == 0 else " " + word,
                    },
                )
            yield ev("response.completed", {"response": response})

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


def run() -> None:
    import uvicorn

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("FAKE_PORT", "8070")),
        log_level="info",
    )


if __name__ == "__main__":
    run()
