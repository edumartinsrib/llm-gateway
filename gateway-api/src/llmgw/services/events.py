"""Avisos entre réplicas da API (pub/sub).

Quando um administrador muda um modelo ou provedor, todas as réplicas recarregam
a tabela de rotas. Com Redis o aviso chega a todas; sem Redis fica só nesta
réplica — suficiente para desenvolvimento e testes. Cada réplica também
recarrega sozinha a cada `LLMGW_PROVIDER_REFRESH_S`, então um aviso perdido só
atrasa a mudança.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)

CONFIG_CHANNEL = "llmgw:config"


class EventBus:
    async def publish(self, channel: str, event: str, data: Any = None) -> None:
        raise NotImplementedError

    def subscribe(self, *channels: str) -> AsyncIterator[tuple[str, Any]]:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class MemoryEventBus(EventBus):
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    async def publish(self, channel: str, event: str, data: Any = None) -> None:
        for queue in list(self._subscribers.get(channel, ())):
            if queue.qsize() < 1000:
                queue.put_nowait((event, data))

    async def subscribe(self, *channels: str) -> AsyncIterator[tuple[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue()
        for ch in channels:
            self._subscribers[ch].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            for ch in channels:
                self._subscribers[ch].discard(queue)


class RedisEventBus(EventBus):
    def __init__(self, redis) -> None:
        self._redis = redis

    async def publish(self, channel: str, event: str, data: Any = None) -> None:
        message = json.dumps({"event": event, "data": data}, default=str)
        await self._redis.publish(channel, message)

    async def subscribe(self, *channels: str) -> AsyncIterator[tuple[str, Any]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(*channels)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    body = json.loads(message["data"])
                except (TypeError, json.JSONDecodeError):
                    continue
                yield body.get("event", ""), body.get("data")
        finally:
            await pubsub.unsubscribe(*channels)
            await pubsub.aclose()


def build_event_bus(redis) -> EventBus:
    if redis is not None:
        return RedisEventBus(redis)
    log.warning("Redis não configurado: avisos de configuração só nesta réplica")
    return MemoryEventBus()
