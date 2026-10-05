"""Provedores (recursos do Microsoft Foundry). A credencial vive só no Vault.

Duas formas de cadastrar:

- **credencial no console**: o administrador preenche endpoint e chave/service principal;
  o gateway grava em `<prefixo>/providers/<nome>` no Vault (`managed_secret=true`);
- **credencial provisionada por fora**: a plataforma grava o segredo no Vault e o
  administrador só informa o caminho (`managed_secret=false`). O mesmo vale para
  `LLMGW_BOOTSTRAP_PROVIDERS`, que cadastra os provedores na subida.
"""

from __future__ import annotations

import posixpath
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmgw import models as m
from llmgw import schemas as s
from llmgw.deps import Admin, Session, State
from llmgw.errors import ApiError, conflict, not_found, unprocessable
from llmgw.services import audit
from llmgw.services.foundry import FoundryConfig, ProviderConfigError, UpstreamError
from llmgw.services.secrets import provider_path
from llmgw.settings import Settings
from llmgw.state import AppState

router = APIRouter(prefix="/api/v1", tags=["provedores"])

SENSITIVE = ("api_key", "client_secret")
# Campos que dizem para onde a credencial é enviada: se mudarem, os segredos têm de
# ser digitados de novo (senão um administrador poderia mandar a chave guardada para
# um endereço dele e lê-la).
DESTINATION = (
    "endpoint",
    "project_endpoint",
    "authority",
    "arm_endpoint",
    "tenant_id",
    "client_id",
)
URL_FIELDS = ("endpoint", "project_endpoint", "authority", "arm_endpoint")
# Ajustes avançados que o formulário não mostra: mantidos do segredo atual.
ADVANCED = ("authority", "arm_endpoint", "scope", "api_version")
VAULT_PATH = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")
SNAPSHOT = ["name", "display_name", "vault_path", "managed_secret", "enabled"]


async def _provider(session: AsyncSession, provider_id: str) -> m.Provider:
    try:
        provider = await session.get(m.Provider, uuid.UUID(provider_id))
    except ValueError:
        provider = None
    if provider is None:
        raise not_found("provedor")
    return provider


def _check_url(settings: Settings, field: str, value: str) -> None:
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" and not (settings.provider_allow_http and parts.scheme == "http"):
        raise unprocessable(f"{field}: use https://")
    allowed = settings.provider_allowed_host_list
    if "*" in allowed:
        return
    if not host or not any(
        host == h.lstrip(".") or host.endswith(h if h.startswith(".") else "." + h) for h in allowed
    ):
        raise unprocessable(
            f"{field}: o host {host or '?'} não está em LLMGW_PROVIDER_ALLOWED_HOSTS"
        )


def _secret_payload(
    settings: Settings, body: s.ProviderSecretIn, existing: dict[str, Any] | None
) -> dict[str, Any]:
    data = {
        k: v
        for k, v in body.model_dump(exclude={"keep_existing_secrets"}).items()
        if v not in (None, "", [])
    }
    for field in URL_FIELDS:
        if data.get(field):
            _check_url(settings, field, data[field])
    if existing:
        for field in ADVANCED:
            if field not in data and existing.get(field):
                data[field] = existing[field]
    if body.keep_existing_secrets and existing:
        missing = [f for f in SENSITIVE if f not in data and existing.get(f)]
        if missing:
            changed = [f for f in DESTINATION if (data.get(f) or None) != (existing.get(f) or None)]
            if changed:
                raise unprocessable(
                    "o destino da credencial mudou ("
                    + ", ".join(changed)
                    + "): digite de novo a chave de API e/ou o client secret"
                )
            for field in missing:
                data[field] = existing[field]
    sp_fields = ("tenant_id", "client_id", "client_secret")
    if body.auth == "api_key" and not all(data.get(f) for f in sp_fields):
        # Com chave de API, o service principal só serve para a descoberta e vai completo.
        for field in sp_fields:
            data.pop(field, None)
    try:
        FoundryConfig.from_secret(data)
    except ProviderConfigError as exc:
        raise unprocessable(f"credencial incompleta: {exc}") from exc
    return data


