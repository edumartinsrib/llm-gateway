"""Rotas compatíveis com a API da OpenAI (`/v1`), autenticadas por chave de API.

Fluxo de uma chamada:

1. chave de API → usuário (ativo) e chave (não revogada nem expirada);
2. modelo pedido → rota (provedor + deployment), conferindo grupos e listas de modelos;
3. orçamento do mês (usuário, chave e gateway) e limites por minuto;
4. cache de respostas (correspondência exata);
5. o agente do provedor dispara para o Azure (repetindo 429/5xx antes do 1º byte);
6. a resposta volta ao cliente (inteira ou em SSE) e o uso é gravado em segundo plano.

O corpo é repassado como veio (só `model` vira o nome do deployment), então
parâmetros novos da OpenAI/Azure funcionam sem mudar o gateway.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from llmgw.deps import ApiCaller, Session, State, User
from llmgw.errors import OpenAIError
from llmgw.security.auth import Principal
from llmgw.services.cache import CachedResponse, make_key
from llmgw.services.foundry import UpstreamError
from llmgw.services.gateway import Route
from llmgw.services.usage import (
    BudgetCheck,
    Usage,
    UsageEvent,
    check_budget,
    compute_cost,
    usage_from_openai,
    usage_from_responses,
)
from llmgw.state import AppState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])
playground = APIRouter(prefix="/api/v1/playground", tags=["playground"])

ROUTE_KIND = {"chat": "chat", "responses": "chat", "embeddings": "embeddings"}
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@dataclass
class Call:
    request_id: str
    principal: Principal
    route: Route
    endpoint: str  # chat | embeddings | responses
    body: dict[str, Any]
    stream: bool
    wants_usage: bool
    started: float
    client_ip: str | None
    user_agent: str | None
    budget: BudgetCheck
    cache_key: str | None = None
    cache_ttl: int = 0
    cache_mode: str = "off"  # on | refresh | off
    headers: dict[str, str] = field(default_factory=dict)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def event(self, status: int, **kwargs: Any) -> UsageEvent:
        user = self.principal.user
        key = self.principal.api_key
        return UsageEvent(
            request_id=self.request_id,
            user_id=user.id,
            username=user.username,
            api_key_id=key.id if key else None,
            api_key_name=key.name if key else "console",
            model_name=self.route.name,
            provider_name=self.route.provider_name,
            deployment=self.route.deployment,
            endpoint=self.endpoint,
            stream=self.stream,
            status_code=status,
            client_ip=self.client_ip,
            user_agent=self.user_agent,
            user_budget=user.monthly_budget_usd,
            latency_ms=self.elapsed_ms(),
            **kwargs,
        )


# --------------------------------------------------------------------------- preparação
async def _read_body(request: Request, max_mb: int) -> dict[str, Any]:
    limit = max_mb * 1024 * 1024
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit:
        raise OpenAIError(413, f"corpo maior que {max_mb} MB", code="request_too_large")
    # Lê em pedaços e para no limite (corpo sem Content-Length não estoura a memória).
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise OpenAIError(413, f"corpo maior que {max_mb} MB", code="request_too_large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        body = json.loads(raw or b"null")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OpenAIError(400, "o corpo não é um JSON válido", code="invalid_json") from exc
    if not isinstance(body, dict):
        raise OpenAIError(400, "o corpo deve ser um objeto JSON", code="invalid_json")
    return body


def _cache_mode(request: Request) -> str:
    cc = request.headers.get("cache-control", "").lower()
    flag = request.headers.get("x-llmgw-cache", "").lower()
    if flag in ("off", "no-store") or "no-store" in cc:
        return "off"
    if flag in ("refresh", "no-cache") or "no-cache" in cc:
        return "refresh"
    return "on"


def model_allowed(principal: Principal, route: Route) -> bool:
    user = principal.user
    if not principal.is_admin and not route.allows_groups(user.groups or [], user.group_dns or []):
        return False
    if user.allowed_models is not None and route.name not in user.allowed_models:
        return False
    key = principal.api_key
    return not (
        key is not None and key.allowed_models is not None and route.name not in key.allowed_models
    )


def _ratelimit_error(message: str, reset_s: int) -> OpenAIError:
    return OpenAIError(
        429,
        message,
        type="rate_limit_error",
        code="rate_limit_exceeded",
        headers={"Retry-After": str(max(reset_s, 1))},
    )


async def _prepare(
    request: Request, principal: Principal, state: AppState, session, endpoint: str
) -> Call:
    settings = state.settings
    body = await _read_body(request, settings.max_request_mb)
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise OpenAIError(
            400, "informe o modelo no campo 'model'", param="model", code="missing_model"
        )
    request.state.requested_model = model[:200]
    route = state.gateway.route(model)
    if route is None:
        raise OpenAIError(
            404,
            f"o modelo '{model}' não existe ou não está habilitado no gateway "
            "(veja GET /v1/models)",
            param="model",
            code="model_not_found",
        )
    if not model_allowed(principal, route):
        raise OpenAIError(
            403,
            f"você não tem acesso ao modelo '{model}'",
            type="permission_error",
            param="model",
            code="model_not_allowed",
        )
    if ROUTE_KIND[endpoint] != route.kind:
        hint = "/v1/embeddings" if route.kind == "embeddings" else "/v1/chat/completions"
        raise OpenAIError(
            400,
            f"o modelo '{model}' é do tipo {route.kind}; use {hint}",
            param="model",
            code="wrong_endpoint",
        )
    if endpoint == "responses" and not route.supports_responses:
        raise OpenAIError(
            400,
            f"o modelo '{model}' não aceita a Responses API",
            param="model",
            code="wrong_endpoint",
        )
    if endpoint == "chat" and not isinstance(body.get("messages"), list):
        raise OpenAIError(
            400, "informe 'messages' (lista)", param="messages", code="missing_messages"
        )
    if endpoint == "embeddings" and body.get("input") in (None, "", []):
        raise OpenAIError(400, "informe 'input'", param="input", code="missing_input")

    user = principal.user
    key = principal.api_key
    budget = await check_budget(session, settings, user, key)
    if not budget.allowed:
        raise OpenAIError(
            429,
            budget.message or "orçamento esgotado",
            type="insufficient_quota",
            code="insufficient_quota",
        )

    headers: dict[str, str] = {}
    rpm = user.rpm_limit if user.rpm_limit is not None else settings.default_rpm
    hit = await state.limiter.hit(f"rpm:user:{user.id}", rpm)
    if not hit.allowed:
        raise _ratelimit_error(f"limite de {rpm} requisições por minuto atingido", hit.reset_s)
    if hit.limit:
        headers["x-ratelimit-limit-requests"] = str(hit.limit)
        headers["x-ratelimit-remaining-requests"] = str(hit.remaining)
        headers["x-ratelimit-reset-requests"] = f"{hit.reset_s}s"
    if key is not None and key.rpm_limit:
        key_hit = await state.limiter.hit(f"rpm:key:{key.id}", key.rpm_limit)
        if not key_hit.allowed:
            raise _ratelimit_error(
                f"limite de {key.rpm_limit} requisições por minuto da chave atingido",
                key_hit.reset_s,
            )
    tpm = user.tpm_limit if user.tpm_limit is not None else settings.default_tpm
    if tpm > 0:
        used, reset = await state.limiter.used(f"tpm:user:{user.id}")
        if used >= tpm:
            raise _ratelimit_error(f"limite de {tpm} tokens por minuto atingido", reset)
        headers["x-ratelimit-limit-tokens"] = str(tpm)
        headers["x-ratelimit-remaining-tokens"] = str(max(tpm - used, 0))
    if budget.user_remaining is not None:
        headers["x-llmgw-budget-remaining-usd"] = f"{budget.user_remaining:.2f}"

    stream = endpoint != "embeddings" and bool(body.get("stream"))
    wants_usage = bool((body.get("stream_options") or {}).get("include_usage")) if stream else True
    call = Call(
        request_id=uuid.uuid4().hex,
        principal=principal,
        route=route,
        endpoint=endpoint,
        body=body,
        stream=stream,
        wants_usage=wants_usage,
        started=time.monotonic(),
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        budget=budget,
        headers=headers,
    )
    mode = _cache_mode(request)
    if settings.cache_enabled and endpoint != "responses" and mode != "off":
        ttl = route.cache_ttl_s
        if ttl is None:
            ttl = (
                settings.cache_embeddings_ttl_s
                if endpoint == "embeddings"
                else settings.cache_chat_ttl_s
            )
        if ttl > 0:
            scope = f"user:{user.id}" if settings.cache_scope == "user" else "global"
            call.cache_key = make_key(scope, endpoint, route.name, body)
            call.cache_ttl = ttl
            call.cache_mode = mode
    call.headers["x-request-id"] = call.request_id
    return call


def estimate_prompt_tokens(body: dict[str, Any]) -> int:
    """Estimativa grosseira (≈4 caracteres por token) para quando o Azure não informa o uso."""
    parts = [body.get(k) for k in ("messages", "input", "instructions", "tools") if body.get(k)]
    return max(1, len(json.dumps(parts, ensure_ascii=False)) // 4)


def _streamed_text(endpoint: str, data: dict[str, Any]) -> str:
    """Texto gerado num evento SSE (conteúdo, argumentos de ferramenta)."""
    if endpoint == "chat":
        out = []
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            out.append(delta.get("content") or "")
            out.append(delta.get("reasoning_content") or "")
            for call in delta.get("tool_calls") or []:
                out.append(((call or {}).get("function") or {}).get("arguments") or "")
        return "".join(out)
    if str(data.get("type", "")).endswith(".delta"):
        delta = data.get("delta")
        return delta if isinstance(delta, str) else ""
    return ""


def _cache_header(call: Call, hit: bool) -> str:
    if call.cache_key is None:
        return "off"
    if hit:
        return "hit"
    return "bypass" if call.cache_mode == "refresh" else "miss"


def _upstream_body(call: Call) -> dict[str, Any]:
    body = dict(call.body)
    if call.stream and call.endpoint == "chat":
        options = dict(body.get("stream_options") or {})
        options["include_usage"] = True
        body["stream_options"] = options
    return body


def _usage_of(call: Call, data: dict[str, Any]) -> Usage:
    raw = data.get("usage")
    found = usage_from_responses(raw) if call.endpoint == "responses" else usage_from_openai(raw)
    return found or Usage()


FORWARDED_ERROR_HEADERS = ("retry-after", "retry-after-ms")


def _upstream_error_headers(resp: httpx.Response) -> dict[str, str]:
    """Cabeçalhos do Azure úteis ao cliente num erro (ex.: quanto esperar num 429)."""
    return {
        k: v
        for k, v in resp.headers.items()
        if k.lower() in FORWARDED_ERROR_HEADERS or k.lower().startswith("x-ratelimit-")
    }


def _error_info(raw: bytes) -> tuple[str | None, str | None]:
    try:
        data = json.loads(raw)
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return error.get("code") or error.get("type"), str(error.get("message") or "")[:1000]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return None, raw[:300].decode(errors="replace") if raw else None


# --------------------------------------------------------------------------- cache
def _replay(call: Call, cached: CachedResponse) -> AsyncIterator[str]:
    async def gen():
        for block, usage_only in cached.events:
            if usage_only and not call.wants_usage:
                continue
            yield block + "\n\n"

    return gen()


def _serve_cached(state: AppState, call: Call, cached: CachedResponse) -> Response:
    usage = Usage.from_dict(cached.usage)
    state.usage.submit(
        call.event(
            200,
            usage=usage,
            cost_usd=Decimal("0"),
            cache_hit=True,
            saved_usd=Decimal(cached.cost_usd or "0"),
            ttft_ms=call.elapsed_ms() if call.stream else None,
        )
    )
    headers = {**call.headers, "x-llmgw-cache": "hit", "x-llmgw-cost-usd": "0"}
    if cached.kind == "sse":
        return StreamingResponse(
            _replay(call, cached),
            media_type="text/event-stream",
            headers={**headers, **SSE_HEADERS},
        )
    return JSONResponse(cached.body, status_code=cached.status, headers=headers)


# --------------------------------------------------------------------------- chamada ao Azure
async def _send(state: AppState, call: Call) -> httpx.Response:
    agent = state.gateway.agent(call.route.provider_id)
    if agent is None:
        state.usage.submit(call.event(503, error_code="provider_unavailable"))
        raise OpenAIError(
            503,
            f"o provedor '{call.route.provider_name}' está indisponível no gateway",
            type="api_error",
            code="provider_unavailable",
        )
    try:
        return await agent.send(call.endpoint, call.route.deployment, _upstream_body(call))
    except UpstreamError as exc:
        if exc.auth_failure:
            await state.gateway.mark_provider_error(call.route.provider_id, exc.message)
            log.error("provedor %s recusou a credencial: %s", call.route.provider_name, exc.detail)
        state.usage.submit(call.event(exc.status, error_code=exc.code, error_message=exc.message))
        raise OpenAIError(exc.status, exc.message, type="api_error", code=exc.code) from exc


async def _complete(state: AppState, call: Call) -> Response:
    resp = await _send(state, call)
    try:
        raw = await resp.aread()
    finally:
        await resp.aclose()
    if resp.status_code >= 400:
        code, message = _error_info(raw)
        state.usage.submit(call.event(resp.status_code, error_code=code, error_message=message))
        return Response(
            raw,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
            headers={
                **_upstream_error_headers(resp),
                **call.headers,
                "x-llmgw-cache": _cache_header(call, False),
            },
        )
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        state.usage.submit(call.event(502, error_code="invalid_upstream_response"))
        raise OpenAIError(
            502, "resposta inválida do provedor", type="api_error", code="invalid_upstream_response"
        ) from exc
    usage = _usage_of(call, data)
    cost = compute_cost(call.route, usage)
    state.usage.submit(call.event(resp.status_code, usage=usage, cost_usd=cost))
    state.usage.spawn(state.limiter.add(f"tpm:user:{call.principal.user.id}", usage.total_tokens))
    if call.cache_key and len(raw) <= state.settings.cache_max_entry_kb * 1024:
        cached = CachedResponse(
            kind="json",
            status=resp.status_code,
            usage=usage.as_dict(),
            cost_usd=str(cost),
            body=data,
        )
        state.usage.spawn(state.cache.set(call.cache_key, cached, call.cache_ttl))
    headers = {
        **call.headers,
        "x-llmgw-cache": _cache_header(call, False),
        "x-llmgw-cost-usd": f"{cost:.10f}",
    }
    return Response(
        raw, status_code=resp.status_code, media_type="application/json", headers=headers
    )


async def _sse_events(resp: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
    """Agrupa as linhas em eventos SSE; devolve (bloco bruto, dados decodificados)."""
    lines: list[str] = []

    def build() -> tuple[str, Any]:
        payload = "\n".join(
            line[5:].lstrip() if line.startswith("data:") else ""
            for line in lines
            if line.startswith("data:")
        )
        data: Any = None
        if payload == "[DONE]":
            data = "[DONE]"
        elif payload:
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = None
        return "\n".join(lines), data

    async for line in resp.aiter_lines():
        if line == "":
            if lines:
                yield build()
                lines = []
            continue
        lines.append(line)
    if lines:
        yield build()


async def _relay(state: AppState, call: Call, resp: httpx.Response) -> AsyncIterator[str]:
    usage: Usage | None = None
    ttft: int | None = None
    completed = False
    status = 200
    error_code: str | None = None
    error_message: str | None = None
    keep = call.cache_key is not None
    kept: list[tuple[str, bool]] = []
    size = 0
    generated = 0  # caracteres gerados, para estimar o uso se o stream acabar antes da hora
    limit = state.settings.cache_max_entry_kb * 1024
    try:
        async for block, data in _sse_events(resp):
            if ttft is None:
                ttft = call.elapsed_ms()
            usage_only = False
            if data == "[DONE]":
                completed = True
            elif isinstance(data, dict):
                generated += len(_streamed_text(call.endpoint, data))
                if call.endpoint == "chat":
                    found = usage_from_openai(data.get("usage")) if data.get("usage") else None
                    if found:
                        usage = found
                        usage_only = not data.get("choices")
                elif call.endpoint == "responses":
                    kind = data.get("type")
                    if kind in ("response.completed", "response.incomplete", "response.failed"):
                        found = usage_from_responses((data.get("response") or {}).get("usage"))
                        usage = found or usage
                        completed = kind == "response.completed"
                    elif kind == "error":
                        error_code = "upstream_stream_error"
                if isinstance(data.get("error"), dict):
                    error_code = data["error"].get("code") or "upstream_stream_error"
                    error_message = str(data["error"].get("message") or "")[:1000]
            if keep:
                size += len(block)
                if size > limit:
                    keep = False
                    kept = []
                else:
                    kept.append((block, usage_only))
            if usage_only and not call.wants_usage:
                continue
            yield block + "\n\n"
    except httpx.HTTPError as exc:
        status = 502
        error_code = "upstream_stream_error"
        error_message = f"o provedor interrompeu a resposta ({type(exc).__name__})"
        log.warning("stream do provedor %s interrompido: %s", call.route.provider_name, exc)
        body = OpenAIError(502, error_message, type="api_error", code=error_code).body()
        yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        # Cliente desconectou no meio da resposta.
        status = 499
        error_code = "client_disconnected"
        raise
    except Exception as exc:
        status = 500
        error_code = "gateway_error"
        error_message = str(exc)[:300]
        log.exception("falha ao repassar o stream da requisição %s", call.request_id)
        raise
    finally:
        estimated = usage is None
        if usage is None:
            # Stream interrompido antes do bloco de uso (cliente desistiu, provedor caiu):
            # o Azure cobra o que gerou, então o gateway registra uma estimativa.
            prompt = estimate_prompt_tokens(call.body)
            completion = (generated + 3) // 4 if generated else 0
            final = Usage(prompt, 0, completion, 0, prompt + completion)
        else:
            final = usage
        cost = compute_cost(call.route, final)
        state.usage.submit(
            call.event(
                status,
                usage=final,
                cost_usd=cost,
                ttft_ms=ttft,
                error_code=error_code,
                error_message=error_message,
                usage_estimated=estimated,
            )
        )
        state.usage.spawn(
            state.limiter.add(f"tpm:user:{call.principal.user.id}", final.total_tokens)
        )
        if keep and completed and status == 200 and error_code is None and kept:
            cached = CachedResponse(
                kind="sse", status=200, usage=final.as_dict(), cost_usd=str(cost), events=kept
            )
            state.usage.spawn(state.cache.set(call.cache_key, cached, call.cache_ttl))
        # Por último: com o cliente desconectado, este await pode ser cancelado.
        with contextlib.suppress(Exception):
            await resp.aclose()


async def _stream(state: AppState, call: Call) -> Response:
    resp = await _send(state, call)
    if resp.status_code >= 400:
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
        code, message = _error_info(raw)
        state.usage.submit(call.event(resp.status_code, error_code=code, error_message=message))
        return Response(
            raw,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
            headers={**_upstream_error_headers(resp), **call.headers},
        )
    headers = {**call.headers, **SSE_HEADERS, "x-llmgw-cache": _cache_header(call, False)}
    return StreamingResponse(
        _relay(state, call, resp), media_type="text/event-stream", headers=headers
    )


async def _handle(request: Request, principal: Principal, state: AppState, session, endpoint: str):
    try:
        call = await _prepare(request, principal, state, session, endpoint)
    except OpenAIError as exc:
        if exc.status in (403, 429):
            await _record_rejection(request, principal, state, endpoint, exc)
        raise
    # A conexão do banco volta ao pool antes da chamada ao Azure (que pode levar minutos).
    await session.close()
    if call.cache_key and call.cache_mode == "on":
        cached = await state.cache.get(call.cache_key)
        if cached is not None:
            return _serve_cached(state, call, cached)
    if call.stream:
        return await _stream(state, call)
    return await _complete(state, call)


async def _record_rejection(
    request: Request, principal: Principal, state: AppState, endpoint: str, exc: OpenAIError
) -> None:
    """Registra chamadas barradas por orçamento, limite ou acesso (aparecem no histórico).

    No máximo uma por usuário e motivo a cada minuto: um cliente insistindo depois do
    429 não vira uma enxurrada de escritas no banco.
    """
    user = principal.user
    if not (await state.limiter.hit(f"rejlog:{user.id}:{exc.code}", 1)).allowed:
        return
    key = principal.api_key
    model = getattr(request.state, "requested_model", None) or "?"
    state.usage.submit(
        UsageEvent(
            request_id=uuid.uuid4().hex,
            user_id=user.id,
            username=user.username,
            api_key_id=key.id if key else None,
            api_key_name=key.name if key else None,
            model_name=model,
            provider_name=None,
            deployment=None,
            endpoint=endpoint,
            stream=False,
            status_code=exc.status,
            error_code=exc.code,
            error_message=exc.message,
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )


# --------------------------------------------------------------------------- rotas
@router.post("/chat/completions")
async def chat_completions(request: Request, principal: ApiCaller, state: State, session: Session):
    return await _handle(request, principal, state, session, "chat")


@router.post("/embeddings")
async def embeddings(request: Request, principal: ApiCaller, state: State, session: Session):
    return await _handle(request, principal, state, session, "embeddings")


@router.post("/responses")
async def responses(request: Request, principal: ApiCaller, state: State, session: Session):
    return await _handle(request, principal, state, session, "responses")


def _model_obj(route: Route) -> dict[str, Any]:
    return {
        "id": route.name,
        "object": "model",
        "created": route.created,
        "owned_by": route.provider_name,
    }


@router.get("/models")
async def list_models(principal: ApiCaller, state: State) -> dict[str, Any]:
    data = [_model_obj(r) for r in state.gateway.routes() if model_allowed(principal, r)]
    return {"object": "list", "data": data}


@router.get("/models/{model:path}")
async def get_model(model: str, principal: ApiCaller, state: State) -> dict[str, Any]:
    route = state.gateway.route(model)
    if route is None or not model_allowed(principal, route):
        raise OpenAIError(
            404, f"o modelo '{model}' não existe", code="model_not_found", param="model"
        )
    return _model_obj(route)


# --------------------------------------------------------------------------- playground do console
@playground.post("/chat/completions")
async def playground_chat(request: Request, principal: User, state: State, session: Session):
    """Mesmo fluxo do /v1, autenticado pela sessão do console (para testar modelos)."""
    return await _handle(request, principal, state, session, "chat")


@playground.post("/embeddings")
async def playground_embeddings(request: Request, principal: User, state: State, session: Session):
    return await _handle(request, principal, state, session, "embeddings")
