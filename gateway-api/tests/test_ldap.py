"""Login e sincronização contra um OpenLDAP real (o mesmo de dev/ldap).

Roda quando `LLMGW_TEST_LDAP_URL` aponta para o OpenLDAP de desenvolvimento
(ex.: `ldap://localhost:3389`, com os dados de dev/ldap/seed.ldif); senão é pulado.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import asyncpg
import httpx
import pytest
from asgi_lifespan import LifespanManager
from conftest import ROOT

from llmgw.db import init_engine
from llmgw.main import build_state, create_app
from llmgw.security.directory import DirectoryError, LdapDirectory
from llmgw.services import ldap_sync
from llmgw.services.secrets import MemorySecretStore

LDAP_URL = os.environ.get("LLMGW_TEST_LDAP_URL")
pytestmark = pytest.mark.skipif(not LDAP_URL, reason="LLMGW_TEST_LDAP_URL não definido")

BASE = "dc=exemplo,dc=org"
SERVICE = dict(
    ldap_url=LDAP_URL,
    ldap_bind_dn=f"cn=leitor-gateway,ou=servicos,{BASE}",
    ldap_bind_password="leitor-senha",
    ldap_user_base=f"ou=pessoas,{BASE}",
    ldap_group_base=f"ou=grupos,{BASE}",
    ldap_allowed_groups="llm-usuarios",
    ldap_admin_groups=f"cn=llm-admins,ou=grupos,{BASE}",
)


def ldap_settings(settings, **overrides):
    return settings.model_copy(update={"auth_mode": "ldap", **SERVICE, **overrides})


async def test_authenticate_with_service_account(settings):
    directory = LdapDirectory(ldap_settings(settings), "leitor-senha")
    user = await directory.authenticate("Bruno", "Senha@123")
    assert user.username == "bruno"
    assert user.display_name == "Bruno Lima" and user.email == "bruno@exemplo.org"
    assert set(user.groups) == {"llm-usuarios", "llm-admins"}  # via memberOf
    assert user.dn == f"uid=bruno,ou=pessoas,{BASE}"


@pytest.mark.parametrize(
    ("username", "password"),
    [("ana", "errada"), ("ninguem", "Senha@123"), ("ana", ""), ("ana", None), ("*", "Senha@123")],
)
async def test_authenticate_rejects(settings, username, password):
    directory = LdapDirectory(ldap_settings(settings), "leitor-senha")
    with pytest.raises(DirectoryError) as exc:
        await directory.authenticate(username, password)
    assert exc.value.status == 401


async def test_filter_injection_is_escaped(settings):
    directory = LdapDirectory(ldap_settings(settings), "leitor-senha")
    with pytest.raises(DirectoryError):
        await directory.authenticate("ana)(uid=*", "Senha@123")


async def test_group_search_mode(settings):
    directory = LdapDirectory(ldap_settings(settings, ldap_group_mode="search"), "leitor-senha")
    user = await directory.authenticate("diego", "Senha@123")
    assert set(user.groups) == {"llm-usuarios", "dados"}


async def test_direct_bind_without_service_account(settings):
    s = ldap_settings(
        settings,
        ldap_bind_dn=None,
        ldap_bind_password=None,
        ldap_user_dn_template=f"uid={{username}},ou=pessoas,{BASE}",
    )
    directory = LdapDirectory(s, None)
    assert directory.can_lookup is False
    user = await directory.authenticate("ana", "Senha@123")
    assert user.username == "ana" and "llm-usuarios" in user.groups
    with pytest.raises(DirectoryError):
        await directory.authenticate("ana", "errada")


async def test_wrong_service_password_is_unavailable(settings):
    directory = LdapDirectory(ldap_settings(settings), "errada")
    with pytest.raises(DirectoryError) as exc:
        await directory.authenticate("ana", "Senha@123")
    assert exc.value.status == 503


async def test_unreachable_server(settings):
    directory = LdapDirectory(
        ldap_settings(settings, ldap_url="ldap://127.0.0.1:1", ldap_timeout_s=2), "leitor-senha"
    )
    with pytest.raises(DirectoryError) as exc:
        await directory.authenticate("ana", "Senha@123")
    assert exc.value.status == 503


def _ldap_admin():
    from ldap3 import Connection, Server

    return Connection(Server(LDAP_URL), f"cn=admin,{BASE}", "admin", auto_bind=True)


@pytest.fixture
async def ldap_db(migrated_db, settings):
    """Banco separado: a sincronização desativa quem não está no LDAP (todos os outros testes)."""
    base, _, name = migrated_db.rpartition("/")
    url = f"{base}/{name}_ldap"
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}_ldap" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}_ldap"')
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
    yield url
    init_engine(settings)  # devolve o engine global ao banco dos outros testes


async def test_console_login_and_periodic_sync(settings, ldap_db):
    """Login pelo console com LDAP; quem sai do grupo é desativado na sincronização."""
    from ldap3 import MODIFY_ADD, MODIFY_DELETE

    s = ldap_settings(settings, bootstrap_admins="", bootstrap_providers="", database_url=ldap_db)
    state = await build_state(s, secrets_store=MemorySecretStore())
    app = create_app(s, state=state)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            denied = await c.post(
                "/api/v1/auth/login", json={"username": "carla", "password": "Senha@123"}
            )
            assert denied.status_code == 403  # fora de llm-usuarios
            wrong = await c.post("/api/v1/auth/login", json={"username": "ana", "password": "x"})
            assert wrong.status_code == 401
            ok = await c.post(
                "/api/v1/auth/login", json={"username": "ana", "password": "Senha@123"}
            )
            assert ok.status_code == 200, ok.text
            assert ok.json()["user"]["is_admin"] is False
            token = ok.json()["token"]
            admin = await c.post(
                "/api/v1/auth/login", json={"username": "bruno", "password": "Senha@123"}
            )
            assert admin.json()["user"]["is_admin"] is True  # grupo llm-admins
            headers = {"Authorization": f"Bearer {token}"}
            key = (await c.post("/api/v1/keys", json={"name": "ldap"}, headers=headers)).json()[
                "key"
            ]
            assert (
                await c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
            ).status_code == 200

            group = f"cn=llm-usuarios,ou=grupos,{BASE}"
            member = f"uid=ana,ou=pessoas,{BASE}"
            conn = _ldap_admin()
            conn.modify(group, {"member": [(MODIFY_DELETE, [member])]})
            try:
                stats = await ldap_sync.sync_once(state)
                assert stats["disabled"] >= 1
                r = await c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
                assert r.status_code == 403 and r.json()["error"]["code"] == "user_blocked"
                assert (await c.get("/api/v1/me", headers=headers)).status_code == 401
            finally:
                conn.modify(group, {"member": [(MODIFY_ADD, [member])]})
                conn.unbind()
            stats = await ldap_sync.sync_once(state)
            assert stats["reactivated"] >= 1
            assert (
                await c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
            ).status_code == 200


async def test_lookup_unknown_user(settings):
    directory = LdapDirectory(ldap_settings(settings), "leitor-senha")
    assert await directory.lookup(f"x-{uuid.uuid4().hex[:6]}") is None
    found = await directory.lookup("diego")
    assert found is not None and "dados" in found.groups


async def test_lookup_by_dn(settings):
    directory = LdapDirectory(ldap_settings(settings), "leitor-senha")
    found = await directory.lookup("qualquer", dn=f"uid=ana,ou=pessoas,{BASE}")
    assert found is not None and found.username == "ana"
    assert f"cn=llm-usuarios,ou=grupos,{BASE}" in found.group_dns
    assert await directory.lookup("ana", dn=f"uid=sumiu,ou=pessoas,{BASE}") is None
