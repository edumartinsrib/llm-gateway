"""API do console: sessão, permissões, chaves, provedores, modelos, usuários, uso e auditoria."""

from __future__ import annotations

import csv
import io

import httpx
import pytest
from conftest import FAKE, PROVIDER_SECRET, api, login, new_user_key

pytestmark = pytest.mark.usefixtures("catalog")


async def test_config_is_public(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/v1/config")
    assert r.status_code == 200
    body = r.json()
    assert body["auth"]["mode"] == "dev"
    assert body["api_base_url"] == "http://gateway.teste/v1"


async def test_session_required_and_admin_only_routes(app, username):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/v1/me")).status_code == 401
        bad = await c.get("/api/v1/me", headers={"Authorization": "Bearer nao.e.jwt"})
        assert bad.status_code == 401
    user = await login(app, username)
    me = (await user.get("/api/v1/me")).json()
    assert me["is_admin"] is False and me["role"] == "user"
    for path in ("/api/v1/users", "/api/v1/providers", "/api/v1/audit"):
        assert (await user.get(path)).status_code == 403, path
    assert (await user.post("/api/v1/cache/clear")).status_code == 403


async def test_bootstrap_admin(admin):
    me = (await admin.get("/api/v1/me")).json()
    assert me["is_admin"] is True and me["username"] == "admin"


async def test_keys_lifecycle(app, admin, username):
    console = await login(app, username)
    r = await console.post(
        "/api/v1/keys",
        json={"name": "notebook", "expires_in_days": 30, "allowed_models": ["gpt-4.1"]},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["key"].startswith("llmgw_") and created["status"] == "active"
    assert created["display"].endswith(created["key"][-4:])
    listed = (await console.get("/api/v1/keys")).json()
    assert len(listed) == 1 and "key" not in listed[0]  # segredo só aparece uma vez
    # Modelo inexistente na lista é recusado.
    bad = await console.post("/api/v1/keys", json={"name": "x", "allowed_models": ["nao-existe"]})
    assert bad.status_code == 422 and bad.json()["errors"] == ["nao-existe"]
    # Outro usuário não vê nem revoga a chave.
    other = await login(app, username + "-b")
    assert (await other.post(f"/api/v1/keys/{created['id']}/revoke")).status_code == 404
    assert (
        await other.get("/api/v1/keys", params={"user_id": created["user_id"]})
    ).status_code == 403
    # O administrador vê e edita.
    seen = (await admin.get("/api/v1/keys", params={"user_id": created["user_id"]})).json()
    assert [k["id"] for k in seen] == [created["id"]]
    r = await admin.patch(
        f"/api/v1/keys/{created['id']}", json={"monthly_budget_usd": 5, "rpm_limit": 10}
    )
    assert r.json()["monthly_budget_usd"] == 5 and r.json()["rpm_limit"] == 10
    # Depois que o administrador mexeu nos limites, o dono não afrouxa mais.
    r = await console.patch(f"/api/v1/keys/{created['id']}", json={"allowed_models": None})
    assert r.status_code == 403
    r = await admin.patch(f"/api/v1/keys/{created['id']}", json={"allowed_models": None})
    assert r.json()["allowed_models"] is None and r.json()["limits_locked"] is True
    assert (await console.post(f"/api/v1/keys/{created['id']}/revoke")).json()[
        "status"
    ] == "revoked"
    assert (
        await console.patch(f"/api/v1/keys/{created['id']}", json={"name": "y"})
    ).status_code == 409


async def test_model_catalog_views(app, admin, catalog, username):
    console = await login(app, username)
    visible = {m["name"] for m in (await console.get("/api/v1/models")).json()}
    assert "gpt-4.1" in visible and "dall-e-3" not in visible
    everything = {m["name"]: m for m in (await admin.get("/api/v1/models?all=true")).json()}
    # Usuário comum não consegue pedir a lista completa.
    assert {m["name"] for m in (await console.get("/api/v1/models?all=true")).json()} == visible
    emb = everything["text-embedding-3-small"]
    assert emb["kind"] == "embeddings" and emb["effective_cache_ttl_s"] == 86400
    assert everything["gpt-4.1"]["routable"] is True
    assert everything["o4-mini"]["supports_reasoning"] is True


async def test_model_rename_disable_and_manual_create(app, admin, catalog, username):
    _, key, _ = await new_user_key(app, username)
    model = catalog["models"]["gpt-4o-mini"]
    r = await admin.patch(f"/api/v1/models/{model['id']}", json={"name": "rapido"})
    assert r.status_code == 200 and r.json()["name"] == "rapido"
    try:
        async with api(app, key) as c:
            ok = await c.post(
                "/v1/chat/completions",
                json={"model": "rapido", "messages": [{"role": "user", "content": "oi"}]},
            )
            gone = await c.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "oi"}]},
            )
        assert ok.status_code == 200 and gone.status_code == 404
        dup = await admin.patch(f"/api/v1/models/{model['id']}", json={"name": "gpt-4.1"})
        assert dup.status_code == 409
    finally:
        await admin.patch(f"/api/v1/models/{model['id']}", json={"name": "gpt-4o-mini"})

    r = await admin.post(
        "/api/v1/models",
        json={
            "provider_id": catalog["provider"]["id"],
            "deployment": "gpt-4.1",
            "name": "x",
        },
    )
    assert r.status_code == 409  # deployment já cadastrado
    manual = await admin.post(
        "/api/v1/models",
        json={
            "provider_id": catalog["provider"]["id"],
            "deployment": "deploy-manual",
            "name": "manual",
            "enabled": True,
            "input_price": 1,
            "output_price": 2,
        },
    )
    assert manual.status_code == 201, manual.text
    assert manual.json()["source"] == "manual" and manual.json()["routable"] is True
    assert (await admin.delete(f"/api/v1/models/{manual.json()['id']}")).status_code == 204


