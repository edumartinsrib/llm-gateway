"""Usuários (só administradores): papel, bloqueio, orçamento, limites e modelos liberados."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmgw import models as m
from llmgw import schemas as s
from llmgw.deps import Admin, Session, State
from llmgw.errors import ApiError, conflict, not_found, unprocessable
from llmgw.security.auth import compute_is_admin
from llmgw.security.directory import DirectoryError
from llmgw.services import audit
from llmgw.services.ldap_sync import apply_directory_result
from llmgw.services.usage import current_period, user_subject
from llmgw.state import AppState

router = APIRouter(prefix="/api/v1", tags=["usuários"])

SNAPSHOT = [
    "username",
    "display_name",
    "role",
    "status",
    "status_reason",
    "monthly_budget_usd",
    "rpm_limit",
    "tpm_limit",
    "allowed_models",
    "groups",
]


async def _user(session: AsyncSession, user_id: str) -> m.User:
    try:
        user = await session.get(m.User, uuid.UUID(user_id))
    except ValueError:
        user = None
    if user is None:
        raise not_found("usuário")
    return user


async def users_out(session: AsyncSession, state: AppState, users: list[m.User]) -> list[s.UserOut]:
    if not users:
        return []
    period = current_period(state.settings.timezone)
    subjects = [user_subject(u.id) for u in users]
    counters = {
        row.subject: row
        for row in (
            await session.scalars(
                select(m.SpendCounter).where(
                    m.SpendCounter.subject.in_(subjects), m.SpendCounter.period == period
                )
            )
        ).all()
    }
    now = datetime.now(UTC)
    keys = dict(
        (
            await session.execute(
                select(m.ApiKey.user_id, func.count())
                .where(
                    m.ApiKey.user_id.in_([u.id for u in users]),
                    m.ApiKey.revoked_at.is_(None),
                    or_(m.ApiKey.expires_at.is_(None), m.ApiKey.expires_at > now),
                )
                .group_by(m.ApiKey.user_id)
            )
        ).all()
    )
    out = []
    for u in users:
        counter = counters.get(user_subject(u.id))
        out.append(
            s.UserOut(
                id=str(u.id),
                username=u.username,
                display_name=u.display_name,
                email=u.email,
                groups=list(u.groups or []),
                role=u.role,  # type: ignore[arg-type]
                is_admin=compute_is_admin(state.settings, u),
                status=u.status,  # type: ignore[arg-type]
                status_reason=u.status_reason,
                monthly_budget_usd=u.monthly_budget_usd,
                spent_month_usd=counter.cost_usd if counter else Decimal("0"),
                requests_month=counter.requests if counter else 0,
                rpm_limit=u.rpm_limit,
                tpm_limit=u.tpm_limit,
                allowed_models=u.allowed_models,
                keys_active=keys.get(u.id, 0),
                created_at=u.created_at,
                last_login_at=u.last_login_at,
                last_seen_at=u.last_seen_at,
                ldap_synced_at=u.ldap_synced_at,
            )
        )
    return out


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


@router.get("/users", response_model=s.Page[s.UserOut])
async def list_users(
    session: Session,
    state: State,
    principal: Admin,
    q: str | None = None,
    status: str | None = None,
    role: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> s.Page[s.UserOut]:
    stmt = select(m.User)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(m.User.username).like(like),
                func.lower(m.User.display_name).like(like),
                func.lower(m.User.email).like(like),
            )
        )
    if status:
        stmt = stmt.where(m.User.status == status)
    if role:
        stmt = stmt.where(m.User.role == role)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    users = (
        await session.scalars(stmt.order_by(m.User.username).limit(limit).offset(offset))
    ).all()
    return s.Page(
        items=await users_out(session, state, list(users)),
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.post("/users", response_model=s.UserOut, status_code=201)
async def create_user(body: s.UserCreate, session: Session, state: State, principal: Admin):
    username = body.username.strip().lower()
    if await session.scalar(select(m.User.id).where(m.User.username == username)):
        raise conflict("usuário já existe")
    user = m.User(
        username=username,
        display_name=body.display_name or username,
        email=body.email,
        role=body.role,
        status="active",
        monthly_budget_usd=(
            body.monthly_budget_usd
            if "monthly_budget_usd" in body.model_fields_set
            else state.settings.default_monthly_budget_usd
        ),
        rpm_limit=body.rpm_limit,
        tpm_limit=body.tpm_limit,
        allowed_models=await _check_models(session, body.allowed_models),
        groups=[],
    )
    session.add(user)
    await session.flush()
    audit.record(
        session,
        actor=principal.username,
        action="user.create",
        resource_type="user",
        resource_id=user.id,
        detail=user.username,
        after=audit.snapshot(user, SNAPSHOT),
    )
    await session.commit()
    return (await users_out(session, state, [user]))[0]


@router.get("/users/{user_id}", response_model=s.UserOut)
async def get_user(user_id: str, session: Session, state: State, principal: Admin) -> s.UserOut:
    return (await users_out(session, state, [await _user(session, user_id)]))[0]


@router.patch("/users/{user_id}", response_model=s.UserOut)
async def update_user(
    user_id: str, body: s.UserUpdate, session: Session, state: State, principal: Admin
) -> s.UserOut:
    user = await _user(session, user_id)
    fields = body.model_fields_set
    is_self = user.id == principal.user.id
    if is_self and body.status == "blocked":
        raise conflict("você não pode bloquear o próprio usuário")
    if (
        is_self
        and body.role == "user"
        and user.role == "admin"
        and not compute_is_admin(
            state.settings, m.User(username=user.username, role="user", groups=user.groups)
        )
    ):
        raise conflict("você não pode remover o próprio papel de administrador")
    before = audit.snapshot(user, SNAPSHOT)
    if "display_name" in fields and body.display_name:
        user.display_name = body.display_name
    if body.role is not None:
        user.role = body.role
    if body.status is not None and body.status != user.status:
        user.status = body.status
        user.status_reason = body.status_reason if body.status == "blocked" else None
        if body.status == "blocked":
            user.session_version += 1  # derruba as sessões abertas no console
    elif "status_reason" in fields and user.status == "blocked":
        user.status_reason = body.status_reason
    if "monthly_budget_usd" in fields:
        user.monthly_budget_usd = body.monthly_budget_usd
    if "rpm_limit" in fields:
        user.rpm_limit = body.rpm_limit
    if "tpm_limit" in fields:
        user.tpm_limit = body.tpm_limit
    if "allowed_models" in fields:
        user.allowed_models = await _check_models(session, body.allowed_models)
    if "groups" in fields and body.groups is not None:
        if state.settings.auth_mode == "ldap":
            raise ApiError(409, "com LDAP os grupos vêm do diretório")
        user.groups = list(dict.fromkeys(g.strip() for g in body.groups if g.strip()))
    audit.record(
        session,
        actor=principal.username,
        action="user.update",
        resource_type="user",
        resource_id=user.id,
        detail=user.username,
        before=before,
        after=audit.snapshot(user, SNAPSHOT),
    )
    await session.commit()
    return (await users_out(session, state, [user]))[0]


@router.post("/users/{user_id}/ldap-sync", response_model=s.UserOut)
async def ldap_sync_user(user_id: str, session: Session, state: State, principal: Admin):
    user = await _user(session, user_id)
    if state.settings.auth_mode != "ldap":
        raise ApiError(409, "o gateway não está em modo LDAP")
    try:
        ident = await state.directory.lookup(user.username, user.external_id)
    except DirectoryError as exc:
        raise ApiError(exc.status if exc.status != 401 else 502, exc.message) from exc
    apply_directory_result(session, state.settings, user, ident, actor=principal.username)
    await session.commit()
    return (await users_out(session, state, [user]))[0]
