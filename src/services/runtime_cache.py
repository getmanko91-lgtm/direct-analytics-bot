from __future__ import annotations

import time
from threading import Lock
from typing import Callable, TypeVar

T = TypeVar("T")

_CACHE: dict[tuple, tuple[float, object]] = {}
_LOCK = Lock()


def get_or_set(key: tuple, producer: Callable[[], T], ttl_seconds: int) -> T:
    now = time.time()
    with _LOCK:
        item = _CACHE.get(key)
        if item and item[0] > now:
            return item[1]  # type: ignore[return-value]

    value = producer()
    expires_at = now + ttl_seconds
    with _LOCK:
        _CACHE[key] = (expires_at, value)
    return value


def invalidate_prefix(prefix: tuple) -> None:
    with _LOCK:
        for key in list(_CACHE.keys()):
            if key[: len(prefix)] == prefix:
                _CACHE.pop(key, None)
