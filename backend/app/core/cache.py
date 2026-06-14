"""Tiny Redis-backed response cache for read-heavy endpoints.

Targets endpoints that:
  - read the same data thousands of times per minute
  - change rarely (config, plan catalog, public marketing)
  - have a stable cache key from path + query params

Usage:

    from app.core.cache import redis_cached

    @router.get("/api/domains/config", response_model=DomainConfig)
    @redis_cached(ttl=60, key="domain-config:{host}")
    async def get_domain_config(host: str, db: DbSession) -> DomainConfig:
        ...

  - `ttl` is seconds.
  - `key` is a python-format-string evaluated against kwargs after FastAPI's
    parameter binding. Body params, DbSession etc. are skipped automatically.
  - Cached value is the JSON-serialized response model.

Fail-open: if Redis is down or the value is corrupt, the handler runs as
if there was no cache. Never raises out to the caller.
"""
from __future__ import annotations

import functools

from fastapi import APIRouter
import json
import logging
from typing import Any, Callable

from pydantic import BaseModel

from app.core.redis_client import get_redis

log = logging.getLogger(__name__)

router = APIRouter()


def _serialize(value: Any) -> str | None:
    """Best-effort serializer for cacheable values. Skip when not pydantic
    or plain JSON — keeps the cache simple (no pickle, no surprises)."""
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return None


def redis_cached(*, ttl: int, key: str):
    """Cache a handler's return value in Redis for `ttl` seconds.

    `key` may reference any kwarg the handler accepts via {name}.
    """
    def decorator(fn: Callable):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                rkey = "cache:" + key.format(**{k: v for k, v in kwargs.items() if isinstance(v, (str, int, float, bool))})
            except KeyError:
                # Bad template — fall through to live call rather than crash.
                return await fn(*args, **kwargs)

            client = get_redis()
            try:
                cached = await client.get(rkey)
            except Exception as e:  # noqa: BLE001
                log.warning("redis get %s failed: %s", rkey, e)
                cached = None

            if cached:
                try:
                    return json.loads(cached)
                except (TypeError, ValueError):
                    log.warning("corrupt cache %s, ignoring", rkey)

            value = await fn(*args, **kwargs)
            payload = _serialize(value)
            if payload:
                try:
                    await client.setex(rkey, ttl, payload)
                except Exception as e:  # noqa: BLE001
                    log.warning("redis setex %s failed: %s", rkey, e)
            return value

        return wrapper

    return decorator


async def invalidate(pattern: str) -> int:
    """Delete cache keys matching a glob (e.g. "cache:domain-config:*").
    Use sparingly — SCAN+DEL on large keyspaces is O(n). Returns count.
    """
    client = get_redis()
    deleted = 0
    try:
        async for key in client.scan_iter(match=pattern):
            await client.delete(key)
            deleted += 1
    except Exception as e:  # noqa: BLE001
        log.warning("invalidate %s failed: %s", pattern, e)
    return deleted
