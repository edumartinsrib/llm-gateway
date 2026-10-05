"""Sincronização periódica com o LDAP.

A cada `LLMGW_LDAP_SYNC_INTERVAL_S` a conta de serviço consulta cada usuário (pelo DN
guardado no login; sem DN, pelo filtro de usuário):

- sumiu do diretório ou saiu dos grupos com acesso → `disabled` (as chaves param de
  funcionar na hora, sem precisar revogar uma a uma);
- voltou a ter acesso → `active` de novo (só quem foi desativado pela sincronização;
  bloqueio manual do administrador continua valendo);
- grupos, nome e e-mail são atualizados.

Proteções: com o LDAP fora do ar a rodada é interrompida sem mexer em ninguém, e se
uma fração grande dos usuários "some" de uma vez (`LLMGW_LDAP_SYNC_MAX_MISSING_PCT`,
ex.: a conta de serviço perdeu a permissão de leitura) a rodada também é descartada.
Cada usuário é gravado numa transação curta, com a linha travada, para não desfazer
um bloqueio feito pelo administrador durante a rodada.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from llmgw.db import sessionmaker
from llmgw.models import User
from llmgw.security.directory import DirectoryError, DirectoryUser
from llmgw.services import audit
from llmgw.services.groups import intersects
from llmgw.settings import Settings
from llmgw.state import AppState

log = logging.getLogger(__name__)

LOCK_KEY = 7_521_002


def _has_access(settings: Settings, user: User, ident: DirectoryUser) -> bool:
    if user.username in settings.bootstrap_admin_list:
        return True
    allowed = settings.ldap_allowed_group_list
    if not allowed:
        return True
    return intersects(ident.groups, allowed, ident.group_dns) or intersects(
        ident.groups, settings.ldap_admin_group_list, ident.group_dns
    )


def apply_directory_result(
    session: AsyncSession,
    settings: Settings,
    user: User,
    ident: DirectoryUser | None,
    *,
    actor: str = "system",
) -> str:
    """Aplica o resultado da consulta ao LDAP; devolve o que aconteceu."""
    now = datetime.now(UTC)
    user.ldap_synced_at = now
    actor_type = "system" if actor == "system" else "user"
    if ident is None or not _has_access(settings, user, ident):
        reason = (
            "não encontrado no LDAP"
            if ident is None
            else "fora dos grupos com acesso ao gateway no LDAP"
        )
        if ident is not None:
            user.groups = list(ident.groups)
            user.group_dns = list(ident.group_dns)
        if user.status == "active":
            user.status = "disabled"
            user.status_reason = reason
            user.session_version += 1
            audit.record(
                session,
                actor=actor,
                actor_type=actor_type,
                action="user.disabled",
                resource_type="user",
                resource_id=user.id,
                detail=f"{user.username}: {reason}",
            )
            return "disabled"
        return "unchanged"
    user.groups = list(ident.groups)
    user.group_dns = list(ident.group_dns)
    if ident.display_name:
        user.display_name = ident.display_name
    if ident.email:
        user.email = ident.email
    if ident.dn:
        user.external_id = ident.dn
    if user.status == "disabled":
        user.status = "active"
        user.status_reason = None
        audit.record(
            session,
            actor=actor,
            actor_type=actor_type,
            action="user.reactivated",
            resource_type="user",
            resource_id=user.id,
            detail=f"{user.username} voltou a ter acesso no LDAP",
        )
        return "reactivated"
    return "updated"


async def sync_once(state: AppState) -> dict[str, int]:
    stats = {"checked": 0, "disabled": 0, "reactivated": 0, "missing": 0}
    directory = state.directory
    settings = state.settings
    if settings.auth_mode != "ldap" or not directory.can_lookup:
        return stats
    async with sessionmaker()() as lock_session:
        got = await lock_session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY})
        if not got:
            return stats
        try:
            await lock_session.commit()
            async with sessionmaker()() as session:
                users = (
                    await session.execute(
                        select(User.id, User.username, User.external_id).where(
                            User.status.in_(["active", "disabled"])
                        )
                    )
                ).all()

            # 1) Consultas ao LDAP, sem transação aberta no banco.
            results: list[tuple] = []
            for user_id, username, dn in users:
                try:
                    ident = await directory.lookup(username, dn)
                except DirectoryError as exc:
                    log.warning("sincronização com o LDAP interrompida: %s", exc.message)
                    return stats
                results.append((user_id, ident))
            missing = sum(1 for _, ident in results if ident is None)
            stats["missing"] = missing
            limit = settings.ldap_sync_max_missing_pct
            if len(results) >= 5 and missing * 100 > len(results) * limit:
                log.error(
                    "sincronização com o LDAP descartada: %d de %d usuários não foram "
                    "encontrados (limite de %d%%). Confira a conta de serviço e a base de busca.",
                    missing,
                    len(results),
                    limit,
                )
                return stats

            # 2) Uma transação curta por usuário, com a linha travada.
            for user_id, ident in results:
                async with sessionmaker()() as session:
                    user = await session.scalar(
                        select(User).where(User.id == user_id).with_for_update()
                    )
                    if user is None or user.status == "blocked":
                        continue
                    result = apply_directory_result(session, settings, user, ident)
                    await session.commit()
                stats["checked"] += 1
                if result in ("disabled", "reactivated"):
                    stats[result] += 1
        finally:
            await lock_session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            await lock_session.commit()
    if stats["disabled"] or stats["reactivated"]:
        log.info("sincronização com o LDAP: %s", stats)
    return stats


async def run_forever(state: AppState) -> None:
    while True:
        await asyncio.sleep(state.settings.ldap_sync_interval_s)
        try:
            await sync_once(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("sincronização com o LDAP falhou: %s", exc)
