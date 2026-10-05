"""Login do console (LDAP ou modo dev) e dados do usuário atual."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmgw import models as m
from llmgw import schemas as s
from llmgw.deps import Session, State, User
from llmgw.errors import ApiError
from llmgw.security.auth import STATUS_MESSAGES, compute_is_admin
from llmgw.security.directory import DirectoryError, DirectoryUser
from llmgw.services import audit
from llmgw.services.groups import intersects
from llmgw.services.usage import current_period, spent_by_subject, user_subject
from llmgw.settings import Settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["sessão"])


async def me_out(session: AsyncSession, settings: Settings, user: m.User) -> s.MeOut:
    period = current_period(settings.timezone)
    spent = await spent_by_subject(session, [user_subject(user.id)], period)
    return s.MeOut(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        groups=list(user.groups or []),
        role=user.role,  # type: ignore[arg-type]
        is_admin=compute_is_admin(settings, user),
        status=user.status,  # type: ignore[arg-type]
        monthly_budget_usd=user.monthly_budget_usd,
        spent_month_usd=spent.get(user_subject(user.id), Decimal("0")),
        period=period,
        rpm_limit=user.rpm_limit,
        tpm_limit=user.tpm_limit,
        allowed_models=user.allowed_models,
        last_login_at=user.last_login_at,
    )


def has_gateway_access(settings: Settings, ident: DirectoryUser) -> bool:
    allowed = settings.ldap_allowed_group_list
    if not allowed or settings.auth_mode != "ldap":
        return True
    if ident.username.lower() in settings.bootstrap_admin_list:
        return True
    return intersects(ident.groups, allowed, ident.group_dns) or intersects(
        ident.groups, settings.ldap_admin_group_list, ident.group_dns
    )


async def upsert_user(
    session: AsyncSession, settings: Settings, ident: DirectoryUser, *, login: bool
) -> tuple[m.User, bool]:
    user = await session.scalar(select(m.User).where(m.User.username == ident.username))
    now = datetime.now(UTC)
    created = False
    if user is None:
        user = m.User(
            username=ident.username,
            display_name=ident.display_name or ident.username,
            email=ident.email,
            external_id=ident.dn,
            groups=list(ident.groups),
            group_dns=list(ident.group_dns),
            role="user",
            status="active",
            monthly_budget_usd=settings.default_monthly_budget_usd,
        )
        session.add(user)
        created = True
    else:
        if ident.display_name:
            user.display_name = ident.display_name
        if ident.email:
            user.email = ident.email
        if ident.dn:
            user.external_id = ident.dn
        # No modo dev os grupos são editados no console; no LDAP vêm do diretório.
        if settings.auth_mode == "ldap" or ident.groups:
            user.groups = list(ident.groups)
            user.group_dns = list(ident.group_dns)
    if login:
        user.last_login_at = now
    if settings.auth_mode == "ldap":
        user.ldap_synced_at = now
    return user, created


@router.post("/auth/login", response_model=s.LoginOut)
async def login(body: s.LoginIn, request: Request, session: Session, state: State) -> s.LoginOut:
    settings = state.settings
    ip = request.client.host if request.client else "?"
    name = body.username.strip().lower()
    attempts = f"login:{name}:{ip}"
    # Também por usuário, de qualquer IP: protege contra o bloqueio da conta no AD.
    per_user = f"login-user:{name}"
    failures, _ = await state.limiter.used(attempts, window_s=300)
    failures_user, _ = await state.limiter.used(per_user, window_s=300)
    if failures >= settings.login_max_attempts or failures_user >= settings.login_max_attempts * 3:
        raise ApiError(429, "muitas tentativas de login; aguarde alguns minutos")
    try:
        ident = await state.directory.authenticate(body.username, body.password)
    except DirectoryError as exc:
        if exc.status == 401:
            await state.limiter.add(attempts, 1, window_s=300)
            await state.limiter.add(per_user, 1, window_s=300)
            log.info("login recusado para %s a partir de %s", body.username.strip().lower(), ip)
        raise ApiError(exc.status, exc.message) from exc
    if not has_gateway_access(settings, ident):
        raise ApiError(403, "seu usuário não está em nenhum grupo com acesso ao gateway")

    user, created = await upsert_user(session, settings, ident, login=True)
    if user.status == "blocked":
        await session.rollback()
        raise ApiError(403, STATUS_MESSAGES["blocked"])
    if user.status == "disabled":
        user.status = "active"
        user.status_reason = None
        audit.record(
            session,
            actor=user.username,
            action="user.reactivated",
            resource_type="user",
            resource_id=user.id,
            detail="voltou a ter acesso no LDAP",
        )
    await session.flush()
    audit.record(
        session,
        actor=user.username,
        action="auth.login",
        resource_type="user",
        resource_id=user.id,
        detail=("primeiro acesso" if created else None),
    )
    await session.commit()
    token, expires = state.sessions.issue(
        user.id, user.username, user.session_version, timedelta(minutes=settings.session_ttl_min)
    )
    return s.LoginOut(token=token, expires_at=expires, user=await me_out(session, settings, user))


@router.post("/auth/logout", status_code=204)
async def logout(session: Session, principal: User) -> None:
    """Encerra as sessões do usuário no console (todas as abas e navegadores)."""
    principal.user.session_version += 1
    await session.commit()


@router.get("/me", response_model=s.MeOut)
async def me(session: Session, state: State, principal: User) -> s.MeOut:
    return await me_out(session, state.settings, principal.user)
