"""Auditoria (só administradores)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from llmgw import models as m
from llmgw import schemas as s
from llmgw.deps import Admin, Session

router = APIRouter(prefix="/api/v1", tags=["auditoria"])


@router.get("/audit", response_model=s.Page[s.AuditOut])
async def list_audit(
    session: Session,
    principal: Admin,
    action: str | None = None,
    actor: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> s.Page[s.AuditOut]:
    A = m.AuditLog
    conds = []
    if action:
        conds.append(A.action.like(f"{action}%"))
    if actor:
        conds.append(A.actor == actor)
    if resource_type:
        conds.append(A.resource_type == resource_type)
    if resource_id:
        conds.append(A.resource_id == resource_id)
    if since:
        conds.append(A.ts >= since)
    if until:
        conds.append(A.ts < until)
    if q:
        like = f"%{q.strip().lower()}%"
        conds.append(or_(func.lower(A.detail).like(like), func.lower(A.actor).like(like)))
    total = await session.scalar(select(func.count()).select_from(A).where(*conds))
    rows = (
        await session.scalars(
            select(A).where(*conds).order_by(A.ts.desc(), A.id.desc()).limit(limit).offset(offset)
        )
    ).all()
    return s.Page(
        items=[s.AuditOut.model_validate(r) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )
