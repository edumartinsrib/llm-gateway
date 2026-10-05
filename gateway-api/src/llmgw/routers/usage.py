"""Histórico de chamadas, resumo para o painel e exportação em CSV.

Usuário comum vê só o próprio uso; administrador vê todos (ou filtra por usuário).
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Integer, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmgw import models as m
from llmgw import schemas as s
from llmgw.deps import Session, State, User
from llmgw.errors import ApiError
from llmgw.security.auth import Principal
from llmgw.services.serializers import usage_out
from llmgw.services.usage import GLOBAL_SUBJECT, current_period, user_subject

router = APIRouter(prefix="/api/v1", tags=["uso"])


def _scope_user(principal: Principal, user_id: str | None) -> uuid.UUID | None:
    """None = todos (só administrador)."""
    if not principal.is_admin:
        if user_id and user_id != str(principal.user.id):
            raise ApiError(403, "você só vê o próprio uso")
        return principal.user.id
    if not user_id:
        return None
    try:
        return uuid.UUID(user_id)
    except ValueError as exc:
        raise ApiError(404, "usuário não encontrado") from exc


def _filters(
    user: uuid.UUID | None,
    model: str | None,
    outcome: str | None,
    cache: str | None,
    endpoint: str | None,
    key_id: str | None,
    since: datetime | None,
    until: datetime | None,
    q: str | None,
) -> list[Any]:
    conds: list[Any] = []
    if user is not None:
        conds.append(m.UsageLog.user_id == user)
    if model:
        conds.append(m.UsageLog.model_name == model)
    if outcome == "ok":
        conds.append(m.UsageLog.status_code < 400)
    elif outcome == "error":
        conds.append(m.UsageLog.status_code >= 400)
    if cache == "hit":
        conds.append(m.UsageLog.cache_hit.is_(True))
    elif cache == "miss":
        conds.append(m.UsageLog.cache_hit.is_(False))
    if endpoint:
        conds.append(m.UsageLog.endpoint == endpoint)
    if key_id:
        try:
            conds.append(m.UsageLog.api_key_id == uuid.UUID(key_id))
        except ValueError:
            conds.append(m.UsageLog.id == -1)
    if since:
        conds.append(m.UsageLog.ts >= since)
    if until:
        conds.append(m.UsageLog.ts < until)
    if q:
        like = f"%{q.strip().lower()}%"
        conds.append(
            or_(
                func.lower(m.UsageLog.username).like(like),
                m.UsageLog.request_id == q.strip(),
                func.lower(m.UsageLog.error_code).like(like),
            )
        )
    return conds


@router.get("/usage", response_model=s.Page[s.UsageOut])
async def list_usage(
    session: Session,
    principal: User,
    user_id: str | None = None,
    model: str | None = None,
    outcome: str | None = Query(default=None, pattern="^(ok|error)$"),
    cache: str | None = Query(default=None, pattern="^(hit|miss)$"),
    endpoint: str | None = Query(default=None, pattern="^(chat|embeddings|responses)$"),
    key_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> s.Page[s.UsageOut]:
    user = _scope_user(principal, user_id)
    conds = _filters(user, model, outcome, cache, endpoint, key_id, since, until, q)
    total = await session.scalar(select(func.count()).select_from(m.UsageLog).where(*conds))
    rows = (
        await session.scalars(
            select(m.UsageLog)
            .where(*conds)
            .order_by(m.UsageLog.ts.desc(), m.UsageLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return s.Page(items=[usage_out(r) for r in rows], total=total or 0, limit=limit, offset=offset)


@router.get("/usage/export")
async def export_usage(
    session: Session,
    principal: User,
    user_id: str | None = None,
    model: str | None = None,
    outcome: str | None = Query(default=None, pattern="^(ok|error)$"),
    cache: str | None = Query(default=None, pattern="^(hit|miss)$"),
    endpoint: str | None = Query(default=None, pattern="^(chat|embeddings|responses)$"),
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = None,
) -> StreamingResponse:
    user = _scope_user(principal, user_id)
    conds = _filters(user, model, outcome, cache, endpoint, None, since, until, q)
    rows = (
        await session.scalars(
            select(m.UsageLog).where(*conds).order_by(m.UsageLog.ts.desc()).limit(100_000)
        )
    ).all()
    columns = [
        "ts",
        "request_id",
        "username",
        "api_key_name",
        "model_name",
        "provider_name",
        "deployment",
        "endpoint",
        "stream",
        "status_code",
        "error_code",
        "prompt_tokens",
        "cached_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cost_usd",
        "cache_hit",
        "saved_usd",
        "latency_ms",
        "ttft_ms",
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([_csv_cell(getattr(r, c)) for c in columns])
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="uso-gateway-{stamp}.csv"'},
    )


def _csv_cell(value: Any) -> Any:
    """Evita que um texto controlado pelo usuário (nome da chave) vire fórmula no Excel."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


