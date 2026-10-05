"""Agente gateway: provedores, modelos e roteamento das chamadas.

Na subida (e a cada `LLMGW_PROVIDER_REFRESH_S`) o gateway:

1. lê os provedores do Postgres e, para cada um, a credencial no Vault;
2. monta um `FoundryAgent` por provedor (ou mantém o atual, se a credencial não mudou;
   uma credencial rotacionada no Vault troca o agente sem reiniciar o serviço);
3. monta a tabela de rotas: nome público do modelo → provedor + deployment + preço.

A descoberta (a cada `LLMGW_DISCOVERY_INTERVAL_S` ou pelo console) lista os
deployments do Foundry e mantém a tabela `llm_models` em dia: deployment novo
entra desabilitado (até um administrador revisar preço e acesso, ou habilitado
direto com `LLMGW_AUTO_ENABLE_MODELS=true`); deployment que sumiu fica indisponível.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy import select, text

from llmgw.db import sessionmaker
from llmgw.models import LlmModel, Provider
from llmgw.services import audit
from llmgw.services.events import CONFIG_CHANNEL, EventBus
from llmgw.services.foundry import (
    CheckResult,
    FoundryAgent,
    FoundryConfig,
    ProviderConfigError,
    UpstreamError,
    guess_features,
)
from llmgw.services.groups import GroupRule
from llmgw.services.secrets import SecretStore
from llmgw.settings import Settings

log = logging.getLogger(__name__)

SYNC_LOCK_KEY = 7_521_001
PROVIDER_LOCK_KEY = 7_521_004


def _lock_id(provider_id: str) -> int:
    """Inteiro de 32 bits estável a partir do id do provedor (chave do advisory lock)."""
    return int(str(provider_id).replace("-", "")[:8], 16) - 2**31


@dataclass(frozen=True)
class Route:
    model_id: str
    name: str
    kind: str
    provider_id: str
    provider_name: str
    deployment: str
    input_price: Decimal
    cached_input_price: Decimal | None
    output_price: Decimal
    cache_ttl_s: int | None
    allowed_groups: GroupRule
    supports_responses: bool
    created: int

    def allows_groups(self, names: Iterable[str], dns: Iterable[str] = ()) -> bool:
        if not self.allowed_groups:
            return True
        return self.allowed_groups.matches(names, dns)


@dataclass
class SyncResult:
    found: int = 0
    created: int = 0
    updated: int = 0
    missing: int = 0
    skipped: int = 0


class Gateway:
    def __init__(
        self,
        settings: Settings,
        secrets: SecretStore,
        bus: EventBus,
        http: httpx.AsyncClient,
    ):
        self.settings = settings
        self._secrets = secrets
        self._bus = bus
        self._http = http
        self._agents: dict[str, FoundryAgent] = {}
        self._routes: dict[str, Route] = {}
        self._lock = asyncio.Lock()
        self._error_marked: dict[str, float] = {}
        self.loaded_at: float | None = None

    # ------------------------------------------------------------------ leitura
    def route(self, name: str) -> Route | None:
        return self._routes.get(name)

    def routes(self) -> list[Route]:
        return sorted(self._routes.values(), key=lambda r: r.name)

    def agent(self, provider_id: str) -> FoundryAgent | None:
        return self._agents.get(provider_id)

    # ------------------------------------------------------------------ montagem
    async def _load_agent(self, session, provider: Provider, *, force: bool) -> FoundryAgent | None:
        pid = str(provider.id)
        current = self._agents.get(pid)
        if current is not None and not force:
            return current
        try:
            data = await self._secrets.read(provider.vault_path)
            config = FoundryConfig.from_secret(data)
        except ProviderConfigError as exc:
            self._agents.pop(pid, None)
            provider.status = "error"
            provider.last_error = f"credencial inválida em {provider.vault_path}: {exc}"
            return None
        except Exception as exc:  # noqa: BLE001 - Vault fora do ar: mantém o agente atual
            log.warning("não foi possível ler %s no Vault: %s", provider.vault_path, exc)
            if current is None:
                provider.status = "error"
                provider.last_error = f"Vault indisponível: {type(exc).__name__}"
            return current

        if current is not None and current.fingerprint == config.fingerprint():
            return current
        agent = FoundryAgent(
            pid, provider.name, config, self._http, retries=self.settings.upstream_retries
        )
        self._agents[pid] = agent
        provider.endpoint = config.endpoint
        provider.auth_type = config.auth
        provider.discovery = config.discovery
        provider.secret_loaded_at = datetime.now(UTC)
        if provider.status != "ok" or provider.last_error:
            provider.status = "ok"
            provider.last_error = None
        if current is not None:
            log.info("credencial do provedor %s mudou no Vault; agente recriado", provider.name)
            audit.record(
                session,
                actor="system",
                actor_type="system",
                action="provider.secret_reloaded",
                resource_type="provider",
                resource_id=provider.id,
                detail="credencial rotacionada no Vault; agente recriado",
            )
        return agent

    async def reload(self, *, refresh_secrets: bool = False) -> None:
        """Relê provedores e modelos do banco e refaz a tabela de rotas."""
        async with self._lock:
            async with sessionmaker()() as session:
                providers = (await session.scalars(select(Provider))).all()
                active: dict[str, Provider] = {}
                for provider in providers:
                    pid = str(provider.id)
                    if not provider.enabled:
                        self._agents.pop(pid, None)
                        continue
                    if await self._load_agent(session, provider, force=refresh_secrets):
                        active[pid] = provider
                for pid in list(self._agents):
                    if pid not in {str(p.id) for p in providers}:
                        self._agents.pop(pid, None)
                models = (
                    await session.scalars(
                        select(LlmModel).where(
                            LlmModel.enabled.is_(True), LlmModel.available.is_(True)
                        )
                    )
                ).all()
                await session.commit()

            routes: dict[str, Route] = {}
            for m in models:
                provider = active.get(str(m.provider_id))
                if provider is None or m.kind not in ("chat", "embeddings"):
                    continue
                routes[m.name] = Route(
                    model_id=str(m.id),
                    name=m.name,
                    kind=m.kind,
                    provider_id=str(m.provider_id),
                    provider_name=provider.name,
                    deployment=m.deployment,
                    input_price=m.input_price or Decimal("0"),
                    cached_input_price=m.cached_input_price,
                    output_price=m.output_price or Decimal("0"),
                    cache_ttl_s=m.cache_ttl_s,
                    allowed_groups=GroupRule(m.allowed_groups or []),
                    supports_responses=m.supports_responses,
                    created=int((m.created_at or datetime.now(UTC)).timestamp()),
                )
            self._routes = routes
            self.loaded_at = time.time()

    async def notify_changed(self) -> None:
        """Recarrega aqui e avisa as outras réplicas."""
        await self.reload()
        await self._bus.publish(CONFIG_CHANNEL, "reload")

    # ------------------------------------------------------------------ operações do console
    async def check_provider(self, provider_id: str) -> CheckResult:
        async with sessionmaker()() as session:
            provider = await session.get(Provider, provider_id)
            if provider is None:
                return CheckResult(False, "provedor não encontrado")
            async with self._lock:
                agent = await self._load_agent(session, provider, force=True)
            if agent is None:
                await session.commit()
                return CheckResult(False, provider.last_error or "credencial indisponível")
            result = await agent.check()
            provider.status = "ok" if result.ok else "error"
            provider.last_error = None if result.ok else result.message
            provider.last_checked_at = datetime.now(UTC)
            await session.commit()
        return result

    async def sync_provider(self, provider_id: str, *, actor: str = "system") -> SyncResult:
        """Descobre os deployments do provedor e atualiza a tabela de modelos."""
        async with sessionmaker()() as session:
            provider = await session.get(Provider, provider_id)
            if provider is None:
                raise ValueError("provedor não encontrado")
            async with self._lock:
                agent = await self._load_agent(session, provider, force=True)
            if agent is None:
                await session.commit()
                raise UpstreamError(502, provider.last_error or "credencial indisponível")
            try:
                deployments = await agent.discover()
            except UpstreamError as exc:
                provider.status = "error"
                provider.last_error = exc.message
                provider.last_checked_at = datetime.now(UTC)
                await session.commit()
                raise

            # Serializa sincronizações do mesmo provedor (console × rodada periódica × réplicas).
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:k, :p)"),
                {"k": PROVIDER_LOCK_KEY, "p": _lock_id(provider_id)},
            )
            now = datetime.now(UTC)
            result = SyncResult(found=len(deployments))
            existing = {
                m.deployment: m
                for m in (
                    await session.scalars(
                        select(LlmModel).where(LlmModel.provider_id == provider.id)
                    )
                ).all()
            }
            taken = set((await session.scalars(select(LlmModel.name))).all())
            seen: set[str] = set()
            for dep in deployments:
                if dep.kind not in ("chat", "embeddings"):
                    result.skipped += 1
                    continue
                seen.add(dep.name)
                model = existing.get(dep.name)
                if model is None:
                    name = dep.name
                    if name in taken:
                        name = f"{provider.name}/{dep.name}"
                        n = 2
                        while name in taken:
                            name = f"{provider.name}/{dep.name}-{n}"
                            n += 1
                    taken.add(name)
                    features = guess_features(dep.model_name)
                    session.add(
                        LlmModel(
                            name=name,
                            provider_id=provider.id,
                            deployment=dep.name,
                            kind=dep.kind,
                            upstream_model=dep.model_name,
                            upstream_version=dep.model_version,
                            publisher=dep.publisher,
                            sku=dep.sku,
                            capabilities=dep.capabilities,
                            source="discovered",
                            enabled=self.settings.auto_enable_models,
                            available=True,
                            supports_responses=dep.kind == "chat",
                            supports_vision=features["supports_vision"],
                            supports_reasoning=features["supports_reasoning"],
                            discovered_at=now,
                            last_seen_at=now,
                        )
                    )
                    result.created += 1
                else:
                    model.upstream_model = dep.model_name
                    model.upstream_version = dep.model_version
                    model.publisher = dep.publisher
                    model.sku = dep.sku
                    model.capabilities = dep.capabilities
                    model.available = True
                    model.last_seen_at = now
                    result.updated += 1
            for name, model in existing.items():
                if name not in seen and model.source == "discovered" and model.available:
                    model.available = False
                    result.missing += 1
            provider.last_sync_at = now
            provider.last_checked_at = now
            provider.status = "ok"
            provider.last_error = None
            audit.record(
                session,
                actor=actor,
                actor_type="system" if actor == "system" else "user",
                action="provider.sync",
                resource_type="provider",
                resource_id=provider.id,
                detail=(
                    f"{result.found} encontrado(s), {result.created} novo(s), "
                    f"{result.missing} indisponível(is)"
                ),
            )
            await session.commit()
        await self.notify_changed()
        return result

    async def mark_provider_error(self, provider_id: str, message: str) -> None:
        """Registra falha de credencial vista numa chamada (no máximo 1 escrita/min)."""
        now = time.time()
        if now - self._error_marked.get(provider_id, 0) < 60:
            return
        self._error_marked[provider_id] = now
        with contextlib.suppress(Exception):
            async with sessionmaker()() as session:
                provider = await session.get(Provider, provider_id)
                if provider is not None:
                    provider.status = "error"
                    provider.last_error = message
                    provider.last_checked_at = datetime.now(UTC)
                    await session.commit()

    # ------------------------------------------------------------------ laços de fundo
    async def sync_all(self) -> None:
        """Descoberta periódica; só uma réplica por vez (advisory lock)."""
        async with sessionmaker()() as session:
            got = await session.scalar(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": SYNC_LOCK_KEY}
            )
            if not got:
                return
            try:
                providers = (
                    await session.scalars(select(Provider).where(Provider.enabled.is_(True)))
                ).all()
                ids = [str(p.id) for p in providers]
                for pid in ids:
                    agent = self._agents.get(pid)
                    if agent is None or agent.config.discovery == "none":
                        continue
                    try:
                        await self.sync_provider(pid)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("descoberta do provedor %s falhou: %s", pid, exc)
            finally:
                await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SYNC_LOCK_KEY})
                await session.commit()

    async def run_forever(self) -> None:
        last_sync = 0.0
        first = True
        while True:
            try:
                if first:
                    first = False
                else:
                    await self.reload(refresh_secrets=True)
                if time.time() - last_sync >= self.settings.discovery_interval_s:
                    last_sync = time.time()
                    await self.sync_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("atualização periódica dos provedores falhou: %s", exc)
            await asyncio.sleep(self.settings.provider_refresh_s)

    async def listen(self) -> None:
        """Recarrega quando outra réplica avisa que a configuração mudou."""
        while True:
            try:
                async for _event, _data in self._bus.subscribe(CONFIG_CHANNEL):
                    await self.reload()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("assinatura de avisos caiu (%s); reconectando", exc)
                await asyncio.sleep(5)
