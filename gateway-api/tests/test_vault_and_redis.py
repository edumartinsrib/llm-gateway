"""Cofre (Vault KV v2 via hvac) e backends com Redis.

O Vault aqui é um servidor HTTP que imita a API KV v2 e o login AppRole (o
binário do Vault não está disponível em todo ambiente de teste); no compose o
gateway fala com o Vault de verdade. Os testes de Redis rodam quando
`LLMGW_TEST_REDIS_URL` está definido.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from llmgw.services.cache import CachedResponse, RedisCache
from llmgw.services.events import RedisEventBus
from llmgw.services.limits import RedisRateLimiter
from llmgw.services.secrets import VaultSecretStore
from llmgw.settings import Settings


def fake_vault() -> FastAPI:
    app = FastAPI()
    app.state.data = {}
    app.state.tokens = {"root-teste"}
    app.state.logins = 0

    def denied(request: Request) -> Response | None:
        if request.headers.get("x-vault-token") not in app.state.tokens:
            return JSONResponse({"errors": ["permission denied"]}, status_code=403)
        return None

    @app.post("/v1/auth/approle/login")
    async def login(request: Request):
        body = await request.json()
        if body.get("role_id") != "papel" or body.get("secret_id") != "segredo":
            return JSONResponse({"errors": ["invalid role or secret ID"]}, status_code=400)
        app.state.logins += 1
        token = f"t-{app.state.logins}"
        app.state.tokens.add(token)
        return {"auth": {"client_token": token, "lease_duration": 3600, "renewable": True}}

    @app.get("/v1/{mount}/data/{path:path}")
    async def read(mount: str, path: str, request: Request):
        if (resp := denied(request)) is not None:
            return resp
        item = app.state.data.get((mount, path))
        if item is None:
            return JSONResponse({"errors": []}, status_code=404)
        version, data = item
        return {
            "data": {
                "data": data,
                "metadata": {"version": version, "deletion_time": "", "destroyed": False},
            }
        }

    @app.post("/v1/{mount}/data/{path:path}")
    @app.put("/v1/{mount}/data/{path:path}")
    async def write(mount: str, path: str, request: Request):
        if (resp := denied(request)) is not None:
            return resp
        body = await request.json()
        version = app.state.data.get((mount, path), (0, None))[0] + 1
        app.state.data[(mount, path)] = (version, body["data"])
        return {"data": {"version": version}}

    @app.delete("/v1/{mount}/metadata/{path:path}")
    async def delete(mount: str, path: str, request: Request):
        if (resp := denied(request)) is not None:
            return resp
        app.state.data.pop((mount, path), None)
        return Response(status_code=204)

    return app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def vault():
    app = fake_vault()
    port = free_port()
    with serve(app, port):
        yield app, f"http://127.0.0.1:{port}"


async def test_vault_store_roundtrip_with_token(vault):
    app, url = vault
    store = VaultSecretStore(Settings(vault_addr=url, vault_token="root-teste", vault_mount="kv"))
    assert await store.read("llmgw/providers/x") is None
    await store.write("llmgw/providers/x", {"endpoint": "https://r", "api_key": "k"})
    assert await store.read("llmgw/providers/x") == {"endpoint": "https://r", "api_key": "k"}
    assert app.state.data[("kv", "llmgw/providers/x")][0] == 1
    await store.delete("llmgw/providers/x")
    assert await store.read("llmgw/providers/x") is None


async def test_vault_approle_relogin_when_token_expires(vault):
    app, url = vault
    store = VaultSecretStore(
        Settings(vault_addr=url, vault_token=None, vault_role_id="papel", vault_secret_id="segredo")
    )
    await store.write("llmgw/_system/keys", {"session_key": "abc"})
    logins = app.state.logins
    app.state.tokens = {"root-teste"}  # token do AppRole expirou
    assert await store.read("llmgw/_system/keys") == {"session_key": "abc"}
    assert app.state.logins == logins + 1


REDIS_URL = os.environ.get("LLMGW_TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="LLMGW_TEST_REDIS_URL não definido")


@pytest.fixture
async def redis():
    import redis.asyncio as aioredis

    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


@needs_redis
async def test_redis_cache(redis):
    cache = RedisCache(redis)
    value = CachedResponse(
        kind="sse",
        status=200,
        usage={"total_tokens": 2},
        cost_usd="0.5",
        events=[("data: 1", False), ("data: [DONE]", False)],
    )
    await cache.set("k-teste", value, 30)
    again = await cache.get("k-teste")
    assert again is not None and again.events == value.events
    assert await redis.ttl("llmgw:cache:k-teste") > 0
    assert await cache.clear() >= 1
    assert await cache.get("k-teste") is None


@needs_redis
async def test_redis_rate_limiter(redis):
    limiter = RedisRateLimiter(redis)
    name = f"teste:{time.time_ns()}"
    hits = [await limiter.hit(name, 2) for _ in range(3)]
    assert [h.allowed for h in hits] == [True, True, False]
    await limiter.add(f"{name}:tpm", 500)
    used, reset = await limiter.used(f"{name}:tpm")
    assert used == 500 and 0 < reset <= 60


@needs_redis
async def test_redis_event_bus(redis):
    import asyncio

    bus = RedisEventBus(redis)
    received = []

    async def listen():
        async for event, data in bus.subscribe("llmgw:teste"):
            received.append((event, data))
            return

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.2)
    await bus.publish("llmgw:teste", "reload", {"x": 1})
    await asyncio.wait_for(task, 5)
    assert received == [("reload", {"x": 1})]