def _external_path(settings: Settings, raw: str) -> str:
    """Valida o caminho de um segredo provisionado por fora (sem `..`, só prefixos liberados)."""
    path = raw.strip()
    if not VAULT_PATH.match(path) or ".." in path.split("/") or posixpath.normpath(path) != path:
        raise unprocessable("caminho do Vault inválido (use algo como llmgw/providers/foundry)")
    if path.startswith(f"{settings.vault_prefix}/_system"):
        raise unprocessable("esse caminho do Vault é reservado ao gateway")
    if not any(path.startswith(prefix) for prefix in settings.provider_vault_prefix_list):
        raise unprocessable(
            "caminho fora dos prefixos liberados: "
            + ", ".join(settings.provider_vault_prefix_list)
            + " (ajuste LLMGW_PROVIDER_VAULT_PREFIXES)"
        )
    return path


async def provider_out(
    session: AsyncSession, state: AppState, provider: m.Provider
) -> s.ProviderOut:
    total, enabled = (
        await session.execute(
            select(func.count(), func.count().filter(m.LlmModel.enabled.is_(True))).where(
                m.LlmModel.provider_id == provider.id
            )
        )
    ).one()
    agent = state.gateway.agent(str(provider.id))
    return s.ProviderOut(
        id=str(provider.id),
        name=provider.name,
        display_name=provider.display_name,
        kind=provider.kind,
        vault_path=provider.vault_path,
        managed_secret=provider.managed_secret,
        enabled=provider.enabled,
        endpoint=provider.endpoint,
        auth_type=provider.auth_type,
        discovery=provider.discovery,
        status=provider.status,
        last_error=provider.last_error,
        last_checked_at=provider.last_checked_at,
        last_sync_at=provider.last_sync_at,
        secret_loaded_at=provider.secret_loaded_at,
        agent_loaded=agent is not None,
        models_total=total or 0,
        models_enabled=enabled or 0,
        config=agent.config.public_view() if agent else None,
        created_at=provider.created_at,
        created_by=provider.created_by,
    )


@router.get("/providers", response_model=list[s.ProviderOut])
async def list_providers(session: Session, state: State, principal: Admin) -> list[s.ProviderOut]:
    rows = (await session.scalars(select(m.Provider).order_by(m.Provider.name))).all()
    return [await provider_out(session, state, p) for p in rows]


@router.get("/providers/{provider_id}", response_model=s.ProviderOut)
async def get_provider(provider_id: str, session: Session, state: State, principal: Admin):
    return await provider_out(session, state, await _provider(session, provider_id))


@router.post("/providers", response_model=s.ProviderOut, status_code=201)
async def create_provider(body: s.ProviderCreate, session: Session, state: State, principal: Admin):
    if await session.scalar(select(m.Provider.id).where(m.Provider.name == body.name)):
        raise conflict(f"já existe um provedor chamado '{body.name}'")
    if (body.secret is None) == (not body.vault_path):
        raise unprocessable(
            "informe a credencial (gravada pelo gateway) ou o caminho de um segredo "
            "já existente no Vault, não os dois"
        )
    managed = body.secret is not None
    if body.secret is not None:
        # Credencial digitada no console vai sempre para o caminho do próprio provedor.
        path = provider_path(state.settings, body.name)
        if await state.secrets.read(path):
            raise conflict(f"já existe um segredo em '{path}' no Vault; use outro nome")
        await state.secrets.write(path, _secret_payload(state.settings, body.secret, None))
    else:
        path = _external_path(state.settings, body.vault_path or "")
        try:
            FoundryConfig.from_secret(await state.secrets.read(path))
        except ProviderConfigError as exc:
            raise unprocessable(f"segredo em '{path}': {exc}") from exc
    provider = m.Provider(
        name=body.name,
        display_name=body.display_name,
        vault_path=path,
        managed_secret=managed,
        created_by=principal.username,
    )
    session.add(provider)
    await session.flush()
    audit.record(
        session,
        actor=principal.username,
        action="provider.create",
        resource_type="provider",
        resource_id=provider.id,
        detail=f"{provider.name} ({path})",
        after=audit.snapshot(provider, SNAPSHOT),
    )
    await session.commit()
    await state.gateway.check_provider(str(provider.id))
    await state.gateway.notify_changed()
    await session.refresh(provider)
    return await provider_out(session, state, provider)


