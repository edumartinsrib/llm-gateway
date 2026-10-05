"""Conversão dos modelos do banco para os esquemas de saída."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from llmgw import models as m
from llmgw import schemas as s
from llmgw.security.tokens import display_key
from llmgw.services.groups import GroupRule
from llmgw.settings import Settings


def key_status(key: m.ApiKey) -> str:
    if key.revoked_at is not None:
        return "revoked"
    if key.expires_at is not None and key.expires_at <= datetime.now(UTC):
        return "expired"
    return "active"


def key_out(key: m.ApiKey, spent: Decimal = Decimal("0")) -> s.KeyOut:
    return s.KeyOut(
        id=str(key.id),
        name=key.name,
        display=display_key(key.id, key.last4),
        user_id=str(key.user_id),
        username=key.user.username,
        status=key_status(key),  # type: ignore[arg-type]
        created_at=key.created_at,
        expires_at=key.expires_at,
        revoked_at=key.revoked_at,
        revoked_by=key.revoked_by,
        last_used_at=key.last_used_at,
        monthly_budget_usd=key.monthly_budget_usd,
        spent_month_usd=spent,
        rpm_limit=key.rpm_limit,
        allowed_models=key.allowed_models,
        limits_locked=key.limits_locked,
    )


def effective_ttl(settings: Settings, model: m.LlmModel) -> int:
    if not settings.cache_enabled:
        return 0
    if model.cache_ttl_s is not None:
        return model.cache_ttl_s
    return (
        settings.cache_embeddings_ttl_s if model.kind == "embeddings" else settings.cache_chat_ttl_s
    )


def model_accessible(model: m.LlmModel, user: m.User, is_admin: bool) -> bool:
    rule = GroupRule(model.allowed_groups or [])
    if rule and not is_admin and not rule.matches(user.groups or [], user.group_dns or []):
        return False
    return user.allowed_models is None or model.name in user.allowed_models


def model_out(
    model: m.LlmModel, settings: Settings, *, routable: bool, accessible: bool
) -> s.ModelOut:
    return s.ModelOut(
        id=str(model.id),
        name=model.name,
        provider_id=str(model.provider_id),
        provider_name=model.provider.name,
        deployment=model.deployment,
        kind=model.kind,
        upstream_model=model.upstream_model,
        upstream_version=model.upstream_version,
        publisher=model.publisher,
        sku=model.sku,
        source=model.source,
        enabled=model.enabled,
        available=model.available,
        routable=routable,
        accessible=accessible,
        description=model.description,
        input_price=model.input_price or Decimal("0"),
        cached_input_price=model.cached_input_price,
        output_price=model.output_price or Decimal("0"),
        cache_ttl_s=model.cache_ttl_s,
        effective_cache_ttl_s=effective_ttl(settings, model),
        allowed_groups=list(model.allowed_groups or []),
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        supports_responses=model.supports_responses,
        supports_vision=model.supports_vision,
        supports_reasoning=model.supports_reasoning,
        discovered_at=model.discovered_at,
        last_seen_at=model.last_seen_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def usage_out(row: m.UsageLog) -> s.UsageOut:
    return s.UsageOut(
        id=row.id,
        ts=row.ts,
        request_id=row.request_id,
        user_id=str(row.user_id) if row.user_id else None,
        username=row.username,
        api_key_id=str(row.api_key_id) if row.api_key_id else None,
        api_key_name=row.api_key_name,
        model_name=row.model_name,
        provider_name=row.provider_name,
        deployment=row.deployment,
        endpoint=row.endpoint,
        stream=row.stream,
        status_code=row.status_code,
        error_code=row.error_code,
        error_message=row.error_message,
        prompt_tokens=row.prompt_tokens,
        cached_tokens=row.cached_tokens,
        completion_tokens=row.completion_tokens,
        reasoning_tokens=row.reasoning_tokens,
        total_tokens=row.total_tokens,
        cost_usd=row.cost_usd or Decimal("0"),
        cache_hit=row.cache_hit,
        usage_estimated=row.usage_estimated,
        saved_usd=row.saved_usd or Decimal("0"),
        latency_ms=row.latency_ms,
        ttft_ms=row.ttft_ms,
        client_ip=row.client_ip,
        user_agent=row.user_agent,
    )
