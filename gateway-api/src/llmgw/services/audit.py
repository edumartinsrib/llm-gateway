"""Registro de auditoria: toda escrita administrativa gera uma linha."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from llmgw.models import AuditLog

# Campos que nunca entram no before/after.
_REDACT = {
    "secret",
    "secret_hash",
    "password",
    "api_key",
    "client_secret",
    "token",
    "bind_password",
}


def snapshot(obj: Any, fields: list[str] | None = None) -> dict[str, Any] | None:
    """Fotografia JSON de um modelo SQLAlchemy (ou dict) para o before/after."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        data = dict(obj)
    else:
        cols = fields or [c.key for c in obj.__table__.columns]
        data = {c: getattr(obj, c) for c in cols}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in _REDACT:
            continue
        if isinstance(value, uuid.UUID):
            value = str(value)
        elif isinstance(value, Decimal):
            value = str(value)
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        out[key] = value
    return out


def record(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: Any = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    detail: str | None = None,
    actor_type: str = "user",
) -> None:
    """Adiciona o registro à sessão; é gravado no mesmo commit da alteração."""
    session.add(
        AuditLog(
            actor=actor,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            before=before,
            after=after,
            detail=detail,
        )
    )