async def _month(session: AsyncSession, subject: str, period: str, budget: Decimal | None):
    counter = await session.get(m.SpendCounter, (subject, period))
    return s.MonthSpend(
        period=period,
        spent_usd=counter.cost_usd if counter else Decimal("0"),
        budget_usd=budget,
        requests=counter.requests if counter else 0,
        saved_usd=counter.saved_usd if counter else Decimal("0"),
    )


@router.get("/usage/summary", response_model=s.SummaryOut)
async def usage_summary(
    session: Session,
    state: State,
    principal: User,
    days: int = Query(default=30, ge=1, le=366),
    user_id: str | None = None,
    mine: bool = False,
) -> s.SummaryOut:
    user = principal.user.id if mine else _scope_user(principal, user_id)
    scope = "all" if user is None else ("me" if user == principal.user.id else "user")
    since = datetime.now(UTC) - timedelta(days=days)
    conds = [m.UsageLog.ts >= since]
    if user is not None:
        conds.append(m.UsageLog.user_id == user)
    U = m.UsageLog
    t = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(cast(U.status_code >= 400, Integer)), 0),
                func.coalesce(func.sum(U.prompt_tokens), 0),
                func.coalesce(func.sum(U.completion_tokens), 0),
                func.coalesce(func.sum(U.total_tokens), 0),
                func.coalesce(func.sum(U.cost_usd), 0),
                func.coalesce(func.sum(cast(U.cache_hit, Integer)), 0),
                func.coalesce(func.sum(U.saved_usd), 0),
                func.avg(case((U.status_code < 400, U.latency_ms))),
                func.percentile_cont(0.95).within_group(U.latency_ms).filter(U.status_code < 400),
            ).where(*conds)
        )
    ).one()
    totals = s.Totals(
        requests=t[0],
        errors=t[1],
        prompt_tokens=t[2],
        completion_tokens=t[3],
        total_tokens=t[4],
        cost_usd=t[5],
        cache_hits=t[6],
        saved_usd=t[7],
        avg_latency_ms=int(t[8]) if t[8] is not None else None,
        p95_latency_ms=int(t[9]) if t[9] is not None else None,
    )
    tz = state.settings.timezone
    day = func.to_char(func.timezone(tz, U.ts), "YYYY-MM-DD")
    by_day = [
        s.DayPoint(day=d, model=model, requests=n, tokens=tok, cost_usd=cost)
        for d, model, n, tok, cost in (
            await session.execute(
                select(
                    day, U.model_name, func.count(), func.sum(U.total_tokens), func.sum(U.cost_usd)
                )
                .where(*conds, U.status_code < 400)
                .group_by(day, U.model_name)
                .order_by(day)
            )
        ).all()
    ]
    by_model = [
        s.Ranked(
            key=model,
            label=model,
            requests=n,
            tokens=tok or 0,
            cost_usd=cost or 0,
            cache_hits=hits or 0,
        )
        for model, n, tok, cost, hits in (
            await session.execute(
                select(
                    U.model_name,
                    func.count(),
                    func.sum(U.total_tokens),
                    func.sum(U.cost_usd),
                    func.sum(cast(U.cache_hit, Integer)),
                )
                .where(*conds, U.status_code < 400)
                .group_by(U.model_name)
                .order_by(func.sum(U.cost_usd).desc(), func.count().desc())
                .limit(10)
            )
        ).all()
    ]
    by_user: list[s.Ranked] = []
    if scope == "all":
        by_user = [
            s.Ranked(
                key=str(uid),
                label=name,
                requests=n,
                tokens=tok or 0,
                cost_usd=cost or 0,
                cache_hits=hits or 0,
            )
            for uid, name, n, tok, cost, hits in (
                await session.execute(
                    select(
                        U.user_id,
                        U.username,
                        func.count(),
                        func.sum(U.total_tokens),
                        func.sum(U.cost_usd),
                        func.sum(cast(U.cache_hit, Integer)),
                    )
                    .where(*conds, U.status_code < 400)
                    .group_by(U.user_id, U.username)
                    .order_by(func.sum(U.cost_usd).desc(), func.count().desc())
                    .limit(10)
                )
            ).all()
        ]

    period = current_period(tz)
    if user is None:
        month = await _month(
            session, GLOBAL_SUBJECT, period, state.settings.global_monthly_budget_usd
        )
        global_month = month
    else:
        target = principal.user if user == principal.user.id else await session.get(m.User, user)
        budget = target.monthly_budget_usd if target else None
        month = await _month(session, user_subject(user), period, budget)
        global_month = (
            await _month(session, GLOBAL_SUBJECT, period, state.settings.global_monthly_budget_usd)
            if principal.is_admin
            else None
        )
    return s.SummaryOut(
        days=days,
        scope=scope,  # type: ignore[arg-type]
        since=since,
        totals=totals,
        by_day=by_day,
        by_model=by_model,
        by_user=by_user,
        month=month,
        global_month=global_month,
    )
