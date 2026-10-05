"""Sincronização com o LDAP (diretório estático): proteções contra desativação em massa."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import asyncpg
import pytest
from conftest import ROOT

from llmgw.db import dispose_engine, init_engine, sessionmaker
from llmgw.models import User
from llmgw.security.directory import StaticDirectory
from llmgw.services import ldap_sync


@pytest.fixture
async def sync_db(migrated_db, settings):
    base, _, name = migrated_db.rpartition("/")
    url = f"{base}/{name}_sync"
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}_sync" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}_sync"')
    finally:
        await conn.close()
    env = {**os.environ, "LLMGW_DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    s = settings.model_copy(
        update={"database_url": url, "auth_mode": "ldap", "ldap_allowed_groups": "llm"}
    )
    init_engine(s)
    yield s
    await dispose_engine()
    init_engine(settings)


async def _users(names, status="active"):
    async with sessionmaker()() as session:
        for n in names:
            session.add(User(username=n, status=status, groups=["llm"], group_dns=[]))
        await session.commit()


async def _status() -> dict[str, str]:
    async with sessionmaker()() as session:
        from sqlalchemy import select

        return {u.username: u.status for u in (await session.scalars(select(User))).all()}


async def test_sync_disables_and_reactivates(sync_db):
    await _users(["ana", "bia", "caio", "duda", "edu", "fabi"])
    directory = StaticDirectory(
        {n: {"groups": ["llm"]} for n in ["ana", "bia", "caio", "duda", "edu"]}
    )
    state = SimpleNamespace(settings=sync_db, directory=directory)
    stats = await ldap_sync.sync_once(state)
    assert stats["disabled"] == 1 and (await _status())["fabi"] == "disabled"
    directory._users["fabi"] = {"groups": ["llm"]}
    stats = await ldap_sync.sync_once(state)
    assert stats["reactivated"] == 1 and (await _status())["fabi"] == "active"


async def test_sync_aborts_when_most_users_vanish(sync_db):
    await _users(["u1", "u2", "u3", "u4", "u5", "u6"])
    # A conta de serviço "perdeu a permissão": quase ninguém é encontrado.
    state = SimpleNamespace(
        settings=sync_db, directory=StaticDirectory({"u1": {"groups": ["llm"]}})
    )
    stats = await ldap_sync.sync_once(state)
    assert stats["disabled"] == 0 and stats["missing"] == 5
    assert set((await _status()).values()) == {"active"}


async def test_sync_never_touches_blocked_users(sync_db):
    await _users(["bloqueado"], status="blocked")
    await _users(["a1", "a2", "a3", "a4", "a5"])
    directory = StaticDirectory(
        {n: {"groups": ["llm"]} for n in ["a1", "a2", "a3", "a4", "a5", "bloqueado"]}
    )
    await ldap_sync.sync_once(SimpleNamespace(settings=sync_db, directory=directory))
    assert (await _status())["bloqueado"] == "blocked"
