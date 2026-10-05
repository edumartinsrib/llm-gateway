"""Limpeza diária do histórico de uso (`LLMGW_USAGE_RETENTION_DAYS`).

Os contadores mensais (`spend_counters`) e a auditoria não são apagados.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, text

from llmgw.db import sessionmaker
from llmgw.models import UsageLog
from llmgw.settings import Settings

log = logging.getLogger(__name__)

LOCK_KEY = 7_521_003


async def purge_once(settings: Settings) -> int:
    if settings.usage_retention_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=settings.usage_retention_days)
    async with sessionmaker()() as session:
        got = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        if not got:
            return 0
        result = await session.execute(delete(UsageLog).where(UsageLog.ts < cutoff))
        await session.commit()
    removed = result.rowcount or 0
    if removed:
        log.info(
            "histórico de uso: %d linha(s) anteriores a %s removida(s)", removed, cutoff.date()
        )
    return removed


async def run_forever(settings: Settings) -> None:
    while True:
        try:
            await purge_once(settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("limpeza do histórico de uso falhou: %s", exc)
        await asyncio.sleep(86400)
