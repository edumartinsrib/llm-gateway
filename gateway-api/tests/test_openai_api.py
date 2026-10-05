"""Rotas /v1 contra o Foundry simulado: roteamento, streaming, cache, custo e limites."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from conftest import PROVIDER_PATH, PROVIDER_SECRET, api, new_user_key

pytestmark = pytest.mark.usefixtures("catalog")


def sse_chunks(text: str) -> list:
    out = []
    for block in text.strip().split("\n\n"):
        data = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
        if not data:
            continue
        payload = "\n".join(data)
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


async def usage_rows(admin, **params) -> list[dict]:
    r = await admin.get("/api/v1/usage", params={"limit": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def flush(state) -> None:
    await state.usage.drain()


def chat_body(text: str, model: str = "gpt-4o-mini", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": text}], **extra}


async def test_models_list_only_enabled_and_accessible(app, admin, username):
    console, key, _ = await new_user_key(app, username)
    async with api(app, key) as c:
        r = await c.get("/v1/models")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["data"]}
    assert {"gpt-4o-mini", "gpt-4.1", "o4-mini", "text-embedding-3-small"} <= ids
    assert "dall-e-3" not in ids  # imagem não é exposta
    async with api(app, key) as c:
        assert (await c.get("/v1/models/gpt-4.1")).json()["owned_by"] == "foundry"
        assert (await c.get("/v1/models/nao-existe")).status_code == 404


async def test_chat_completion_cost_and_cache(app, admin, state, fake_foundry, username):
    _, key, me = await new_user_key(app, username)
    body = chat_body("olá gateway, tudo bem?")
    before = fake_foundry.state.calls[("chat", "gpt-4o-mini")]
    async with api(app, key) as c:
        r1 = await c.post("/v1/chat/completions", json=body)
        r2 = await c.post("/v1/chat/completions", json=body)
        r3 = await c.post("/v1/chat/completions", json=body, headers={"Cache-Control": "no-cache"})
    assert r1.status_code == 200, r1.text
    assert r1.headers["x-llmgw-cache"] == "miss"
    assert r1.json()["choices"][0]["message"]["content"].startswith("Resposta simulada")
    assert Decimal(r1.headers["x-llmgw-cost-usd"]) > 0
    assert r2.headers["x-llmgw-cache"] == "hit"
    assert r2.json() == r1.json()
    assert r3.headers["x-llmgw-cache"] == "bypass"
    # O acerto não chama o Azure.
    assert fake_foundry.state.calls[("chat", "gpt-4o-mini")] == before + 2
    sent = fake_foundry.state.requests[-1]
    assert sent["path"] == "/openai/v1/chat/completions"
    assert sent["body"]["model"] == "gpt-4o-mini"
    assert sent["headers"]["api-key"] == "chave-falsa"

    await flush(state)
    rows = await usage_rows(admin, user_id=me["id"])
    assert len(rows) == 3
    hit = next(r for r in rows if r["cache_hit"])
    miss = [r for r in rows if not r["cache_hit"]]
    assert hit["cost_usd"] == 0 and hit["saved_usd"] > 0
    usage = r1.json()["usage"]
    expected = (usage["prompt_tokens"] * 0.15 + usage["completion_tokens"] * 0.60) / 1_000_000
    assert miss[0]["cost_usd"] == pytest.approx(expected, abs=1e-6)
    assert miss[0]["prompt_tokens"] == usage["prompt_tokens"]


async def test_prompt_cache_tokens_cost_less(app, admin, state, username):
    _, key, me = await new_user_key(app, username)
    async with api(app, key) as c:
        r = await c.post(
            "/v1/chat/completions",
            json=chat_body("um prompt longo com #cache-prompt para o azure", "gpt-4.1"),
            headers={"X-LLMGW-Cache": "off"},
        )
    assert r.status_code == 200
    assert r.headers["x-llmgw-cache"] == "off"
    u = r.json()["usage"]
    cached = u["prompt_tokens_details"]["cached_tokens"]
    assert cached > 0
    expected = (
        (u["prompt_tokens"] - cached) * 2.00 + cached * 0.50 + u["completion_tokens"] * 8.00
    ) / 1_000_000
    assert float(r.headers["x-llmgw-cost-usd"]) == pytest.approx(expected, abs=1e-6)
    await flush(state)
    row = (await usage_rows(admin, user_id=me["id"]))[0]
    assert row["cached_tokens"] == cached


async def test_streaming_hides_usage_unless_asked_and_replays_from_cache(
    app, admin, state, fake_foundry, username
):
    _, key, me = await new_user_key(app, username)
    body = chat_body("conte uma história curta", stream=True)
    async with api(app, key) as c:
        r1 = await c.post("/v1/chat/completions", json=body)
        r2 = await c.post("/v1/chat/completions", json=body)
        r3 = await c.post(
            "/v1/chat/completions",
            json={**body, "stream_options": {"include_usage": True}},
        )
    assert r1.headers["content-type"].startswith("text/event-stream")
    chunks = sse_chunks(r1.text)
    assert chunks[-1] == "[DONE]"
    text = "".join(
        (c["choices"][0]["delta"].get("content") or "") for c in chunks[:-1] if c.get("choices")
    )
    assert text.startswith("Resposta simulada de gpt-4o-mini")
    # O gateway pede o uso ao Azure, mas não repassa o bloco a quem não pediu.
    assert not any(isinstance(c, dict) and c.get("usage") and not c.get("choices") for c in chunks)
    assert fake_foundry.state.requests[-1]["body"]["stream_options"] == {"include_usage": True}
    assert r2.headers["x-llmgw-cache"] == "hit"
    assert sse_chunks(r2.text) == chunks
    with_usage = sse_chunks(r3.text)
    assert any(isinstance(c, dict) and c.get("usage") for c in with_usage)

    await flush(state)
    rows = await usage_rows(admin, user_id=me["id"])
    streamed = [r for r in rows if r["stream"] and not r["cache_hit"]]
    assert streamed and all(r["total_tokens"] > 0 and r["cost_usd"] > 0 for r in streamed)
    assert all(r["ttft_ms"] is not None for r in streamed)


async def test_embeddings_and_wrong_endpoint(app, state, fake_foundry, username):
    _, key, _ = await new_user_key(app, username)
    async with api(app, key) as c:
        r = await c.post(
            "/v1/embeddings", json={"model": "text-embedding-3-small", "input": ["a", "b c"]}
        )
        again = await c.post(
            "/v1/embeddings", json={"model": "text-embedding-3-small", "input": ["a", "b c"]}
        )
        wrong = await c.post("/v1/chat/completions", json=chat_body("oi", "text-embedding-3-small"))
        wrong2 = await c.post("/v1/embeddings", json={"model": "gpt-4.1", "input": "x"})
    assert r.status_code == 200, r.text
    assert len(r.json()["data"]) == 2
    assert again.headers["x-llmgw-cache"] == "hit"
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "wrong_endpoint"
    assert wrong2.status_code == 400


async def test_responses_api_stream_and_usage(app, admin, state, username):
    _, key, me = await new_user_key(app, username)
    async with api(app, key) as c:
        r = await c.post("/v1/responses", json={"model": "gpt-4.1", "input": "resuma isto"})
        s = await c.post(
            "/v1/responses", json={"model": "gpt-4.1", "input": "resuma isto", "stream": True}
        )
    assert r.status_code == 200, r.text
    assert r.json()["output"][0]["content"][0]["text"].startswith("Resposta simulada")
    assert r.headers["x-llmgw-cache"] == "off"  # Responses API não usa o cache
    assert "event: response.completed" in s.text
    await flush(state)
    rows = await usage_rows(admin, user_id=me["id"], endpoint="responses")
    assert len(rows) == 2
    assert all(row["total_tokens"] > 0 and row["cost_usd"] > 0 for row in rows)


async def test_auth_errors(app, username):
    _, key, _ = await new_user_key(app, username)
    async with api(app, "llmgw_naoexiste.segredo") as c:
        r = await c.post("/v1/chat/completions", json=chat_body("oi"))
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
    async with api(app, key[:-3] + "xyz") as c:
        assert (await c.get("/v1/models")).status_code == 401
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers={"api-key": key}
    ) as c:
        assert (await c.get("/v1/models")).status_code == 200  # cabeçalho no estilo Azure
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/v1/models")).status_code == 401


async def test_validation_errors(app, username):
    _, key, _ = await new_user_key(app, username)
    async with api(app, key) as c:
        no_model = await c.post("/v1/chat/completions", json={"messages": []})
        bad_json = await c.post(
            "/v1/chat/completions",
            content=b"{nao json",
            headers={"content-type": "application/json"},
        )
        unknown = await c.post("/v1/chat/completions", json=chat_body("oi", "gpt-99"))
        no_messages = await c.post("/v1/chat/completions", json={"model": "gpt-4.1"})
    assert no_model.status_code == 400 and no_model.json()["error"]["param"] == "model"
    assert bad_json.status_code == 400
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "model_not_found"
    assert no_messages.status_code == 400


async def test_upstream_errors_and_retries(app, admin, state, fake_foundry, username):
    _, key, me = await new_user_key(app, username)
    async with api(app, key) as c:
        filtered = await c.post("/v1/chat/completions", json=chat_body("isso #filtro"))
        retried = await c.post("/v1/chat/completions", json=chat_body("tente #429-uma-vez"))
        throttled = await c.post("/v1/chat/completions", json=chat_body("sempre #429"))
        failing = await c.post("/v1/chat/completions", json=chat_body("quebra #500"))
    # Erro do Azure volta como veio (formato OpenAI), sem custo.
    assert filtered.status_code == 400
    assert filtered.json()["error"]["code"] == "content_filter"
    # 429 com retry-after curto: o gateway repete e o cliente nem percebe.
    assert retried.status_code == 200
    # retry-after longo (60 s): devolve o 429 na hora em vez de segurar a conexão.
    assert throttled.status_code == 429
    assert throttled.headers["retry-after"] == "60"  # repassa o tempo de espera do Azure
    assert failing.status_code == 500
    await flush(state)
    rows = await usage_rows(admin, user_id=me["id"])
    by_code = {r["status_code"] for r in rows}
    assert {400, 200, 429, 500} <= by_code
    assert next(r for r in rows if r["status_code"] == 400)["error_code"] == "content_filter"
    assert all(r["cost_usd"] == 0 for r in rows if r["status_code"] >= 400)


async def test_budget_blocks_when_exhausted(app, admin, state, username):
    console, key, me = await new_user_key(app, username)
    r = await admin.patch(f"/api/v1/users/{me['id']}", json={"monthly_budget_usd": "0.000001"})
    assert r.status_code == 200, r.text
    async with api(app, key) as c:
        first = await c.post(
            "/v1/chat/completions",
            json=chat_body("gaste um pouco", "gpt-4.1"),
            headers={"X-LLMGW-Cache": "off"},
        )
        assert first.status_code == 200
        await flush(state)
        blocked = await c.post("/v1/chat/completions", json=chat_body("mais", "gpt-4.1"))
    assert blocked.status_code == 429
    assert blocked.json()["error"]["type"] == "insufficient_quota"
    mine = (await console.get("/api/v1/me")).json()
    assert mine["spent_month_usd"] > 0
    # Alerta de orçamento registrado na auditoria.
    audit = (await admin.get("/api/v1/audit", params={"action": "budget.alert"})).json()
    assert any(username in (a["detail"] or "") for a in audit["items"])
    # Sem orçamento (null) volta a funcionar.
    await admin.patch(f"/api/v1/users/{me['id']}", json={"monthly_budget_usd": None})
    async with api(app, key) as c:
        assert (
            await c.post("/v1/chat/completions", json=chat_body("ok", "gpt-4.1"))
        ).status_code == 200


async def test_key_budget_and_key_model_restriction(app, state, username):
    console, key, _ = await new_user_key(
        app, username, monthly_budget_usd="0.000001", allowed_models=["gpt-4.1"]
    )
    async with api(app, key) as c:
        denied = await c.post("/v1/chat/completions", json=chat_body("oi", "gpt-4o-mini"))
        ok = await c.post(
            "/v1/chat/completions",
            json=chat_body("oi", "gpt-4.1"),
            headers={"X-LLMGW-Cache": "off"},
        )
        await flush(state)
        over = await c.post(
            "/v1/chat/completions",
            json=chat_body("de novo", "gpt-4.1"),
            headers={"X-LLMGW-Cache": "off"},
        )
        models = (await c.get("/v1/models")).json()["data"]
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "model_not_allowed"
    assert ok.status_code == 200
    assert over.status_code == 429 and "chave" in over.json()["error"]["message"]
    assert [m["id"] for m in models] == ["gpt-4.1"]


async def test_rate_limit_per_minute(app, admin, state, username):
    _, key, me = await new_user_key(app, username)
    await admin.patch(f"/api/v1/users/{me['id']}", json={"rpm_limit": 2})
    async with api(app, key) as c:
        codes = [
            (await c.post("/v1/chat/completions", json=chat_body(f"msg {i}"))).status_code
            for i in range(3)
        ]
        last = await c.post("/v1/chat/completions", json=chat_body("msg x"))
    assert codes == [200, 200, 429]
    assert last.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(last.headers["retry-after"]) >= 1
    # Duas recusas no mesmo minuto: só uma vai para o histórico, e nenhuma conta no mês.
    await state.usage.drain()
    rows = await usage_rows(admin, user_id=me["id"])
    assert [r["status_code"] for r in rows].count(429) == 1
    mine = (await admin.get(f"/api/v1/users/{me['id']}")).json()
    assert mine["requests_month"] == 2


async def test_model_groups_restrict_access(app, admin, catalog, username):
    _, key, me = await new_user_key(app, username)
    model_id = catalog["models"]["o4-mini"]["id"]
    await admin.patch(f"/api/v1/models/{model_id}", json={"allowed_groups": ["Dados"]})
    try:
        async with api(app, key) as c:
            denied = await c.post("/v1/chat/completions", json=chat_body("oi", "o4-mini"))
            assert denied.status_code == 403
            assert "o4-mini" not in {m["id"] for m in (await c.get("/v1/models")).json()["data"]}
        # Grupo comparado pelo nome, sem diferenciar maiúsculas (no modo dev o admin edita).
        r = await admin.patch(f"/api/v1/users/{me['id']}", json={"groups": ["dados"]})
        assert r.status_code == 200, r.text
        async with api(app, key) as c:
            allowed = await c.post("/v1/chat/completions", json=chat_body("oi", "o4-mini"))
        assert allowed.status_code == 200
    finally:
        await admin.patch(f"/api/v1/models/{model_id}", json={"allowed_groups": []})


async def test_revoked_key_and_blocked_user(app, admin, username):
    console, key, me = await new_user_key(app, username)
    keys = (await console.get("/api/v1/keys")).json()
    async with api(app, key) as c:
        assert (await c.get("/v1/models")).status_code == 200
    r = await console.post(f"/api/v1/keys/{keys[0]['id']}/revoke")
    assert r.json()["status"] == "revoked"
    async with api(app, key) as c:
        r = await c.get("/v1/models")
    assert r.status_code == 401 and "revogada" in r.json()["error"]["message"]

    _, key2, _ = await new_user_key(app, username)
    await admin.patch(
        f"/api/v1/users/{me['id']}", json={"status": "blocked", "status_reason": "teste"}
    )
    async with api(app, key2) as c:
        r = await c.get("/v1/models")
    assert r.status_code == 403 and r.json()["error"]["code"] == "user_blocked"
    # A sessão do console também cai (session_version).
    assert (await console.get("/api/v1/me")).status_code == 401
    await admin.patch(f"/api/v1/users/{me['id']}", json={"status": "active"})


async def test_rotated_credential_in_vault(app, admin, state, secrets_store, username):
    _, key, _ = await new_user_key(app, username)
    try:
        secrets_store._data[PROVIDER_PATH] = {**PROVIDER_SECRET, "api_key": "chave-errada"}
        await state.gateway.reload(refresh_secrets=True)
        async with api(app, key) as c:
            r = await c.post(
                "/v1/chat/completions", json=chat_body("oi"), headers={"X-LLMGW-Cache": "off"}
            )
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "provider_auth_error"
        provider = next(
            p for p in (await admin.get("/api/v1/providers")).json() if p["name"] == "foundry"
        )
        assert provider["status"] == "error"
        audit = (
            await admin.get("/api/v1/audit", params={"action": "provider.secret_reloaded"})
        ).json()
        assert audit["total"] >= 1
    finally:
        secrets_store._data[PROVIDER_PATH] = dict(PROVIDER_SECRET)
        await state.gateway.reload(refresh_secrets=True)
    async with api(app, key) as c:
        r = await c.post(
            "/v1/chat/completions", json=chat_body("oi"), headers={"X-LLMGW-Cache": "off"}
        )
    assert r.status_code == 200


async def test_openai_sdk_against_gateway(app, username):
    """O SDK oficial da OpenAI (o que ferramentas como o Pi usam) fala com o gateway."""
    from openai import AsyncOpenAI

    _, key, _ = await new_user_key(app, username)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    client = AsyncOpenAI(api_key=key, base_url="http://gateway.teste/v1", http_client=http)
    models = [m.id async for m in client.models.list()]
    assert "gpt-4.1" in models
    done = await client.chat.completions.create(
        model="gpt-4.1", messages=[{"role": "user", "content": "via sdk"}]
    )
    assert done.choices[0].message.content.endswith("via sdk")
    stream = await client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "user", "content": "via sdk em stream"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    parts, usage = [], None
    async for chunk in stream:
        if chunk.choices:
            parts.append(chunk.choices[0].delta.content or "")
        if chunk.usage:
            usage = chunk.usage
    assert "".join(parts).endswith("via sdk em stream")
    assert usage is not None and usage.total_tokens > 0
    emb = await client.embeddings.create(model="text-embedding-3-small", input="vetor")
    assert len(emb.data[0].embedding) == 8
    resp = await client.responses.create(model="gpt-4.1", input="pela responses api")
    assert resp.output_text.endswith("pela responses api")
    from openai import RateLimitError

    with pytest.raises(RateLimitError):
        await client.with_options(max_retries=0).chat.completions.create(
            model="gpt-4.1", messages=[{"role": "user", "content": "sempre #429"}]
        )
    await http.aclose()
