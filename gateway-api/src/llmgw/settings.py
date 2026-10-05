"""Configuração do gateway, lida só de variáveis de ambiente e do Consul.

Mesmo arranjo do Orquestrador: a plataforma injeta as conexões de Postgres,
Vault, Consul e Redis com nomes próprios e este módulo é o único lugar que
conhece esses nomes. Precedência: variável de ambiente > chave no Consul
(prefixo LLMGW_CONSUL_PREFIX) > padrão.

Segredos (credencial do Azure, senha da conta de serviço do LDAP, chave das
sessões) não ficam aqui: vêm do Vault (ver services/secrets.py).
"""

from __future__ import annotations

import base64
import json
import logging
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import AliasChoices, Field
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

log = logging.getLogger(__name__)


def _env(*names: str) -> AliasChoices:
    return AliasChoices(*names)


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class ConsulSettingsSource(PydanticBaseSettingsSource):
    """Lê `<prefixo>/<campo>` do KV do Consul. Silencioso se o Consul não estiver configurado."""

    def __init__(
        self, settings_cls: type[BaseSettings], address: str | None, token: str | None, prefix: str
    ):
        super().__init__(settings_cls)
        self._values: dict[str, Any] = {}
        if not address:
            return
        url = address if address.startswith("http") else f"http://{address}"
        headers = {"X-Consul-Token": token} if token else {}
        try:
            resp = httpx.get(
                f"{url}/v1/kv/{prefix.strip('/')}/",
                params={"recurse": "true"},
                headers=headers,
                timeout=5,
            )
            if resp.status_code == 404:
                return
            resp.raise_for_status()
            for entry in resp.json():
                key = entry["Key"].rsplit("/", 1)[-1]
                if not key or entry.get("Value") is None:
                    continue
                raw = base64.b64decode(entry["Value"]).decode()
                try:
                    self._values[key] = json.loads(raw)
                except json.JSONDecodeError:
                    self._values[key] = raw
        except httpx.HTTPError as exc:
            log.warning("Consul indisponível, seguindo sem ele: %s", exc)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return {k: v for k, v in self._values.items() if k in self.settings_cls.model_fields}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)

    # --- Postgres ---
    database_url: str | None = Field(
        default=None, validation_alias=_env("LLMGW_DATABASE_URL", "DATABASE_URL")
    )
    db_host: str = Field(
        default="localhost",
        validation_alias=_env("LLMGW_DB_HOST", "POSTGRES_HOST", "PGHOST", "DB_HOST"),
    )
    db_port: int = Field(
        default=5432, validation_alias=_env("LLMGW_DB_PORT", "POSTGRES_PORT", "PGPORT", "DB_PORT")
    )
    db_user: str = Field(
        default="llmgw",
        validation_alias=_env("LLMGW_DB_USER", "POSTGRES_USER", "PGUSER", "DB_USER"),
    )
    db_password: str = Field(
        default="llmgw",
        validation_alias=_env(
            "LLMGW_DB_PASSWORD", "POSTGRES_PASSWORD", "PGPASSWORD", "DB_PASSWORD"
        ),
    )
    db_name: str = Field(
        default="llmgw",
        validation_alias=_env("LLMGW_DB_NAME", "POSTGRES_DB", "PGDATABASE", "DB_NAME"),
    )
    db_pool_size: int = Field(default=10, validation_alias=_env("LLMGW_DB_POOL_SIZE"))

    # --- Redis (cache de respostas, limites por minuto, eventos entre réplicas) ---
    redis_url: str | None = Field(
        default=None, validation_alias=_env("LLMGW_REDIS_URL", "REDIS_URL")
    )
    redis_host: str | None = Field(default=None, validation_alias=_env("REDIS_HOST"))
    redis_port: int = Field(default=6379, validation_alias=_env("REDIS_PORT"))
    redis_password: str | None = Field(default=None, validation_alias=_env("REDIS_PASSWORD"))

    # --- Vault ---
    vault_addr: str | None = Field(
        default=None, validation_alias=_env("VAULT_ADDR", "LLMGW_VAULT_ADDR")
    )
    vault_token: str | None = Field(
        default=None, validation_alias=_env("VAULT_TOKEN", "LLMGW_VAULT_TOKEN")
    )
    vault_role_id: str | None = Field(default=None, validation_alias=_env("VAULT_ROLE_ID"))
    vault_secret_id: str | None = Field(default=None, validation_alias=_env("VAULT_SECRET_ID"))
    vault_namespace: str | None = Field(default=None, validation_alias=_env("VAULT_NAMESPACE"))
    vault_mount: str = Field(
        default="secret", validation_alias=_env("LLMGW_VAULT_MOUNT", "VAULT_KV_MOUNT")
    )
    vault_prefix: str = Field(default="llmgw", validation_alias=_env("LLMGW_VAULT_PREFIX"))
    secret_store: Literal["vault", "memory"] = Field(
        default="vault", validation_alias=_env("LLMGW_SECRET_STORE")
    )

    # --- Autenticação do console ---
    # dev aceita qualquer usuário sem senha: só com LLMGW_AUTH_MODE=dev explícito.
    auth_mode: Literal["dev", "ldap"] = Field(
        default="ldap", validation_alias=_env("LLMGW_AUTH_MODE")
    )
    session_ttl_min: int = Field(default=480, validation_alias=_env("LLMGW_SESSION_TTL_MIN"))
    session_key: str | None = Field(default=None, validation_alias=_env("LLMGW_SESSION_KEY"))
    bootstrap_admins: str = Field(default="", validation_alias=_env("LLMGW_BOOTSTRAP_ADMINS"))
    login_max_attempts: int = Field(default=10, validation_alias=_env("LLMGW_LOGIN_MAX_ATTEMPTS"))

    # --- LDAP ---
    ldap_url: str | None = Field(default=None, validation_alias=_env("LLMGW_LDAP_URL", "LDAP_URL"))
    ldap_bind_dn: str | None = Field(
        default=None, validation_alias=_env("LLMGW_LDAP_BIND_DN", "LDAP_BIND_DN")
    )
    ldap_bind_password: str | None = Field(
        default=None, validation_alias=_env("LLMGW_LDAP_BIND_PASSWORD", "LDAP_BIND_PASSWORD")
    )
    ldap_user_base: str | None = Field(default=None, validation_alias=_env("LLMGW_LDAP_USER_BASE"))
    ldap_user_filter: str = Field(
        default="(|(sAMAccountName={username})(uid={username}))",
        validation_alias=_env("LLMGW_LDAP_USER_FILTER"),
    )
    ldap_user_dn_template: str | None = Field(
        default=None, validation_alias=_env("LLMGW_LDAP_USER_DN_TEMPLATE")
    )
    ldap_group_mode: Literal["auto", "memberof", "search"] = Field(
        default="auto", validation_alias=_env("LLMGW_LDAP_GROUP_MODE")
    )
    ldap_group_base: str | None = Field(
        default=None, validation_alias=_env("LLMGW_LDAP_GROUP_BASE")
    )
    ldap_group_filter: str = Field(
        default="(|(member={user_dn})(uniqueMember={user_dn})(memberUid={username}))",
        validation_alias=_env("LLMGW_LDAP_GROUP_FILTER"),
    )
    ldap_nested_groups: bool = Field(
        default=False, validation_alias=_env("LLMGW_LDAP_NESTED_GROUPS")
    )
    ldap_allowed_groups: str = Field(default="", validation_alias=_env("LLMGW_LDAP_ALLOWED_GROUPS"))
    ldap_admin_groups: str = Field(default="", validation_alias=_env("LLMGW_LDAP_ADMIN_GROUPS"))
    ldap_start_tls: bool = Field(default=False, validation_alias=_env("LLMGW_LDAP_START_TLS"))
    ldap_verify_tls: bool = Field(default=True, validation_alias=_env("LLMGW_LDAP_VERIFY_TLS"))
    ldap_ca_file: str | None = Field(default=None, validation_alias=_env("LLMGW_LDAP_CA_FILE"))
    ldap_timeout_s: int = Field(default=10, validation_alias=_env("LLMGW_LDAP_TIMEOUT_S"))
    ldap_attr_display_name: str = Field(
        default="displayName", validation_alias=_env("LLMGW_LDAP_ATTR_DISPLAY_NAME")
    )
    ldap_attr_email: str = Field(default="mail", validation_alias=_env("LLMGW_LDAP_ATTR_EMAIL"))
    ldap_sync_interval_s: int = Field(
        default=900, validation_alias=_env("LLMGW_LDAP_SYNC_INTERVAL_S")
    )
    ldap_sync_max_missing_pct: int = Field(
        default=20, validation_alias=_env("LLMGW_LDAP_SYNC_MAX_MISSING_PCT")
    )

    # --- Provedores (Foundry) ---
    public_url: str = Field(
        default="http://localhost:8090", validation_alias=_env("LLMGW_PUBLIC_URL")
    )
    bootstrap_providers: str = Field(default="", validation_alias=_env("LLMGW_BOOTSTRAP_PROVIDERS"))
    # Caminhos do Vault (além de <prefixo>/providers/) que o console aceita para segredos
    # provisionados por fora, ex.: "plataforma/azure/". Nunca <prefixo>/_system.
    provider_vault_prefixes: str = Field(
        default="", validation_alias=_env("LLMGW_PROVIDER_VAULT_PREFIXES")
    )
    # Hosts aceitos nos endpoints informados pelo console (sufixos; "*" = qualquer).
    provider_allowed_hosts: str = Field(
        default=".azure.com,.azure.us,.azure.cn,.microsoftonline.com,.microsoftonline.us,.chinacloudapi.cn,.usgovcloudapi.net",
        validation_alias=_env("LLMGW_PROVIDER_ALLOWED_HOSTS"),
    )
    provider_allow_http: bool = Field(
        default=False, validation_alias=_env("LLMGW_PROVIDER_ALLOW_HTTP")
    )
    forwarded_allow_ips: str = Field(
        default="127.0.0.1",
        validation_alias=_env("LLMGW_FORWARDED_ALLOW_IPS", "FORWARDED_ALLOW_IPS"),
    )
    provider_refresh_s: int = Field(default=60, validation_alias=_env("LLMGW_PROVIDER_REFRESH_S"))
    discovery_interval_s: int = Field(
        default=3600, validation_alias=_env("LLMGW_DISCOVERY_INTERVAL_S")
    )
    auto_enable_models: bool = Field(
        default=False, validation_alias=_env("LLMGW_AUTO_ENABLE_MODELS")
    )
    upstream_timeout_s: float = Field(
        default=600, validation_alias=_env("LLMGW_UPSTREAM_TIMEOUT_S")
    )
    upstream_connect_timeout_s: float = Field(
        default=10, validation_alias=_env("LLMGW_UPSTREAM_CONNECT_TIMEOUT_S")
    )
    upstream_retries: int = Field(default=2, validation_alias=_env("LLMGW_UPSTREAM_RETRIES"))
    upstream_max_connections: int = Field(
        default=200, validation_alias=_env("LLMGW_UPSTREAM_MAX_CONNECTIONS")
    )
    max_request_mb: int = Field(default=50, validation_alias=_env("LLMGW_MAX_REQUEST_MB"))
    background_tasks_enabled: bool = Field(
        default=True, validation_alias=_env("LLMGW_BACKGROUND_TASKS")
    )

    # --- Cache de respostas ---
    cache_enabled: bool = Field(default=True, validation_alias=_env("LLMGW_CACHE_ENABLED"))
    cache_scope: Literal["user", "global"] = Field(
        default="user", validation_alias=_env("LLMGW_CACHE_SCOPE")
    )
    cache_chat_ttl_s: int = Field(default=3600, validation_alias=_env("LLMGW_CACHE_CHAT_TTL_S"))
    cache_embeddings_ttl_s: int = Field(
        default=86400, validation_alias=_env("LLMGW_CACHE_EMBEDDINGS_TTL_S")
    )
    cache_max_entry_kb: int = Field(default=1024, validation_alias=_env("LLMGW_CACHE_MAX_ENTRY_KB"))
    cache_memory_max_entries: int = Field(
        default=5000, validation_alias=_env("LLMGW_CACHE_MEMORY_MAX_ENTRIES")
    )

    # --- Custos e limites ---
    timezone: str = Field(default="America/Sao_Paulo", validation_alias=_env("LLMGW_TIMEZONE"))
    default_monthly_budget_usd: Decimal | None = Field(
        default=None, validation_alias=_env("LLMGW_DEFAULT_MONTHLY_BUDGET_USD")
    )
    global_monthly_budget_usd: Decimal | None = Field(
        default=None, validation_alias=_env("LLMGW_GLOBAL_MONTHLY_BUDGET_USD")
    )
    budget_alert_pct: int = Field(default=80, validation_alias=_env("LLMGW_BUDGET_ALERT_PCT"))
    default_rpm: int = Field(default=0, validation_alias=_env("LLMGW_DEFAULT_RPM"))
    default_tpm: int = Field(default=0, validation_alias=_env("LLMGW_DEFAULT_TPM"))
    usage_retention_days: int = Field(
        default=400, validation_alias=_env("LLMGW_USAGE_RETENTION_DAYS")
    )

    # --- Operação ---
    cors_origins: str = Field(default="", validation_alias=_env("LLMGW_CORS_ORIGINS"))
    otel_endpoint: str | None = Field(
        default=None, validation_alias=_env("OTEL_EXPORTER_OTLP_ENDPOINT")
    )

    # --- Consul ---
    consul_addr: str | None = Field(
        default=None, validation_alias=_env("CONSUL_HTTP_ADDR", "CONSUL_ADDR")
    )
    consul_token: str | None = Field(
        default=None, validation_alias=_env("CONSUL_HTTP_TOKEN", "CONSUL_TOKEN")
    )
    consul_prefix: str = Field(default="llmgw/config", validation_alias=_env("LLMGW_CONSUL_PREFIX"))

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        import os

        consul = ConsulSettingsSource(
            settings_cls,
            os.environ.get("CONSUL_HTTP_ADDR") or os.environ.get("CONSUL_ADDR"),
            os.environ.get("CONSUL_HTTP_TOKEN") or os.environ.get("CONSUL_TOKEN"),
            os.environ.get("LLMGW_CONSUL_PREFIX", "llmgw/config"),
        )
        return (init_settings, env_settings, consul, dotenv_settings, file_secret_settings)

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            url = self.database_url
            for prefix in ("postgres://", "postgresql://"):
                if url.startswith(prefix):
                    return "postgresql+asyncpg://" + url[len(prefix) :]
            return url
        return (
            f"postgresql+asyncpg://{quote(self.db_user)}:{quote(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def effective_redis_url(self) -> str | None:
        if self.redis_url:
            return self.redis_url
        if self.redis_host:
            auth = f":{quote(self.redis_password)}@" if self.redis_password else ""
            return f"redis://{auth}{self.redis_host}:{self.redis_port}/0"
        return None

    @property
    def bootstrap_admin_list(self) -> list[str]:
        return [u.lower() for u in _split(self.bootstrap_admins)]

    @property
    def ldap_allowed_group_list(self) -> list[str]:
        return _split(self.ldap_allowed_groups)

    @property
    def ldap_admin_group_list(self) -> list[str]:
        return _split(self.ldap_admin_groups)

    @property
    def ldap_url_list(self) -> list[str]:
        return _split(self.ldap_url or "")

    @property
    def provider_vault_prefix_list(self) -> list[str]:
        own = f"{self.vault_prefix}/providers/"
        extra = [p.strip().strip("/") + "/" for p in _split(self.provider_vault_prefixes)]
        return [own, *extra]

    @property
    def provider_allowed_host_list(self) -> list[str]:
        return [h.lower() for h in _split(self.provider_allowed_hosts)]

    @property
    def cors_origin_list(self) -> list[str]:
        return _split(self.cors_origins)

    @property
    def bootstrap_provider_map(self) -> dict[str, str]:
        """`nome=caminho/no/vault,outro=...` → {nome: caminho}."""
        out: dict[str, str] = {}
        for entry in _split(self.bootstrap_providers):
            name, sep, path = entry.partition("=")
            if sep and name.strip() and path.strip():
                out[name.strip()] = path.strip().strip("/")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
