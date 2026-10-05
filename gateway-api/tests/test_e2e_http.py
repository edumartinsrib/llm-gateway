"""Ponta a ponta com servidores HTTP de verdade (uvicorn):

Vault (simulado) → agente → Foundry (simulado), com o SDK oficial da OpenAI do lado do
cliente: credencial lida do Vault, descoberta na subida, streaming real, queda do
provedor no meio do stream e cliente que desiste no meio da resposta.
"""

from __future__ import annotations

import time

import httpx
import pytest
from conftest import PROVIDER_PATH, PROVIDER_SECRET
from test_vault_and_redis import fake_vault, free_port, serve

from llmgw.db import init_engine
from llmgw.devtools.fake_foundry import create_app as create_fake_foundry
from llmgw.main import create_app


@pytest.fixture(scope="module")
def stack(settings):
    foundry_port, vault_port, gw_port = free_port(), free_port(), free_port()
    foundry_url = f"http://127.0.0.1:{foundry_port}"
    vault = fake_vault()
    vault.state.data[("secret", PROVIDER_PATH)] = (
        1,
        {
            **PROVIDER_SECRET,
            "endpoint": foundry_url,
            "authority": foundry_url,
            "project_endpoint": f"{foundry_url}/api/projects/projeto-1",
        },
    )
    gw_settings = settings.model_copy(
        update={
            "secret_store": "vault",
            "vault_addr": f"http://127.0.0.1:{vault_port}",
            "vault_token": "root-teste",
            "background_tasks_enabled": True,
            "auto_enable_models": True,
            "session_key": None,
        }
    )
    with (
        serve(create_fake_foundry(stream_delay_ms=20), foundry_port),
        serve(vault, vault_port),
        serve(create_app(gw_settings), gw_port),
    ):
        yield {"url": f"http://127.0.0.1:{gw_port}", "vault": vault}
    init_engine(settings)  # devolve o engine global aos outros testes


def _wait_models(url: str, key: str) -> list[str]:
    for _ in range(100):
        r = httpx.get(f"{url}/v1/models", headers={"Authorization": f"Bearer {key}"})
        ids = [m["id"] for m in r.json().get("data", [])]
        if "gpt-4.1" in ids:
            return ids
        time.sleep(0.1)
    raise AssertionError("modelos não apareceram")


def _usage(url: str, token: str, **params) -> list[dict]:
    r = httpx.get(
        f"{url}/api/v1/usage",
        params={"limit": 200, **params},
        headers={"Authorization": f"Bearer {token}"},
    )
    return r.json()["items"]


def test_end_to_end_with_openai_sdk(stack):
    from openai import APIError, OpenAI

    url = stack["url"]
    # A chave de sessão foi gerada e gravada no Vault na subida.
    assert "session_key" in stack["vault"].state.data[("secret", "llmgw/_system/keys")][1]
    admin = httpx.post(f"{url}/api/v1/auth/login", json={"username": "admin"}).json()["token"]
    user = httpx.post(f"{url}/api/v1/auth/login", json={"username": "e2e-sdk"}).json()
    token = user["token"]
    key = httpx.post(
        f"{url}/api/v1/keys", json={"name": "sdk"}, headers={"Authorization": f"Bearer {token}"}
    ).json()["key"]
    ids = _wait_models(url, key)
    assert "text-embedding-3-small" in ids

    client = OpenAI(api_key=key, base_url=f"{url}/v1", max_retries=0)
    done = client.chat.completions.create(
        model="gpt-4.1", messages=[{"role": "user", "content": "pergunta ponta a ponta"}]
    )
    assert done.choices[0].message.content.endswith("pergunta ponta a ponta")

    started = time.monotonic()
    first_at = None
    parts = []
    for chunk in client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "user", "content": "uma resposta em várias palavras para o stream"}],
        stream=True,
    ):
        if chunk.choices and chunk.choices[0].delta.content:
            first_at = first_at or time.monotonic()
            parts.append(chunk.choices[0].delta.content)
    total = time.monotonic() - started
    assert "".join(parts).endswith("várias palavras para o stream")
    # Streaming de verdade: o primeiro pedaço chega bem antes do fim.
    assert first_at is not None and first_at - started < total * 0.6

    with pytest.raises(APIError):
        for _ in client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "esta resposta #corta no meio do caminho ok"}],
            stream=True,
        ):
            pass

    emb = client.embeddings.create(model="text-embedding-3-small", input=["a", "b"])
    assert len(emb.data) == 2

    # Cliente desiste depois do primeiro pedaço.
    with httpx.stream(
        "POST",
        f"{url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "X-LLMGW-Cache": "off"},
        json={
            "model": "gpt-4.1",
            "stream": True,
            "messages": [{"role": "user", "content": " ".join(["palavra"] * 60)}],
        },
        timeout=30,
    ) as resp:
        for _ in resp.iter_lines():
            break

    rows = []
    for _ in range(50):
        rows = _usage(url, admin, user_id=user["user"]["id"])
        if any(r["status_code"] == 499 for r in rows) and len(rows) >= 5:
            break
        time.sleep(0.1)
    by_status = {}
    for row in rows:
        by_status.setdefault(row["status_code"], []).append(row)
    assert len(by_status[200]) == 3  # chat, stream e embeddings
    assert all(r["cost_usd"] > 0 for r in by_status[200])
    cut_short = by_status[499][0]
    assert cut_short["error_code"] == "client_disconnected"
    # Sem o bloco de uso do Azure, o gateway registra uma estimativa (não zero).
    assert cut_short["usage_estimated"] is True and cut_short["cost_usd"] > 0
    cut = [r for r in by_status[200] + by_status.get(502, []) if r["error_code"]]
    assert cut and cut[0]["error_code"] == "upstream_stream_error"
