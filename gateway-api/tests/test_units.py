"""Testes de unidade: credencial do Foundry, classificação, custo, cache, grupos e tokens."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import jwt
import pytest

from llmgw.security.tokens import (
    API_KEY_PREFIX,
    SessionIssuer,
    display_key,
    make_token,
    parse_token,
    verify_secret,
)
from llmgw.services.cache import CachedResponse, MemoryCache, make_key
from llmgw.services.foundry import FoundryConfig, ProviderConfigError, classify, guess_features
from llmgw.services.gateway import Route
from llmgw.services.groups import GroupRule, group_key, intersects, normalize_dn
from llmgw.services.usage import Usage, compute_cost, usage_from_openai, usage_from_responses
from llmgw.settings import Settings


# --------------------------------------------------------------------------- credencial
def test_config_api_key_minimal():
    cfg = FoundryConfig.from_secret({"endpoint": "https://r.openai.azure.com/", "api_key": "k"})
    assert cfg.endpoint == "https://r.openai.azure.com"
    assert cfg.auth == "api_key" and cfg.discovery == "none" and cfg.api_style == "v1"


def test_config_accepts_pasted_base_url_and_project_endpoint():
    cfg = FoundryConfig.from_secret(
        {
            "endpoint": "https://r.services.ai.azure.com/api/projects/meu-projeto",
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
        }
    )
    assert cfg.endpoint == "https://r.services.ai.azure.com"
    assert cfg.project_endpoint == "https://r.services.ai.azure.com/api/projects/meu-projeto"
    assert cfg.auth == "service_principal" and cfg.discovery == "project"
    base = FoundryConfig.from_secret(
        {"endpoint": "https://r.services.ai.azure.com/openai/v1/", "api_key": "k"}
    )
    assert base.endpoint == "https://r.services.ai.azure.com"


def test_config_infers_arm_and_static_discovery():
    arm = FoundryConfig.from_secret(
        {
            "endpoint": "https://r.openai.azure.com",
            "api_key": "k",
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
            "subscription_id": "sub",
            "resource_group": "rg",
            "account_name": "acc",
        }
    )
    assert arm.auth == "api_key" and arm.discovery == "arm" and arm.has_service_principal
    static = FoundryConfig.from_secret(
        {"endpoint": "https://r.openai.azure.com", "api_key": "k", "deployments": "a, b,a"}
    )
    assert static.discovery == "static" and static.static_deployments == ("a", "b")


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        (None, "não encontrado"),
        ({"api_key": "k"}, "endpoint"),
        ({"endpoint": "ftp://x", "api_key": "k"}, "endpoint inválido"),
        ({"endpoint": "https://x", "auth": "api_key"}, "api_key"),
        ({"endpoint": "https://x", "tenant_id": "t"}, "service_principal"),
        ({"endpoint": "https://x", "api_key": "k", "discovery": "project"}, "service principal"),
        ({"endpoint": "https://x", "api_key": "k", "discovery": "static"}, "deployments"),
        ({"endpoint": "https://x", "api_key": "k", "api_style": "v2"}, "api_style"),
    ],
)
def test_config_errors(secret, message):
    with pytest.raises(ProviderConfigError, match=message):
        FoundryConfig.from_secret(secret)


def test_fingerprint_changes_with_secret_and_view_hides_it():
    a = FoundryConfig.from_secret({"endpoint": "https://x", "api_key": "k1"})
    b = FoundryConfig.from_secret({"endpoint": "https://x", "api_key": "k2"})
    assert a.fingerprint() != b.fingerprint()
    view = a.public_view()
    assert view["has_api_key"] is True and "k1" not in str(view)


def test_classify_and_features():
    assert classify("text-embedding-3-large", {}) == "embeddings"
    assert classify("x", {"embeddings": "true"}) == "embeddings"
    assert classify("gpt-4o", {"chatCompletion": "true"}) == "chat"
    assert classify("dall-e-3", {}) == "other"
    assert classify("x", {"imageGenerations": "true"}) == "other"
    assert classify("DeepSeek-R1", {}) == "chat"
    assert guess_features("o4-mini")["supports_reasoning"] is True
    assert guess_features("gpt-4o")["supports_vision"] is True
    assert guess_features("text-embedding-3-small") == {
        "supports_reasoning": False,
        "supports_vision": False,
    }


# --------------------------------------------------------------------------- custo
def _route(inp="2", cached=None, out="8") -> Route:
    return Route(
        model_id="1",
        name="m",
        kind="chat",
        provider_id="p",
        provider_name="p",
        deployment="d",
        input_price=Decimal(inp),
        cached_input_price=Decimal(cached) if cached is not None else None,
        output_price=Decimal(out),
        cache_ttl_s=None,
        allowed_groups=GroupRule([]),
        supports_responses=True,
        created=0,
    )


def test_cost_with_and_without_cached_price():
    usage = Usage(prompt_tokens=1_000_000, cached_tokens=400_000, completion_tokens=500_000)
    assert compute_cost(_route(cached="0.5"), usage) == Decimal("5.4")
    assert compute_cost(_route(), usage) == Decimal("6")  # sem preço de cache: entrada cheia
    assert compute_cost(_route(), Usage()) == Decimal("0")


def test_usage_extraction():
    chat = usage_from_openai(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 99},
            "completion_tokens_details": {"reasoning_tokens": 3},
        }
    )
    assert chat == Usage(10, 10, 5, 3, 15)  # cache nunca maior que o prompt
    emb = usage_from_openai({"prompt_tokens": 7, "total_tokens": 7})
    assert emb == Usage(7, 0, 0, 0, 7)
    resp = usage_from_responses(
        {"input_tokens": 4, "output_tokens": 6, "input_tokens_details": {"cached_tokens": 2}}
    )
    assert resp == Usage(4, 2, 6, 0, 10)
    assert usage_from_openai(None) is None and usage_from_openai("x") is None


# --------------------------------------------------------------------------- cache
def test_cache_key_ignores_irrelevant_fields_and_respects_scope():
    body = {"model": "m", "messages": [{"role": "user", "content": "a"}], "temperature": 0}
    k1 = make_key("user:1", "chat", "m", body)
    assert k1 == make_key("user:1", "chat", "m", {**body, "user": "x", "metadata": {"a": 1}})
    assert k1 == make_key("user:1", "chat", "m", dict(reversed(list(body.items()))))
    assert k1 != make_key("user:2", "chat", "m", body)
    assert k1 != make_key("user:1", "chat", "m", {**body, "temperature": 1})
    assert k1 != make_key("user:1", "chat", "m", {**body, "stream": True})


async def test_memory_cache_ttl_and_lru():
    cache = MemoryCache(max_entries=2)
    value = CachedResponse(kind="json", status=200, usage={}, cost_usd="0.1", body={"a": 1})
    await cache.set("a", value, 60)
    await cache.set("b", value, 60)
    await cache.get("a")
    await cache.set("c", value, 60)  # remove o menos usado (b)
    assert await cache.get("b") is None
    assert (await cache.get("a")).body == {"a": 1}
    await cache.set("d", value, -1)
    assert await cache.get("d") is None


def test_cached_sse_roundtrip():
    value = CachedResponse(
        kind="sse", status=200, usage={"total_tokens": 3}, cost_usd="0", events=[("data: x", False)]
    )
    again = CachedResponse.loads(value.dumps())
    assert again.events == [("data: x", False)]


# --------------------------------------------------------------------------- grupos
def test_group_matching():
    assert group_key("CN=LLM Usuarios,OU=Grupos,DC=corp,DC=local") == "llm usuarios"
    assert group_key("  llm-usuarios ") == "llm-usuarios"
    assert intersects(["llm-usuarios"], ["LLM-Usuarios"])
    assert intersects([], ["cn=LLM-Usuarios,ou=grupos,dc=x"], ["CN=llm-usuarios,OU=Grupos,DC=x"])
    assert not intersects(["llm-usuarios"], ["cn=LLM-Usuarios,ou=grupos,dc=x"])
    assert not intersects(["dados"], ["llm-usuarios"])


# --------------------------------------------------------------------------- tokens
def test_api_key_roundtrip():
    key_id = uuid.uuid4()
    token, digest = make_token(API_KEY_PREFIX, key_id)
    parsed = parse_token(token, API_KEY_PREFIX)
    assert parsed is not None and parsed[0] == key_id and verify_secret(parsed[1], digest)
    assert not verify_secret(parsed[1] + "x", digest)
    assert parse_token("sk-outra", API_KEY_PREFIX) is None
    assert parse_token(f"{API_KEY_PREFIX}nao-uuid.x", API_KEY_PREFIX) is None
    assert parse_token(f"{API_KEY_PREFIX}{key_id.hex}", API_KEY_PREFIX) is None
    assert display_key(key_id, token[-4:]).endswith(token[-4:])


def test_session_tokens():
    issuer = SessionIssuer("chave-de-teste-com-mais-de-32-bytes!!")
    uid = uuid.uuid4()
    token, _ = issuer.issue(uid, "ana", 3, timedelta(minutes=5))
    claims = issuer.verify(token)
    assert claims.user_id == uid and claims.session_version == 3
    with pytest.raises(jwt.PyJWTError):
        SessionIssuer("outra-chave-de-teste-com-32-bytes!!").verify(token)
    expired, _ = issuer.issue(uid, "ana", 3, timedelta(seconds=-1))
    with pytest.raises(jwt.ExpiredSignatureError):
        issuer.verify(expired)


# --------------------------------------------------------------------------- configuração
def test_settings_env_names(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("REDIS_HOST", "cache")
    monkeypatch.setenv("REDIS_PASSWORD", "p@ss")
    monkeypatch.setenv("LLMGW_BOOTSTRAP_PROVIDERS", "foundry=llmgw/providers/foundry, b = x/y/ ")
    monkeypatch.setenv("LLMGW_DEFAULT_MONTHLY_BUDGET_USD", "50")
    s = Settings()
    assert "@db:5432/" in s.sqlalchemy_url
    assert s.effective_redis_url == "redis://:p%40ss@cache:6379/0"
    assert s.bootstrap_provider_map == {"foundry": "llmgw/providers/foundry", "b": "x/y"}
    assert s.default_monthly_budget_usd == Decimal("50")


def test_group_rule_dn_vs_name():
    admins = GroupRule(["CN=GW-Admins,OU=Seguranca,DC=corp,DC=local"])
    # Mesmo nome em outra OU não concede acesso quando a regra é um DN.
    assert not admins.matches(["gw-admins"], ["CN=GW-Admins,OU=Autoatendimento,DC=corp,DC=local"])
    assert admins.matches([], ["cn=gw-admins, ou=seguranca, dc=corp, dc=local"])
    by_name = GroupRule(["gw-admins"])
    assert by_name.matches(["GW-Admins"], [])
    assert not GroupRule([])
    assert normalize_dn("CN=A B,OU=X") == "cn=a b,ou=x"


def test_check_url_rules():
    from llmgw.errors import ApiError
    from llmgw.routers.providers import _check_url, _external_path

    strict = Settings(provider_allow_http=False)
    _check_url(strict, "endpoint", "https://meu.services.ai.azure.com")
    _check_url(strict, "authority", "https://login.microsoftonline.com")
    for bad in (
        "http://meu.services.ai.azure.com",
        "https://atacante.com",
        "https://azure.com.evil.io",
    ):
        with pytest.raises(ApiError):
            _check_url(strict, "endpoint", bad)
    paths = Settings(provider_vault_prefixes="plataforma/azure")
    assert _external_path(paths, "llmgw/providers/x") == "llmgw/providers/x"
    assert _external_path(paths, "plataforma/azure/foundry") == "plataforma/azure/foundry"
    for bad in (
        "llmgw/providers/../_system/keys",
        "/llmgw/providers/x",
        "llmgw//providers/x",
        "llmgw/_system/keys",
        "outra-equipe/segredo",
        "llmgw/providers/./x",
    ):
        with pytest.raises(ApiError):
            _external_path(paths, bad)


async def test_read_timeout_is_not_retried():
    import httpx

    from llmgw.services.foundry import FoundryAgent, UpstreamError

    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ReadTimeout("demorou", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = FoundryConfig.from_secret({"endpoint": "https://r.openai.azure.com", "api_key": "k"})
    agent = FoundryAgent("p", "p", cfg, http, retries=2)
    with pytest.raises(UpstreamError) as exc:
        await agent.send("chat", "gpt", {"messages": []})
    assert exc.value.status == 504 and len(attempts) == 1

    attempts.clear()

    def refuse(request):
        attempts.append(request)
        raise httpx.ConnectError("recusado", request=request)

    agent = FoundryAgent(
        "p", "p", cfg, httpx.AsyncClient(transport=httpx.MockTransport(refuse)), retries=2
    )
    with pytest.raises(UpstreamError):
        await agent.send("chat", "gpt", {"messages": []})
    assert len(attempts) == 3  # conexão recusada: nada chegou ao Azure, então repete
