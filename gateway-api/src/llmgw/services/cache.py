"""Cache de respostas (correspondência exata da requisição).

A chave é o SHA-256 do corpo canônico (JSON com chaves ordenadas) + rota + modelo
+ escopo (`user`: cada usuário tem o seu cache; `global`: compartilhado). Campos
que não mudam a resposta (`user`, `metadata`, `stream_options`) ficam de fora.

Respostas em streaming são guardadas como a sequência de eventos SSE e
reproduzidas na mesma ordem. Um acerto não chama o Azure, custa zero e registra
quanto foi economizado.

O cliente controla por requisição com `Cache-Control: no-cache` (não lê, mas
grava), `Cache-Control: no-store` (nem lê nem grava) ou `X-LLMGW-Cache: off`.
Com Redis o cache é compartilhado entre réplicas; sem Redis fica em memória (LRU).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

IGNORED_FIELDS = {"user", "metadata", "stream_options"}
KEY_PREFIX = "llmgw:cache:"


@dataclass
class CachedResponse:
    kind: str  # json | sse
    status: int
    usage: dict[str, int]
    cost_usd: str
    body: Any = None
    # (bloco SSE já formatado, se é o bloco só de uso)
    events: list[tuple[str, bool]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def dumps(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def loads(cls, raw: str | bytes) -> CachedResponse:
        data = json.loads(raw)
        data["events"] = [tuple(e) for e in data.get("events") or []]
        return cls(**data)


def make_key(scope: str, endpoint: str, model: str, body: dict[str, Any]) -> str:
    canonical = {k: v for k, v in body.items() if k not in IGNORED_FIELDS}
    raw = json.dumps(
        {"s": scope, "e": endpoint, "m": model, "b": canonical},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


class ResponseCache:
    async def get(self, key: str) -> CachedResponse | None:
        raise NotImplementedError

    async def set(self, key: str, value: CachedResponse, ttl_s: int) -> None:
        raise NotImplementedError

    async def clear(self) -> int:
        raise NotImplementedError


class MemoryCache(ResponseCache):
    def __init__(self, max_entries: int = 5000):
        self._max = max_entries
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def get(self, key: str) -> CachedResponse | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, raw = item
        if expires < time.time():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return CachedResponse.loads(raw)

    async def set(self, key: str, value: CachedResponse, ttl_s: int) -> None:
        self._data[key] = (time.time() + ttl_s, value.dumps())
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    async def clear(self) -> int:
        count = len(self._data)
        self._data.clear()
        return count


class RedisCache(ResponseCache):
    def __init__(self, redis):
        self._redis = redis

    async def get(self, key: str) -> CachedResponse | None:
        try:
            raw = await self._redis.get(KEY_PREFIX + key)
        except Exception as exc:  # noqa: BLE001 - cache fora do ar não derruba a chamada
            log.warning("cache indisponível: %s", exc)
            return None
        return CachedResponse.loads(raw) if raw else None

    async def set(self, key: str, value: CachedResponse, ttl_s: int) -> None:
        try:
            await self._redis.set(KEY_PREFIX + key, value.dumps(), ex=ttl_s)
        except Exception as exc:  # noqa: BLE001
            log.warning("não foi possível gravar no cache: %s", exc)

    async def clear(self) -> int:
        count = 0
        async for key in self._redis.scan_iter(match=KEY_PREFIX + "*", count=500):
            await self._redis.delete(key)
            count += 1
        return count


def build_cache(redis, max_entries: int) -> ResponseCache:
    if redis is not None:
        return RedisCache(redis)
    log.warning("Redis não configurado: cache de respostas só em memória nesta réplica")
    return MemoryCache(max_entries)