async def test_provider_with_managed_secret(app, admin, state, secrets_store):
    body = {
        "name": "foundry-arm",
        "display_name": "Foundry via ARM",
        "secret": {
            "endpoint": f"{FAKE}/openai/v1/",
            "auth": "service_principal",
            "tenant_id": "tenant-1",
            "client_id": "cliente-falso",
            "client_secret": "segredo-falso",
            "discovery": "arm",
            "subscription_id": "sub-1",
            "resource_group": "rg-ia",
            "account_name": "conta-ia",
            "authority": FAKE,
        },
    }
    body["secret"]["arm_endpoint"] = FAKE
    r = await admin.post("/api/v1/providers", json={**body, "secret": {**body["secret"], "x": 1}})
    assert r.status_code == 422  # campo desconhecido no segredo é recusado
    r = await admin.post("/api/v1/providers", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["managed_secret"] is True
    assert created["vault_path"] == "llmgw/providers/foundry-arm"
    stored = secrets_store._data["llmgw/providers/foundry-arm"]
    assert stored["endpoint"] == f"{FAKE}/openai/v1/" and stored["client_secret"] == "segredo-falso"
    assert created["status"] == "ok" and created["endpoint"] == FAKE
    check = (await admin.post(f"/api/v1/providers/{created['id']}/check")).json()
    assert check["ok"] is True, check
    assert check["deployments"] == 5
    sync = (await admin.post(f"/api/v1/providers/{created['id']}/sync")).json()
    # Mesmos deployments do outro provedor: os nomes ganham o prefixo do provedor.
    assert sync["created"] == 4 and sync["skipped"] == 1
    names = {m["name"] for m in (await admin.get("/api/v1/models?all=true")).json()}
    assert "foundry-arm/gpt-4.1" in names
    listed = next(
        p for p in (await admin.get("/api/v1/providers")).json() if p["name"] == "foundry-arm"
    )
    assert listed["config"]["discovery"] == "arm" and listed["config"]["has_client_secret"]
    assert "client_secret" not in listed["config"]
    assert listed["models_total"] == 4

    # Atualizar sem reenviar o segredo mantém o que está no Vault.
    update = {
        **body["secret"],
        "client_secret": None,
        "discovery": "static",
        "deployments": ["gpt-4.1"],
    }
    r = await admin.put(f"/api/v1/providers/{created['id']}/secret", json=update)
    assert r.status_code == 200, r.text
    assert secrets_store._data["llmgw/providers/foundry-arm"]["client_secret"] == "segredo-falso"
    # Credencial nunca aparece na auditoria.
    audit = (await admin.get("/api/v1/audit", params={"resource_type": "provider"})).json()
    assert "segredo-falso" not in str(audit)

    assert (await admin.delete(f"/api/v1/providers/{created['id']}")).status_code == 204
    assert "llmgw/providers/foundry-arm" not in secrets_store._data
    names = {m["name"] for m in (await admin.get("/api/v1/models?all=true")).json()}
    assert "foundry-arm/gpt-4.1" not in names


async def test_provider_with_external_secret(admin, secrets_store):
    secrets_store._data["plataforma/azure/foundry-b"] = {
        "endpoint": FAKE,
        "api_key": "chave-falsa",
        "deployments": "gpt-4.1, text-embedding-3-small",
    }
    r = await admin.post(
        "/api/v1/providers", json={"name": "foundry-b", "vault_path": "plataforma/azure/foundry-b"}
    )
    assert r.status_code == 201, r.text
    provider = r.json()
    assert provider["managed_secret"] is False and provider["discovery"] == "static"
    # Credencial de fora: o console não sobrescreve.
    r = await admin.put(
        f"/api/v1/providers/{provider['id']}/secret",
        json={"endpoint": FAKE, "auth": "api_key", "api_key": "x"},
    )
    assert r.status_code == 409
    missing = await admin.post(
        "/api/v1/providers", json={"name": "foundry-c", "vault_path": "plataforma/nao-existe"}
    )
    assert missing.status_code == 422
    reserved = await admin.post(
        "/api/v1/providers", json={"name": "foundry-d", "vault_path": "llmgw/_system/keys"}
    )
    assert reserved.status_code == 422
    await admin.delete(f"/api/v1/providers/{provider['id']}")
    assert "plataforma/azure/foundry-b" in secrets_store._data  # segredo externo fica


async def test_users_admin(app, admin, username):
    console = await login(app, username)
    me = (await console.get("/api/v1/me")).json()
    page = (await admin.get("/api/v1/users", params={"q": username})).json()
    assert page["total"] == 1 and page["items"][0]["username"] == username
    r = await admin.patch(
        f"/api/v1/users/{me['id']}",
        json={"role": "admin", "monthly_budget_usd": 25, "tpm_limit": 50000},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_admin"] is True and r.json()["monthly_budget_usd"] == 25
    assert (await console.get("/api/v1/users")).status_code == 200
    # Administrador não bloqueia a si mesmo.
    assert (
        await console.patch(f"/api/v1/users/{me['id']}", json={"status": "blocked"})
    ).status_code == 409
    created = await admin.post(
        "/api/v1/users", json={"username": "Pre.Cadastro", "monthly_budget_usd": 3}
    )
    assert created.status_code == 201 and created.json()["username"] == "pre.cadastro"
    assert (await admin.post("/api/v1/users", json={"username": "pre.cadastro"})).status_code == 409
    audit = (await admin.get("/api/v1/audit", params={"action": "user.update"})).json()
    assert any(a["resource_id"] == me["id"] for a in audit["items"])


async def test_usage_summary_and_export(app, admin, state, username):
    console, key, me = await new_user_key(app, username)
    async with api(app, key) as c:
        for text in ("um", "dois", "um"):
            await c.post(
                "/v1/chat/completions",
                json={"model": "gpt-4.1", "messages": [{"role": "user", "content": text}]},
            )
        await c.post("/v1/embeddings", json={"model": "text-embedding-3-small", "input": "x"})
    await state.usage.drain()
    mine = (await console.get("/api/v1/usage/summary", params={"days": 7})).json()
    assert mine["scope"] == "me"
    assert mine["totals"]["requests"] == 4 and mine["totals"]["cache_hits"] == 1
    assert {m["key"] for m in mine["by_model"]} == {"gpt-4.1", "text-embedding-3-small"}
    assert mine["by_user"] == [] and mine["global_month"] is None
    assert mine["month"]["spent_usd"] > 0 and mine["by_day"]
    # Usuário comum não vê o uso dos outros.
    assert (await console.get("/api/v1/usage", params={"user_id": "x"})).status_code == 403
    everyone = (await admin.get("/api/v1/usage/summary", params={"days": 7})).json()
    assert everyone["scope"] == "all" and everyone["by_user"]
    assert everyone["totals"]["requests"] >= 4
    hits = (await console.get("/api/v1/usage", params={"cache": "hit"})).json()
    assert hits["total"] == 1
    r = await console.get("/api/v1/usage/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 4 and {row["username"] for row in rows} == {username}


async def test_clear_cache(admin):
    r = await admin.post("/api/v1/cache/clear")
    assert r.status_code == 200 and r.json()["removed"] >= 0


async def test_login_rate_limited(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        codes = [
            (await c.post("/api/v1/auth/login", json={"username": "   "})).status_code
            for _ in range(12)
        ]
    assert 429 in codes


async def test_provider_secret_validation(admin):
    r = await admin.post(
        "/api/v1/providers",
        json={"name": "incompleto", "secret": {"endpoint": FAKE, "auth": "service_principal"}},
    )
    assert r.status_code == 422 and "tenant_id" in r.json()["detail"]
    r = await admin.post(
        "/api/v1/providers", json={"name": "Nome Invalido", "secret": PROVIDER_SECRET}
    )
    assert r.status_code == 422


async def test_playground_uses_console_session(app, admin, state, username):
    console = await login(app, username)
    r = await console.post(
        "/api/v1/playground/chat/completions",
        json={"model": "gpt-4.1", "messages": [{"role": "user", "content": "teste no console"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"].endswith("teste no console")
    s = await console.post(
        "/api/v1/playground/chat/completions",
        json={"model": "gpt-4.1", "stream": True, "messages": [{"role": "user", "content": "oi"}]},
    )
    assert s.headers["content-type"].startswith("text/event-stream") and "[DONE]" in s.text
    await state.usage.drain()
    rows = (await console.get("/api/v1/usage")).json()["items"]
    assert rows and all(row["api_key_name"] == "console" for row in rows)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/api/v1/playground/chat/completions", json={})).status_code == 401


async def test_provider_paths_and_secret_destination(admin, secrets_store):
    traversal = await admin.post(
        "/api/v1/providers",
        json={"name": "foundry-x", "vault_path": "llmgw/providers/../_system/keys"},
    )
    assert traversal.status_code == 422
    other_team = await admin.post(
        "/api/v1/providers", json={"name": "foundry-x", "vault_path": "outra-equipe/azure"}
    )
    assert other_team.status_code == 422 and "prefixos" in other_team.json()["detail"]
    both = await admin.post(
        "/api/v1/providers",
        json={"name": "foundry-x", "vault_path": "llmgw/providers/x", "secret": PROVIDER_SECRET},
    )
    assert both.status_code == 422

    secret = {k: v for k, v in PROVIDER_SECRET.items() if k != "project_endpoint"}
    created = (
        await admin.post(
            "/api/v1/providers",
            json={"name": "foundry-dest", "secret": {**secret, "deployments": ["gpt-4.1"]}},
        )
    ).json()
    try:
        # Trocar o endpoint sem redigitar a chave: recusado (a chave guardada iria para lá).
        moved = await admin.put(
            f"/api/v1/providers/{created['id']}/secret",
            json={
                **secret,
                "api_key": None,
                "endpoint": "http://outro.teste",
                "deployments": ["x"],
            },
        )
        assert moved.status_code == 422 and "digite de novo" in moved.json()["detail"]
        # Redigitando, pode.
        ok = await admin.put(
            f"/api/v1/providers/{created['id']}/secret",
            json={**secret, "endpoint": "http://outro.teste", "deployments": ["x"]},
        )
        assert ok.status_code == 200
        stored = secrets_store._data["llmgw/providers/foundry-dest"]
        # Ajustes avançados (authority) foram mantidos mesmo sem vir no formulário.
        assert stored["authority"] == PROVIDER_SECRET["authority"]
    finally:
        await admin.delete(f"/api/v1/providers/{created['id']}")


async def test_admin_set_key_limits_are_locked(app, admin, username):
    console = await login(app, username)
    me = (await console.get("/api/v1/me")).json()
    r = await admin.post(
        f"/api/v1/keys?user_id={me['id']}", json={"name": "da-equipe", "monthly_budget_usd": 5}
    )
    assert r.status_code == 201 and r.json()["limits_locked"] is True
    key_id = r.json()["id"]
    loosen = await console.patch(f"/api/v1/keys/{key_id}", json={"monthly_budget_usd": None})
    assert loosen.status_code == 403
    rename = await console.patch(f"/api/v1/keys/{key_id}", json={"name": "renomeada"})
    assert rename.status_code == 200 and rename.json()["monthly_budget_usd"] == 5
    own = await console.post("/api/v1/keys", json={"name": "minha", "monthly_budget_usd": 1})
    assert own.json()["limits_locked"] is False
    assert (
        await console.patch(f"/api/v1/keys/{own.json()['id']}", json={"monthly_budget_usd": None})
    ).status_code == 200


async def test_logout_ends_sessions(app, username):
    console = await login(app, username)
    other_tab = await login(app, username)
    assert (await console.post("/api/v1/auth/logout")).status_code == 204
    assert (await console.get("/api/v1/me")).status_code == 401
    assert (await other_tab.get("/api/v1/me")).status_code == 401


async def test_csv_export_escapes_formulas_and_filters(app, admin, state, username):
    console = await login(app, username)
    key = (await console.post("/api/v1/keys", json={"name": "=HYPERLINK(1)"})).json()["key"]
    async with api(app, key) as c:
        await c.post(
            "/v1/chat/completions",
            json={"model": "gpt-4.1", "messages": [{"role": "user", "content": "csv"}]},
        )
    await state.usage.drain()
    r = await admin.get("/api/v1/usage/export", params={"q": username})
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows and {row["username"] for row in rows} == {username}
    assert all(row["api_key_name"] == "'=HYPERLINK(1)" for row in rows)


async def test_money_overflow_is_validation_error(app, username):
    console = await login(app, username)
    r = await console.post("/api/v1/keys", json={"name": "x", "monthly_budget_usd": "99999999999"})
    assert r.status_code == 422
