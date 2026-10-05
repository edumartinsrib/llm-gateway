"""Esquemas de entrada e saída da API do console (ver docs/api.md)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

T = TypeVar("T")

# Dinheiro sai como número no JSON (o console formata); entra como número ou texto.
Money = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float, when_used="json")]
MoneyIn = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=6)]

Role = Literal["user", "admin"]
UserStatus = Literal["active", "blocked", "disabled"]
ModelKind = Literal["chat", "embeddings"]
ProviderName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- config / sessão
class AuthConfig(BaseModel):
    mode: Literal["dev", "ldap"]


class CacheConfig(BaseModel):
    enabled: bool
    scope: Literal["user", "global"]
    chat_ttl_s: int
    embeddings_ttl_s: int


class ConfigOut(BaseModel):
    auth: AuthConfig
    version: str
    api_base_url: str
    cache: CacheConfig
    timezone: str
    budget_alert_pct: int


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str | None = Field(default=None, max_length=1024)


class MeOut(BaseModel):
    id: str
    username: str
    display_name: str | None
    email: str | None
    groups: list[str]
    role: Role
    is_admin: bool
    status: UserStatus
    monthly_budget_usd: Money | None
    spent_month_usd: Money
    period: str
    rpm_limit: int | None
    tpm_limit: int | None
    allowed_models: list[str] | None
    last_login_at: datetime | None


class LoginOut(BaseModel):
    token: str
    expires_at: datetime
    user: MeOut


# --------------------------------------------------------------------------- chaves de API
class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    monthly_budget_usd: MoneyIn | None = None
    rpm_limit: int | None = Field(default=None, ge=1, le=100_000)
    allowed_models: list[str] | None = None


class KeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    monthly_budget_usd: MoneyIn | None = None
    rpm_limit: int | None = Field(default=None, ge=1, le=100_000)
    allowed_models: list[str] | None = None


class KeyOut(BaseModel):
    id: str
    name: str
    display: str
    user_id: str
    username: str
    status: Literal["active", "revoked", "expired"]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_by: str | None
    last_used_at: datetime | None
    monthly_budget_usd: Money | None
    spent_month_usd: Money
    rpm_limit: int | None
    allowed_models: list[str] | None
    limits_locked: bool


class KeyCreated(KeyOut):
    key: str


# --------------------------------------------------------------------------- modelos
class ModelFields(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    input_price: MoneyIn | None = None
    cached_input_price: MoneyIn | None = None
    output_price: MoneyIn | None = None
    cache_ttl_s: int | None = Field(default=None, ge=0, le=30 * 86400)
    allowed_groups: list[str] | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    supports_responses: bool | None = None
    supports_vision: bool | None = None
    supports_reasoning: bool | None = None


class ModelCreate(ModelFields):
    provider_id: str
    deployment: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: ModelKind = "chat"
    enabled: bool = False


class ModelUpdate(ModelFields):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: ModelKind | None = None
    enabled: bool | None = None


class ModelOut(BaseModel):
    id: str
    name: str
    provider_id: str
    provider_name: str
    deployment: str
    kind: str
    upstream_model: str | None
    upstream_version: str | None
    publisher: str | None
    sku: str | None
    source: str
    enabled: bool
    available: bool
    routable: bool
    accessible: bool
    description: str | None
    input_price: Money
    cached_input_price: Money | None
    output_price: Money
    cache_ttl_s: int | None
    effective_cache_ttl_s: int
    allowed_groups: list[str]
    context_window: int | None
    max_output_tokens: int | None
    supports_responses: bool
    supports_vision: bool
    supports_reasoning: bool
    discovered_at: datetime | None
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime | None


# --------------------------------------------------------------------------- provedores
class ProviderSecretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=8, max_length=500)
    auth: Literal["api_key", "service_principal"]
    api_key: str | None = Field(default=None, max_length=500)
    tenant_id: str | None = Field(default=None, max_length=100)
    client_id: str | None = Field(default=None, max_length=100)
    client_secret: str | None = Field(default=None, max_length=500)
    discovery: Literal["project", "arm", "static", "none"] | None = None
    project_endpoint: str | None = Field(default=None, max_length=500)
    subscription_id: str | None = Field(default=None, max_length=100)
    resource_group: str | None = Field(default=None, max_length=200)
    account_name: str | None = Field(default=None, max_length=200)
    deployments: list[str] | None = None
    api_style: Literal["v1", "deployments"] | None = None
    api_version: str | None = Field(default=None, max_length=40)
    scope: str | None = Field(default=None, max_length=200)
    # Nuvens soberanas (ex.: Azure Government): login e Resource Manager próprios.
    authority: str | None = Field(default=None, max_length=300)
    arm_endpoint: str | None = Field(default=None, max_length=300)
    # Ao editar, campos sensíveis vazios mantêm o valor já guardado no Vault.
    keep_existing_secrets: bool = True


class ProviderCreate(BaseModel):
    name: ProviderName
    display_name: str | None = Field(default=None, max_length=200)
    vault_path: str | None = Field(default=None, max_length=500)
    secret: ProviderSecretIn | None = None


class ProviderUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None


class ProviderOut(BaseModel):
    id: str
    name: str
    display_name: str | None
    kind: str
    vault_path: str
    managed_secret: bool
    enabled: bool
    endpoint: str | None
    auth_type: str | None
    discovery: str | None
    status: str
    last_error: str | None
    last_checked_at: datetime | None
    last_sync_at: datetime | None
    secret_loaded_at: datetime | None
    agent_loaded: bool
    models_total: int
    models_enabled: int
    config: dict[str, Any] | None
    created_at: datetime
    created_by: str | None


class CheckOut(BaseModel):
    ok: bool
    message: str
    deployments: int | None = None


class SyncOut(BaseModel):
    found: int
    created: int
    updated: int
    missing: int
    skipped: int


# --------------------------------------------------------------------------- usuários
class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=256)
    display_name: str | None = Field(default=None, max_length=300)
    email: str | None = Field(default=None, max_length=300)
    role: Role = "user"
    monthly_budget_usd: MoneyIn | None = None
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    allowed_models: list[str] | None = None


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=300)
    role: Role | None = None
    status: Literal["active", "blocked"] | None = None
    status_reason: str | None = Field(default=None, max_length=500)
    monthly_budget_usd: MoneyIn | None = None
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    allowed_models: list[str] | None = None
    groups: list[str] | None = None  # só no modo dev (no LDAP os grupos vêm do diretório)


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str | None
    email: str | None
    groups: list[str]
    role: Role
    is_admin: bool
    status: UserStatus
    status_reason: str | None
    monthly_budget_usd: Money | None
    spent_month_usd: Money
    requests_month: int
    rpm_limit: int | None
    tpm_limit: int | None
    allowed_models: list[str] | None
    keys_active: int
    created_at: datetime
    last_login_at: datetime | None
    last_seen_at: datetime | None
    ldap_synced_at: datetime | None


# --------------------------------------------------------------------------- uso
class UsageOut(BaseModel):
    id: int
    ts: datetime
    request_id: str
    user_id: str | None
    username: str
    api_key_id: str | None
    api_key_name: str | None
    model_name: str
    provider_name: str | None
    deployment: str | None
    endpoint: str
    stream: bool
    status_code: int
    error_code: str | None
    error_message: str | None
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cost_usd: Money
    cache_hit: bool
    usage_estimated: bool
    saved_usd: Money
    latency_ms: int | None
    ttft_ms: int | None
    client_ip: str | None
    user_agent: str | None


class Totals(BaseModel):
    requests: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Money = Decimal("0")
    cache_hits: int = 0
    saved_usd: Money = Decimal("0")
    avg_latency_ms: int | None = None
    p95_latency_ms: int | None = None


class DayPoint(BaseModel):
    day: str
    model: str
    requests: int
    tokens: int
    cost_usd: Money


class Ranked(BaseModel):
    key: str
    label: str
    requests: int
    tokens: int
    cost_usd: Money
    cache_hits: int = 0


class MonthSpend(BaseModel):
    period: str
    spent_usd: Money
    budget_usd: Money | None
    requests: int
    saved_usd: Money


class SummaryOut(BaseModel):
    days: int
    scope: Literal["me", "all", "user"]
    since: datetime
    totals: Totals
    by_day: list[DayPoint]
    by_model: list[Ranked]
    by_user: list[Ranked]
    month: MonthSpend
    global_month: MonthSpend | None


# --------------------------------------------------------------------------- auditoria
class AuditOut(Out):
    id: int
    ts: datetime
    actor: str
    actor_type: str
    action: str
    resource_type: str
    resource_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    detail: str | None
