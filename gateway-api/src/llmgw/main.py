"""Aplicação FastAPI do gateway."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from llmgw import __version__
from llmgw import models as m
from llmgw.db import dispose_engine, init_engine, sessionmaker
from llmgw.errors import install_handlers
from llmgw.routers import (
    audit_log,
    auth,
    keys,
    models_admin,
    openai_api,
    providers,
    public,
    usage,
    users,
)
from llmgw.security.directory import Directory, LdapDirectory, StaticDirectory
from llmgw.security.tokens import SessionIssuer
from llmgw.services import audit
from llmgw.services.cache import build_cache
from llmgw.services.events import build_event_bus
from llmgw.services.foundry import build_http_client
from llmgw.services.gateway import Gateway
from llmgw.services.limits import build_rate_limiter
from llmgw.services.secrets import SecretStore, build_secret_store, system_path
from llmgw.services.usage import UsageRecorder
from llmgw.settings import Settings, get_settings
from llmgw.state import AppState

log = logging.getLogger(__name__)


async def load_session_key(settings: Settings, store: SecretStore) -> str:
    """Chave das sessões do console: env > Vault > gerada e gravada no Vault."""
    if settings.session_key:
        return settings.session_key
    path = system_path(settings, "keys")
    stored = await store.read(path) or {}
    key = stored.get("session_key")
    if not key:
        key = secrets.token_urlsafe(48)
        await store.write(path, {**stored, "session_key": key})
        log.info("chave de sessão gerada e gravada no cofre")
    return key


async def load_ldap_password(settings: Settings, store: SecretStore) -> str | None:
    """Senha da conta de serviço do LDAP: env > Vault (`<prefixo>/_system/ldap`)."""
    if settings.ldap_bind_password:
        return settings.ldap_bind_password
    stored = await store.read(system_path(settings, "ldap")) or {}
    return stored.get("bind_password")


async def build_directory(settings: Settings, store: SecretStore) -> Directory:
    if settings.auth_mode == "dev":
        log.warning("Modo dev: o login do console aceita qualquer usuário sem senha")
        return StaticDirectory()
    return LdapDirectory(settings, await load_ldap_password(settings, store))


async def build_state(
    settings: Settings,
    *,
    secrets_store: SecretStore | None = None,
    directory: Directory | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AppState:
    store = secrets_store or build_secret_store(settings)
    redis = None
    if settings.effective_redis_url:
        import redis.asyncio as aioredis

        redis = aioredis.from_url(settings.effective_redis_url, decode_responses=True)
    bus = build_event_bus(redis)
    http = build_http_client(
        timeout_s=settings.upstream_timeout_s,
        connect_timeout_s=settings.upstream_connect_timeout_s,
        max_connections=settings.upstream_max_connections,
        transport=transport,
    )
    return AppState(
        settings=settings,
        secrets=store,
        bus=bus,
        gateway=Gateway(settings, store, bus, http),
        cache=build_cache(redis, settings.cache_memory_max_entries),
        limiter=build_rate_limiter(redis),
        usage=UsageRecorder(settings),
        sessions=SessionIssuer(await load_session_key(settings, store)),
        directory=directory or await build_directory(settings, store),
        http=http,
        redis=redis,
    )


async def seed(settings: Settings) -> None:
    """Cadastra os provedores de `LLMGW_BOOTSTRAP_PROVIDERS` (segredo provisionado no Vault)."""
    wanted = settings.bootstrap_provider_map
    if not wanted:
        return
    async with sessionmaker()() as session:
        existing = set((await session.scalars(select(m.Provider.name))).all())
        for name, path in wanted.items():
            if name in existing:
                continue
            provider = m.Provider(
                name=name, vault_path=path, managed_secret=False, created_by="system"
            )
            session.add(provider)
            await session.flush()
            audit.record(
                session,
                actor="system",
                actor_type="system",
                action="provider.create",
                resource_type="provider",
                resource_id=provider.id,
                detail=f"LLMGW_BOOTSTRAP_PROVIDERS ({path})",
            )
            log.info("provedor %s cadastrado a partir de %s", name, path)
        await session.commit()


def create_app(settings: Settings | None = None, *, state: AppState | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_engine(settings)
        st = state or await build_state(settings)
        app.state.llmgw = st
        await seed(settings)
        await st.gateway.reload(refresh_secrets=True)
        tasks: list[asyncio.Task] = []
        if settings.background_tasks_enabled:
            from llmgw.services import ldap_sync, maintenance

            tasks = [
                asyncio.create_task(st.gateway.run_forever()),
                asyncio.create_task(st.gateway.listen()),
                asyncio.create_task(ldap_sync.run_forever(st)),
                asyncio.create_task(maintenance.run_forever(settings)),
            ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await st.usage.drain()
            await st.http.aclose()
            await st.bus.close()
            if st.redis is not None:
                await st.redis.aclose()
            await dispose_engine()

    app = FastAPI(
        title="Gateway LLM",
        description=(
            "API compatível com a OpenAI (`/v1`) na frente do Microsoft Foundry, "
            "e API do console (`/api/v1`)."
        ),
        version=__version__,
        lifespan=lifespan,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
    )
    install_handlers(app)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["x-request-id", "x-llmgw-cache", "x-llmgw-cost-usd"],
        )
    for module in (
        public,
        auth,
        keys,
        models_admin,
        providers,
        usage,
        audit_log,
        users,
        openai_api,
    ):
        app.include_router(module.router)
    app.include_router(openai_api.playground)

    if settings.otel_endpoint:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
    return app


def run() -> None:
    import os

    import uvicorn

    from llmgw.telemetry import setup_telemetry

    setup_telemetry("llm-gateway")
    uvicorn.run(
        "llmgw.main:create_app",
        factory=True,
        host=os.environ.get("LLMGW_HOST", "0.0.0.0"),
        port=int(os.environ.get("LLMGW_PORT", "8000")),
        proxy_headers=True,
        # Só confia em X-Forwarded-For vindo do proxy (nginx do console, ingress).
        forwarded_allow_ips=get_settings().forwarded_allow_ips,
        log_config=None,
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    run()