@router.patch("/providers/{provider_id}", response_model=s.ProviderOut)
async def update_provider(
    provider_id: str, body: s.ProviderUpdate, session: Session, state: State, principal: Admin
):
    provider = await _provider(session, provider_id)
    before = audit.snapshot(provider, SNAPSHOT)
    if "display_name" in body.model_fields_set:
        provider.display_name = body.display_name
    if body.enabled is not None:
        provider.enabled = body.enabled
    audit.record(
        session,
        actor=principal.username,
        action="provider.update",
        resource_type="provider",
        resource_id=provider.id,
        detail=provider.name,
        before=before,
        after=audit.snapshot(provider, SNAPSHOT),
    )
    await session.commit()
    await state.gateway.notify_changed()
    await session.refresh(provider)
    return await provider_out(session, state, provider)


@router.put("/providers/{provider_id}/secret", response_model=s.ProviderOut)
async def update_secret(
    provider_id: str, body: s.ProviderSecretIn, session: Session, state: State, principal: Admin
):
    provider = await _provider(session, provider_id)
    if not provider.managed_secret or provider.vault_path != provider_path(
        state.settings, provider.name
    ):
        raise ApiError(
            409,
            f"a credencial deste provedor é mantida fora do gateway; atualize-a no Vault "
            f"em '{provider.vault_path}' (o gateway relê em até "
            f"{state.settings.provider_refresh_s} s)",
        )
    existing = await state.secrets.read(provider.vault_path)
    await state.secrets.write(provider.vault_path, _secret_payload(state.settings, body, existing))
    audit.record(
        session,
        actor=principal.username,
        action="provider.secret_update",
        resource_type="provider",
        resource_id=provider.id,
        detail=f"{provider.name} ({provider.vault_path})",
        after={"endpoint": body.endpoint, "auth": body.auth, "discovery": body.discovery},
    )
    await session.commit()
    await state.gateway.check_provider(str(provider.id))
    await state.gateway.notify_changed()
    await session.refresh(provider)
    return await provider_out(session, state, provider)


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str, session: Session, state: State, principal: Admin
) -> None:
    provider = await _provider(session, provider_id)
    audit.record(
        session,
        actor=principal.username,
        action="provider.delete",
        resource_type="provider",
        resource_id=provider.id,
        detail=provider.name,
        before=audit.snapshot(provider, SNAPSHOT),
    )
    path = provider.vault_path
    owned = provider.managed_secret and path == provider_path(state.settings, provider.name)
    await session.delete(provider)
    await session.commit()
    if owned:
        await state.secrets.delete(path)
    await state.gateway.notify_changed()


@router.post("/providers/{provider_id}/check", response_model=s.CheckOut)
async def check_provider(provider_id: str, session: Session, state: State, principal: Admin):
    provider = await _provider(session, provider_id)
    result = await state.gateway.check_provider(str(provider.id))
    return s.CheckOut(ok=result.ok, message=result.message, deployments=result.deployments)


@router.post("/providers/{provider_id}/sync", response_model=s.SyncOut)
async def sync_provider(provider_id: str, session: Session, state: State, principal: Admin):
    provider = await _provider(session, provider_id)
    if not provider.enabled:
        raise ApiError(409, "o provedor está desabilitado")
    try:
        result = await state.gateway.sync_provider(str(provider.id), actor=principal.username)
    except UpstreamError as exc:
        raise ApiError(502, exc.message) from exc
    return s.SyncOut(**result.__dict__)
