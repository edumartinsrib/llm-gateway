"""Limites por minuto (requisições e tokens) em janela fixa de 60 s.

Com Redis os contadores valem para todas as réplicas; sem Redis, por processo.
Também limita tentativas de login por usuário + IP.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

PREFIX = "llmgw:rl:"


@dataclass
class Hit:
    allowed: bool
    limit: int
    remaining: int
    reset_s: int


def _window(window_s: int) -> tuple[int, int]:
    now = int(time.time())
    index = now // window_s
    return index, window_s - (now % window_s)


class RateLimiter:
    async def _incr(self, key: str, amount: int, ttl_s: int) -> int:
        raise NotImplementedError

    async def _get(self, key: str) -> int:
        raise NotImplementedError

    async def hit(self, name: str, limit: int, window_s: int = 60) -> Hit:
        """Conta uma ocorrência e diz se ainda está dentro do limite (0 = sem limite)."""
        if limit <= 0:
            return Hit(True, 0, 0, 0)
        index, reset = _window(window_s)
        try:
            count = await self._incr(f"{PREFIX}{name}:{index}", 1, window_s * 2)
        except Exception as exc:  # noqa: BLE001 - limite fora do ar não bloqueia o uso
            log.warning("limite indisponível: %s", exc)
            return Hit(True, limit, limit, reset)
        return Hit(count <= limit, limit, max(limit - count, 0), reset)

    async def add(self, name: str, amount: int, window_s: int = 60) -> None:
        if amount <= 0:
            return
        index, _ = _window(window_s)
        try:
            await self._incr(f"{PREFIX}{name}:{index}", amount, window_s * 2)
        except Exception as exc:  # noqa: BLE001
            log.warning("limite indisponível: %s", exc)

    async def used(self, name: str, window_s: int = 60) -> tuple[int, int]:
        index, reset = _window(window_s)
        try:
            return await self._get(f"{PREFIX}{name}:{index}"), reset
        except Exception as exc:  # noqa: BLE001
            log.warning("limite indisponível: %s", exc)
            return 0, reset


class MemoryRateLimiter(RateLimiter):
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, int]] = {}

    def _gc(self) -> None:
        if len(self._data) < 10_000:
            return
        now = time.time()
        for key in [k for k, (exp, _) in self._data.items() if exp < now]:
            self._data.pop(key, None)

    async def _incr(self, key: str, amount: int, ttl_s: int) -> int:
        self._gc()
        expires, count = self._data.get(key, (time.time() + ttl_s, 0))
        count += amount
        self._data[key] = (expires, count)
        return count

    async def _get(self, key: str) -> int:
        item = self._data.get(key)
        return item[1] if item and item[0] >= time.time() else 0


class RedisRateLimiter(RateLimiter):
    def __init__(self, redis) -> None:
        self._redis = redis

    async def _incr(self, key: str, amount: int, ttl_s: int) -> int:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incrby(key, amount)
            pipe.expire(key, ttl_s)
            count, _ = await pipe.execute()
        return int(count)

    async def _get(self, key: str) -> int:
        value = await self._redis.get(key)
        return int(value or 0)


def build_rate_limiter(redis) -> RateLimiter:
    if redis is not None:
        return RedisRateLimiter(redis)
    return MemoryRateLimiter()
