"""Fixtures: Postgres real (migrado com Alembic), Foundry simulado e cofre em memória."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from llmgw.devtools.fake_foundry import create_app as create_fake_foundry
from llmgw.main import build_state, create_app
from llmgw.security.directory import StaticDirectory
from llmgw.services.secrets import MemorySecretStore
from llmgw.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
DB_URL = os.environ.get(
    "LLMGW_TEST_DATABASE_URL", "postgresql://llmgw:llmgw@localhost:5432/llmgw_test"
)
FAKE = "http://foundry.teste"
PROVIDER_PATH = "llmgw/providers/foundry"
PROVIDER_SECRET = {
    "endpoint": FAKE,
    "auth": "api_key",
    "api_key": "chave-falsa",
    "tenant_id": "tenant-1",
    "client_id": "cliente-falso",
    "client_secret": "segredo-falso",
    "authority": FAKE,
    "project_endpoint": f"{FAKE}/api/projects/projeto-1",
}


@pytest.fixture(scope="session")
def migrated_db() -> str:
    env = {**os.environ, "LLMGW_DATABASE_URL": DB_URL}
    alembic = [sys.executable, "-m", "alembic"]
    subprocess.run(
        [*alembic, "downgrade", "base"], cwd=ROOT, env=env, check=True, capture_output=True
    )
    subprocess.run(
        [*alembic, "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True
    )
    return DB_URL


@pytest.fixture(scope="session")
def settings(migrated_db) -> Settings:
    return Settings(
        database_url=migrated_db,
        auth_mode="dev",
        secret_store="memory",
        background_tasks_enabled=False,
        bootstrap_admins="admin",
        bootstrap_providers=f"foundry={PROVIDER_PATH}",
        cache_enabled=True,
        cache_scope="user",
        upstream_retries=2,
        public_url="http://gateway.teste",
        session_key="chave-de-sessao-de-teste-com-mais-de-32-bytes",
        redis_url=None,
        timezone="America/Sao_Paulo",
        # O Foundry simulado responde em http://foundry.teste.
        provider_allow_http=True,
        provider_allowed_hosts="*",
        provider_vault_prefixes="plataforma/",
    )


@pytest.fixture(scope="session")
def fake_foundry():
    return create_fake_foundry(stream_delay_ms=0)


@pytest.fixture(scope="session")
def secrets_store() -> MemorySecretStore:
    store = MemorySecretStore()
    store._data[PROVIDER_PATH] = dict(PROVIDER_SECRET)
    return store


@pytest.fixture(scope="session")
async def app(settings, fake_foundry, secrets_store):
    state = await build_state(
        settings,
        secrets_store=secrets_store,
        directory=StaticDirectory(),
        transport=httpx.ASGITransport(app=fake_foundry),
    )
    application = create_app(settings, state=state)
    async with LifespanManager(application):
        yield application


@pytest.fixture(scope="session")
def state(app):
    return app.state.llmgw


def _client(app, headers: dict | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.teste",
        headers=headers or {},
    )


async def login(app, username: str, password: str | None = None) -> httpx.AsyncClient:
    async with _client(app) as c:
        r = await c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return _client(app, {"Authorization": f"Bearer {r.json()['token']}"})


@pytest.fixture(scope="session")
async def admin(app):
    client = await login(app, "admin")
    yield client
    await client.aclose()


@pytest.fixture(scope="session")
async def catalog(admin, state):
    """Sincroniza o provedor de teste e habilita os modelos com preço."""
    providers = (await admin.get("/api/v1/providers")).json()
    foundry = next(p for p in providers if p["name"] == "foundry")
    r = await admin.post(f"/api/v1/providers/{foundry['id']}/sync")
    assert r.status_code == 200, r.text
    models = {m["name"]: m for m in (await admin.get("/api/v1/models?all=true")).json()}
    prices = {
        "gpt-4o-mini": ("0.15", "0.075", "0.60"),
        "gpt-4.1": ("2.00", "0.50", "8.00"),
        "o4-mini": ("1.10", "0.275", "4.40"),
        "text-embedding-3-small": ("0.02", None, "0"),
    }
    for name, (inp, cached, out) in prices.items():
        r = await admin.patch(
            f"/api/v1/models/{models[name]['id']}",
            json={
                "enabled": True,
                "input_price": inp,
                "cached_input_price": cached,
                "output_price": out,
            },
        )
        assert r.status_code == 200, r.text
    return {"provider": foundry, "models": models}


@pytest.fixture
def username() -> str:
    return f"user-{uuid.uuid4().hex[:6]}"


async def new_user_key(app, name: str, **key_fields) -> tuple[httpx.AsyncClient, str, dict]:
    """Loga o usuário, cria uma chave e devolve (cliente do console, chave, usuário)."""
    console = await login(app, name)
    me = (await console.get("/api/v1/me")).json()
    r = await console.post("/api/v1/keys", json={"name": "teste", **key_fields})
    assert r.status_code == 201, r.text
    return console, r.json()["key"], me


def api(app, key: str) -> httpx.AsyncClient:
    return _client(app, {"Authorization": f"Bearer {key}"})
