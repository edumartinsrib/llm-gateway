"""Uso, custo e orçamento.

- Extrai os tokens da resposta (chat, embeddings e Responses API).
- Calcula o custo com o preço do modelo (USD por 1 milhão de tokens), cobrando os
  tokens servidos do cache de prompt do Azure pelo preço de entrada em cache.
- Grava uma linha em `usage_log` e soma o mês em `spend_counters` para o usuário,
  a chave e o gateway inteiro, fora do caminho da resposta (tarefa em segundo plano).
- Confere o orçamento antes de cada chamada.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from llmgw.db import sessionmaker
from llmgw.models import ApiKey, SpendCounter, UsageLog, User
from llmgw.services import audit
from llmgw.services.gateway import Route
from llmgw.settings import Settings

log = logging.getLogger(__name__)

MILLION = Decimal(1_000_000)
MAX_PENDING = 10_000
PRECISION = Decimal("0.0000000001")


@dataclass
class Usage:
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Usage:
        data = data or {}
        return cls(**{k: int(data.get(k) or 0) for k in cls().as_dict()})


def _int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def usage_from_openai(raw: dict[str, Any] | None) -> Usage | None:
    """`usage` de chat completions e embeddings."""
    if not isinstance(raw, dict):
        return None
    prompt = _int(raw.get("prompt_tokens"))
    completion = _int(raw.get("completion_tokens"))
    cached = _int((raw.get("prompt_tokens_details") or {}).get("cached_tokens"))
    reasoning = _int((raw.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    total = _int(raw.get("total_tokens")) or prompt + completion
    return Usage(prompt, min(cached, prompt), completion, reasoning, total)


def usage_from_responses(raw: dict[str, Any] | None) -> Usage | None:
    """`usage` da Responses API (input/output em vez de prompt/completion)."""
    if not isinstance(raw, dict):
        return None
    prompt = _int(raw.get("input_tokens"))
    completion = _int(raw.get("output_tokens"))
    cached = _int((raw.get("input_tokens_details") or {}).get("cached_tokens"))
    reasoning = _int((raw.get("output_tokens_details") or {}).get("reasoning_tokens"))
    total = _int(raw.get("total_tokens")) or prompt + completion
    return Usage(prompt, min(cached, prompt), completion, reasoning, total)


def compute_cost(route: Route, usage: Usage) -> Decimal:
    uncached = max(usage.prompt_tokens - usage.cached_tokens, 0)
    cached_price = (
        route.cached_input_price if route.cached_input_price is not None else route.input_price
    )
    cost = (
        Decimal(uncached) * route.input_price
        + Decimal(usage.cached_tokens) * cached_price
        + Decimal(usage.completion_tokens) * route.output_price
    ) / MILLION
    return cost.quantize(PRECISION)


def current_period(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m")


def user_subject(user_id: uuid.UUID | str) -> str:
    return f"user:{user_id}"


def key_subject(key_id: uuid.UUID | str) -> str:
    return f"key:{key_id}"


GLOBAL_SUBJECT = "global"


# --------------------------------------------------------------------------- orçamento
@dataclass
class BudgetCheck:
    allowed: bool
    message: str | None = None
    user_spent: Decimal = Decimal("0")
    user_budget: Decimal | None = None

    @property
    def user_remaining(self) -> Decimal | None:
        if self.user_budget is None:
            return None
        return max(self.user_budget - self.user_spent, Decimal("0"))


async def spent_by_subject(
    session: AsyncSession, subjects: list[str], period: str
) -> dict[str, Decimal]:
    rows = await session.execute(
        select(SpendCounter.subject, SpendCounter.cost_usd).where(
            SpendCounter.subject.in_(subjects), SpendCounter.period == period
        )
    )
    return {subject: cost or Decimal("0") for subject, cost in rows.all()}


async def check_budget(
    session: AsyncSession, settings: Settings, user: User, key: ApiKey | None
) -> BudgetCheck:
    period = current_period(settings.timezone)
    subjects = [user_subject(user.id), GLOBAL_SUBJECT]
    if key is not None:
        subjects.append(key_subject(key.id))
    spent = await spent_by_subject(session, subjects, period)
    user_spent = spent.get(user_subject(user.id), Decimal("0"))
    check = BudgetCheck(True, user_spent=user_spent, user_budget=user.monthly_budget_usd)
    if user.monthly_budget_usd is not None and user_spent >= user.monthly_budget_usd:
        check.allowed = False
        check.message = (
            f"orçamento mensal do usuário esgotado (US$ {user_spent:.2f} de "
            f"US$ {user.monthly_budget_usd:.2f}); fale com o administrador do gateway"
        )
        return check
    if key is not None and key.monthly_budget_usd is not None:
        key_spent = spent.get(key_subject(key.id), Decimal("0"))
        if key_spent >= key.monthly_budget_usd:
            check.allowed = False
            check.message = (
                f"orçamento mensal da chave '{key.name}' esgotado "
                f"(US$ {key_spent:.2f} de US$ {key.monthly_budget_usd:.2f})"
            )
            return check
    if settings.global_monthly_budget_usd is not None:
        total = spent.get(GLOBAL_SUBJECT, Decimal("0"))
        if total >= settings.global_monthly_budget_usd:
            check.allowed = False
            check.message = "orçamento mensal do gateway esgotado; fale com o administrador"
    return check


# --------------------------------------------------------------------------- registro
@dataclass
class UsageEvent:
    request_id: str
    user_id: uuid.UUID
    username: str
    api_key_id: uuid.UUID | None
    api_key_name: str | None
    model_name: str
    provider_name: str | None
    deployment: str | None
    endpoint: str
    stream: bool
    status_code: int
    usage: Usage = field(default_factory=Usage)
    cost_usd: Decimal = Decimal("0")
    cache_hit: bool = False
    saved_usd: Decimal = Decimal("0")
    latency_ms: int | None = None
    ttft_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    user_budget: Decimal | None = None
    usage_estimated: bool = False
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


def _counter_upsert(subject: str, period: str, event: UsageEvent):
    tokens = event.usage.total_tokens
    stmt = insert(SpendCounter).values(
        subject=subject,
        period=period,
        cost_usd=event.cost_usd,
        requests=1,
        tokens=tokens,
        cache_hits=1 if event.cache_hit else 0,
        saved_usd=event.saved_usd,
    )
    excluded = stmt.excluded
    return stmt.on_conflict_do_update(
        index_elements=[SpendCounter.subject, SpendCounter.period],
        set_={
            "cost_usd": SpendCounter.cost_usd + excluded.cost_usd,
            "requests": SpendCounter.requests + excluded.requests,
            "tokens": SpendCounter.tokens + excluded.tokens,
            "cache_hits": SpendCounter.cache_hits + excluded.cache_hits,
            "saved_usd": SpendCounter.saved_usd + excluded.saved_usd,
            "updated_at": datetime.now(UTC),
        },
    ).returning(SpendCounter.cost_usd, SpendCounter.alerted_at)


async def write_usage(settings: Settings, event: UsageEvent) -> None:
    period = current_period(settings.timezone)
    async with sessionmaker()() as session:
        session.add(
            UsageLog(
                ts=event.ts,
                request_id=event.request_id,
                user_id=event.user_id,
                username=event.username,
                api_key_id=event.api_key_id,
                api_key_name=event.api_key_name,
                model_name=event.model_name,
                provider_name=event.provider_name,
                deployment=event.deployment,
                endpoint=event.endpoint,
                stream=event.stream,
                status_code=event.status_code,
                error_code=event.error_code,
                error_message=(event.error_message or None) and event.error_message[:1000],
                prompt_tokens=event.usage.prompt_tokens,
                cached_tokens=event.usage.cached_tokens,
                completion_tokens=event.usage.completion_tokens,
                reasoning_tokens=event.usage.reasoning_tokens,
                total_tokens=event.usage.total_tokens,
                cost_usd=event.cost_usd,
                cache_hit=event.cache_hit,
                usage_estimated=event.usage_estimated,
                saved_usd=event.saved_usd,
                latency_ms=event.latency_ms,
                ttft_ms=event.ttft_ms,
                client_ip=event.client_ip,
                user_agent=(event.user_agent or None) and event.user_agent[:300],
            )
        )
        subjects = [user_subject(event.user_id), GLOBAL_SUBJECT]
        if event.api_key_id is not None:
            subjects.append(key_subject(event.api_key_id))
        # Chamadas recusadas ou com erro sem custo ficam só no histórico: não mexem
        # nos contadores do mês (nem disputam a linha "global").
        if event.status_code >= 400 and event.cost_usd <= 0 and not event.cache_hit:
            subjects = []
        user_total: Decimal | None = None
        alerted = None
        for subject in subjects:
            row = (await session.execute(_counter_upsert(subject, period, event))).one()
            if subject == user_subject(event.user_id):
                user_total, alerted = row
        budget = event.user_budget
        if (
            budget is not None
            and budget > 0
            and alerted is None
            and user_total is not None
            and user_total >= budget * Decimal(settings.budget_alert_pct) / 100
        ):
            counter = await session.get(SpendCounter, (user_subject(event.user_id), period))
            if counter is not None:
                counter.alerted_at = datetime.now(UTC)
            audit.record(
                session,
                actor="system",
                actor_type="system",
                action="budget.alert",
                resource_type="user",
                resource_id=event.user_id,
                detail=(
                    f"{event.username} usou US$ {user_total:.2f} de US$ {budget:.2f} "
                    f"({settings.budget_alert_pct}% do orçamento de {period})"
                ),
            )
        await session.commit()


class UsageRecorder:
    """Grava o uso fora do caminho da resposta; `drain()` espera o que ficou pendente."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._tasks: set[asyncio.Task] = set()
        self._dropped = 0

    def submit(self, event: UsageEvent) -> None:
        self.spawn(self._write(event))

    def spawn(self, coro) -> None:
        """Roda uma tarefa curta em segundo plano (gravação no cache, contadores)."""
        if len(self._tasks) >= MAX_PENDING:
            coro.close()
            self._dropped += 1
            if self._dropped % 1000 == 1:
                log.error("fila de gravação cheia: %d tarefa(s) descartada(s)", self._dropped)
            return
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, event: UsageEvent) -> None:
        for attempt in range(3):
            try:
                await write_usage(self._settings, event)
                return
            except Exception as exc:  # noqa: BLE001 - o uso não pode derrubar a chamada
                if attempt == 2:
                    log.error("falha ao gravar uso da requisição %s: %s", event.request_id, exc)
                    return
                await asyncio.sleep(0.5 * (attempt + 1))

    async def drain(self, wait_s: float = 10) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=wait_s)
