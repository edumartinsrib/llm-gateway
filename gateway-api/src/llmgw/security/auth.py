"""Autenticação: sessão do console (JWT) e chave de API nas rotas `/v1`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from llmgw.db import get_session
from llmgw.errors import ApiError, OpenAIError
from llmgw.models import ApiKey, User
from llmgw.security.tokens import API_KEY_PREFIX, parse_token, verify_secret
from llmgw.services.groups import intersects
from llmgw.settings import Settings
from llmgw.state import AppState, get_state

STATUS_MESSAGES = {
    "blocked": "usuário bloqueado pelo administrador do gateway",
    "disabled": "usuário sem acesso ao gateway no LDAP",
}


@dataclass
class Principal:
    user: User
    is_admin: bool
    api_key: ApiKey | None = None

    @property
    def username(self) -> str:
        return self.user.username


def compute_is_admin(settings: Settings, user: User) -> bool:
    return (
        user.role == "admin"
        or user.username.lower() in settings.bootstrap_admin_list
        or intersects(user.groups or [], settings.ldap_admin_group_list, user.group_dns or [])
    )


def _touch(user: User, key: ApiKey | None) -> bool:
    """Atualiza `last_seen_at`/`last_used_at` no máximo a cada 5 minutos."""
    now = datetime.now(UTC)
    changed = False
    if user.last_seen_at is None or now - user.last_seen_at > timedelta(minutes=5):
        user.last_seen_at = now
        changed = True
    if key is not None and (
        key.last_used_at is None or now - key.last_used_at > timedelta(minutes=5)
    ):
        key.last_used_at = now
        changed = True
    return changed


# --------------------------------------------------------------------------- console
async def get_principal(
    request: Request,
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
    authorization: str | None = Header(default=None),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "sessão ausente: entre no console")
    try:
        claims = state.sessions.verify(authorization[7:].strip())
    except jwt.PyJWTError as exc:
        raise ApiError(401, "sessão expirada ou inválida; entre novamente") from exc
    user = await session.get(User, claims.user_id)
    if user is None or user.session_version != claims.session_version:
        raise ApiError(401, "sessão encerrada; entre novamente")
    if user.status != "active":
        raise ApiError(403, STATUS_MESSAGES.get(user.status, "usuário sem acesso"))
    _touch(user, None)
    await session.commit()
    principal = Principal(user=user, is_admin=compute_is_admin(state.settings, user))
    request.state.principal = principal
    return principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise ApiError(403, "apenas administradores do gateway")
    return principal


# --------------------------------------------------------------------------- /v1 (chave de API)
def _extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("api-key") or request.headers.get("x-api-key")


async def api_key_principal(
    request: Request,
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> Principal:
    raw = _extract_key(request)
    if not raw:
        raise OpenAIError(
            401,
            "chave de API ausente: envie 'Authorization: Bearer <chave>'",
            type="invalid_request_error",
            code="invalid_api_key",
        )
    parsed = parse_token(raw, API_KEY_PREFIX)
    key = await session.get(ApiKey, parsed[0]) if parsed else None
    if key is None or parsed is None or not verify_secret(parsed[1], key.secret_hash):
        raise OpenAIError(401, "chave de API inválida", code="invalid_api_key")
    if key.revoked_at is not None:
        raise OpenAIError(401, "chave de API revogada", code="invalid_api_key")
    if key.expires_at is not None and key.expires_at <= datetime.now(UTC):
        raise OpenAIError(401, "chave de API expirada", code="invalid_api_key")
    user = key.user
    if user.status != "active":
        raise OpenAIError(
            403, STATUS_MESSAGES.get(user.status, "usuário sem acesso"), code="user_blocked"
        )
    _touch(user, key)
    # Fecha a transação já aqui: a conexão volta ao pool enquanto o corpo da
    # requisição ainda está chegando (uploads lentos não seguram o banco).
    await session.commit()
    principal = Principal(user=user, is_admin=compute_is_admin(state.settings, user), api_key=key)
    request.state.principal = principal
    return principal
