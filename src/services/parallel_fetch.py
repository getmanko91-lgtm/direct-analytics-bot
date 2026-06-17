from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

T = TypeVar("T")
R = TypeVar("R")

# Параллельные запросы к разным логинам Директа; не ставить слишком высоко из‑за лимитов API.
DEFAULT_MAX_WORKERS = 5


def map_parallel(
    func: Callable[[T], R],
    items: list[T],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> list[R]:
    if not items:
        return []
    if len(items) == 1:
        return [func(items[0])]
    workers = min(max_workers, len(items))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(func, items))
