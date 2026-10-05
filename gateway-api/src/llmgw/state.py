"""Dependências de longa duração da aplicação, montadas no lifespan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import Request

if TYPE_CHECKING:
    import httpx

    from llmgw.security.directory import Directory
    from llmgw.security.tokens import SessionIssuer
    from llmgw.services.cache import ResponseCache
    from llmgw.services.events import EventBus
    from llmgw.services.gateway import Gateway
    from llmgw.services.limits import RateLimiter
    from llmgw.services.secrets import SecretStore
    from llmgw.services.usage import UsageRecorder
    from llmgw.settings import Settings


@dataclass
class AppState:
    settings: Settings
    secrets: SecretStore
    bus: EventBus
    gateway: Gateway
    cache: ResponseCache
    limiter: RateLimiter
    usage: UsageRecorder
    sessions: SessionIssuer
    directory: Directory
    http: httpx.AsyncClient
    redis: Any = None


def get_state(request: Request) -> AppState:
    return request.app.state.llmgw
