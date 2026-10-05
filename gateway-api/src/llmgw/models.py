"""Modelo de dados do gateway (Postgres).

Segredos nunca entram aqui: o provedor guarda só o caminho no Vault e a chave de
API do usuário guarda só o hash SHA-256 do segredo. Prompts e respostas também
não são gravados; o registro de uso tem só metadados, tokens e custo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Valores monetários em USD com 10 casas: um embedding curto custa ~US$ 0,00000004.
Money = Numeric(20, 10)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB, list[str]: JSONB}


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class User(TimestampMixin, Base):
    """Usuário do gateway. Criado no primeiro login (LDAP) ou por um administrador."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(300), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(300))
    external_id: Mapped[str | None] = mapped_column(String(500))  # DN no LDAP
    # Grupos vistos no último login/sincronização: nomes (CN) e DNs completos.
    groups: Mapped[list[str]] = mapped_column(default=list)
    group_dns: Mapped[list[str]] = mapped_column(default=list)
    role: Mapped[str] = mapped_column(String(20), default="user")  # user | admin
    # active | blocked (por um administrador) | disabled (perdeu acesso no LDAP)
    status: Mapped[str] = mapped_column(String(20), default="active")
    status_reason: Mapped[str | None] = mapped_column(String(500))
    monthly_budget_usd: Mapped[Decimal | None] = mapped_column(Money)
    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    tpm_limit: Mapped[int | None] = mapped_column(Integer)
    # None = todos os modelos liberados para os grupos do usuário
    allowed_models: Mapped[list[str] | None] = mapped_column(JSONB)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ldap_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class ApiKey(TimestampMixin, Base):
    """Chave de API de um usuário: `llmgw_<id>.<segredo>`; só o hash do segredo é guardado."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    secret_hash: Mapped[str] = mapped_column(String(64))
    last4: Mapped[str] = mapped_column(String(4))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(300))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    monthly_budget_usd: Mapped[Decimal | None] = mapped_column(Money)
    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    allowed_models: Mapped[list[str] | None] = mapped_column(JSONB)
    # Limites definidos por um administrador: o dono da chave não pode afrouxar.
    limits_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(lazy="joined")


class Provider(TimestampMixin, Base):
    """Conexão com um recurso do Microsoft Foundry. A credencial fica só no Vault."""

    __tablename__ = "providers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30), default="azure_foundry")
    vault_path: Mapped[str] = mapped_column(String(500))
    # True: o segredo foi gravado pelo console. False: provisionado por fora (plataforma).
    managed_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Cópias não sensíveis do segredo, para exibição (atualizadas a cada leitura do Vault)
    endpoint: Mapped[str | None] = mapped_column(String(500))
    auth_type: Mapped[str | None] = mapped_column(String(30))
    discovery: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # ok | error | unknown
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    secret_loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class LlmModel(TimestampMixin, Base):
    """Modelo exposto pelo gateway: um nome público ligado a um deployment do provedor."""

    __tablename__ = "llm_models"
    __table_args__ = (UniqueConstraint("provider_id", "deployment"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    deployment: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="chat")  # chat | embeddings
    upstream_model: Mapped[str | None] = mapped_column(String(200))
    upstream_version: Mapped[str | None] = mapped_column(String(100))
    publisher: Mapped[str | None] = mapped_column(String(100))
    sku: Mapped[str | None] = mapped_column(String(100))
    capabilities: Mapped[dict[str, Any]] = mapped_column(default=dict)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # discovered | manual
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    available: Mapped[bool] = mapped_column(Boolean, default=True)  # ainda existe no provedor
    description: Mapped[str | None] = mapped_column(Text)
    # Preços em USD por 1 milhão de tokens
    input_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    cached_input_price: Mapped[Decimal | None] = mapped_column(Money)
    output_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    cache_ttl_s: Mapped[int | None] = mapped_column(
        Integer
    )  # None = padrão do gateway; 0 = sem cache
    allowed_groups: Mapped[list[str]] = mapped_column(default=list)  # vazio = todos os usuários
    context_window: Mapped[int | None] = mapped_column(Integer)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    supports_responses: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_reasoning: Mapped[bool] = mapped_column(Boolean, default=False)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    provider: Mapped[Provider] = relationship(lazy="joined")


class UsageLog(Base):
    """Uma linha por chamada ao gateway. Sem conteúdo de prompt ou resposta."""

    __tablename__ = "usage_log"
    __table_args__ = (
        Index("ix_usage_ts", "ts"),
        Index("ix_usage_user_ts", "user_id", "ts"),
        Index("ix_usage_model_ts", "model_name", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    request_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    username: Mapped[str] = mapped_column(String(300))
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    api_key_name: Mapped[str | None] = mapped_column(String(120))
    model_name: Mapped[str] = mapped_column(String(200))
    provider_name: Mapped[str | None] = mapped_column(String(120))
    deployment: Mapped[str | None] = mapped_column(String(200))
    endpoint: Mapped[str] = mapped_column(String(30))  # chat | embeddings | responses
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    status_code: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    # Tokens estimados (stream interrompido antes do bloco de uso do Azure).
    usage_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    saved_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    client_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))


class SpendCounter(Base):
    """Acumulado do mês por sujeito (`user:<id>`, `key:<id>` ou `global`), usado no orçamento."""

    __tablename__ = "spend_counters"

    subject: Mapped[str] = mapped_column(String(80), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # AAAA-MM
    cost_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    requests: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_hits: Mapped[int] = mapped_column(BigInteger, default=0)
    saved_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(300))
    actor_type: Mapped[str] = mapped_column(String(20), default="user")  # user | system
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[str | None] = mapped_column(String(100))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    detail: Mapped[str | None] = mapped_column(Text)
