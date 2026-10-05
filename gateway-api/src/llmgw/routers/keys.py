"""Chaves de API: cada usuário gera as suas; o administrador vê e revoga todas."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmgw import models as m
from llmgw import schemas as s
from llmgw.deps import Session, State, User
from llmgw.errors import ApiError, conflict, not_found, unprocessable
from llmgw.security.auth import Principal
from llmgw.security.tokens import API_KEY_PREFIX, make_token
from llmgw.services import audit
from llmgw.services.serializers import key_out
from llmgw.services.usage import current_period, key_subject, spent_by_subject

router = APIRouter(prefix="/api/v1", tags=["chaves"])

MAX_ACTIVE_KEYS = 20


def _uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise not_found(what) from exc


async def _target_user(session: AsyncSession, principal: Principal, user_id: str | None) -> m.User:
    if not user_id or user_id == str(principal.user.id):
        return principal.user
    if not principal.is_admin:
        raise ApiError(403, "apenas administradores gerenciam chaves de outros usuários")
    user = await session.get(m.User, _uuid(user_id, "usuário"))
    if user is None:
        raise not_found("usuário")
    return user


async def _load_key(session: AsyncSession, principal: Principal, key_id: str) -> m.ApiKey:
    key = await session.get(m.ApiKey, _uuid(key_id, "chave"))
    if key is None or (key.user_id != principal.user.id and not principal.is_admin):
        raise not_found("chave")
    return key


async def _check_models(session: AsyncSession, names: list[str] | None) -> list[str] | None:
    if names is None:
        return None
    names = list(dict.fromkeys(n.strip() for n in names if n.strip()))
    known = set(
        (await session.scalars(select(m.LlmModel.name).where(m.LlmModel.name.in_(names)))).all()
    )
    unknown = [n for n in names if n not in known]
    if unknown:
        raise unprocessable("modelo(s) inexistente(s)", unknown)
    return names


async def _out(session: AsyncSession, state, keys: list[m.ApiKey]) -> list[s.KeyOut]:
    period = current_period(state.settings.timezone)
    spent = await spent_by_subject(session, [key_subject(k.id) for k in keys], period)
    return [key_out(k, spent.get(key_subject(k.id), Decimal("0"))) for k in keys]


@router.get("/keys", response_model=list[s.KeyOut])
async def list_keys(
    session: Session, state: State, principal: User, user_id: str | None = None
) -> list[s.KeyOut]:
    user = await _target_user(session, principal, user_id)
    keys = (
        await session.scalars(
            select(m.ApiKey).where(m.ApiKey.user_id == user.id).order_by(m.ApiKey.created_at.desc())
        )
    ).all()
    return await _out(session, state, list(keys))


@router.post("/keys", response_model=s.KeyCreated, status_code=201)
async def create_key(
    body: s.KeyCreate, session: Session, state: State, principal: User, user_id: str | None = None
) -> s.KeyCreated:
    user = await _target_user(session, principal, user_id)
    if user.status != "active":
        raise ApiError(409, "o usuário não está ativo")
    active = await session.scalar(
        select(func.count())
        .select_from(m.ApiKey)
        .where(m.ApiKey.user_id == user.id, m.ApiKey.revoked_at.is_(None))
    )
    if (active or 0) >= MAX_ACTIVE_KEYS:
        raise conflict(
            f"limite de {MAX_ACTIVE_KEYS} chaves ativas por usuário; revogue alguma antes"
        )
    key_id = uuid.uuid4()
    token, digest = make_token(API_KEY_PREFIX, key_id)
    key = m.ApiKey(
        id=key_id,
        user_id=user.id,
        name=body.name.strip(),
        secret_hash=digest,
        last4=token[-4:],
        expires_at=(
            datetime.now(UTC) + timedelta(days=body.expires_in_days)
            if body.expires_in_days
            else None
        ),
        monthly_budget_usd=body.monthly_budget_usd,
        rpm_limit=body.rpm_limit,
        allowed_models=await _check_models(session, body.allowed_models),
        # Chave criada por um administrador para outra pessoa: o dono não afrouxa os limites.
        limits_locked=user.id != principal.user.id,
    )
    session.add(key)
    await session.flush()
    audit.record(
        session,
        actor=principal.username,
        action="key.create",
        resource_type="api_key",
        resource_id=key.id,
        detail=f"chave '{key.name}' de {user.username}",
        after={
            "name": key.name,
            "user": user.username,
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            "monthly_budget_usd": str(key.monthly_budget_usd) if key.monthly_budget_usd else None,
            "allowed_models": key.allowed_models,
        },
    )
    await session.commit()
    await session.refresh(key)
    out = (await _out(session, state, [key]))[0]
    return s.KeyCreated(**out.model_dump(), key=token)


@router.patch("/keys/{key_id}", response_model=s.KeyOut)
async def update_key(
    key_id: str, body: s.KeyUpdate, session: Session, state: State, principal: User
) -> s.KeyOut:
    key = await _load_key(session, principal, key_id)
    if key.revoked_at is not None:
        raise conflict("a chave já foi revogada")
    before = audit.snapshot(key, ["name", "monthly_budget_usd", "rpm_limit", "allowed_models"])
    fields = body.model_fields_set
    limit_fields = fields & {"monthly_budget_usd", "rpm_limit", "allowed_models"}
    owner = key.user_id == principal.user.id
    if limit_fields and key.limits_locked and owner and not principal.is_admin:
        raise ApiError(403, "os limites desta chave foram definidos pelo administrador")
    if limit_fields and not owner:
        key.limits_locked = True
    if "name" in fields and body.name:
        key.name = body.name.strip()
    if "monthly_budget_usd" in fields:
        key.monthly_budget_usd = body.monthly_budget_usd
    if "rpm_limit" in fields:
        key.rpm_limit = body.rpm_limit
    if "allowed_models" in fields:
        key.allowed_models = await _check_models(session, body.allowed_models)
    audit.record(
        session,
        actor=principal.username,
        action="key.update",
        resource_type="api_key",
        resource_id=key.id,
        detail=f"chave '{key.name}' de {key.user.username}",
        before=before,
        after=audit.snapshot(key, ["name", "monthly_budget_usd", "rpm_limit", "allowed_models"]),
    )
    await session.commit()
    return (await _out(session, state, [key]))[0]


@router.post("/keys/{key_id}/revoke", response_model=s.KeyOut)
async def revoke_key(key_id: str, session: Session, state: State, principal: User) -> s.KeyOut:
    key = await _load_key(session, principal, key_id)
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        key.revoked_by = principal.username
        audit.record(
            session,
            actor=principal.username,
            action="key.revoke",
            resource_type="api_key",
            resource_id=key.id,
            detail=f"chave '{key.name}' de {key.user.username}",
        )
        await session.commit()
    return (await _out(session, state, [key]))[0]
