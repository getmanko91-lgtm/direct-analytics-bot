"""Готовые отчёты вкладок на диске: перезапуск не запускает сбор заново."""

from __future__ import annotations

import hashlib
import pickle
import threading
import time
from pathlib import Path

from src.db.database import DATA_DIR

RESULT_TTL_SECONDS = 6 * 60 * 60
_LOCK = threading.Lock()


def load_finished(kind: str, key: tuple, *, ttl_seconds: int = RESULT_TTL_SECONDS):
    with _LOCK:
        payload = _read(_path(kind, key, "done"))
    if payload is None:
        return None
    finished_at, cabinets = payload
    if time.time() - float(finished_at) > ttl_seconds:
        clear(kind, key)
        return None
    return float(finished_at), list(cabinets)


def save_finished(kind: str, key: tuple, cabinets: list, finished_at: float) -> None:
    with _LOCK:
        _write(_path(kind, key, "done"), (finished_at, list(cabinets)))
        _path(kind, key, "partial").unlink(missing_ok=True)


def load_partial(kind: str, key: tuple, *, ttl_seconds: int = RESULT_TTL_SECONDS) -> dict[int, object]:
    with _LOCK:
        payload = _read(_path(kind, key, "partial"))
        if payload is None:
            return {}
        saved_at, cabinets = payload
        if time.time() - float(saved_at) > ttl_seconds:
            _path(kind, key, "partial").unlink(missing_ok=True)
            return {}
        return {int(client_id): cabinet for client_id, cabinet in cabinets.items()}


def remember_partial(kind: str, key: tuple, client_id: int, cabinet: object) -> None:
    with _LOCK:
        path = _path(kind, key, "partial")
        payload = _read(path)
        cabinets = {} if payload is None else dict(payload[1])
        cabinets[int(client_id)] = cabinet
        _write(path, (time.time(), cabinets))


def begin_collection(
    kind: str,
    key: tuple,
    *,
    force: bool,
    ttl_seconds: int = RESULT_TTL_SECONDS,
):
    """Готовый отчёт или уже собранные кабинеты. force стирает сохранённое."""
    if force:
        clear(kind, key)
        return None, {}
    finished = load_finished(kind, key, ttl_seconds=ttl_seconds)
    if finished is not None:
        return finished, {}
    return None, load_partial(kind, key, ttl_seconds=ttl_seconds)


def clear(kind: str, key: tuple) -> None:
    with _LOCK:
        for suffix in ("done", "partial"):
            _path(kind, key, suffix).unlink(missing_ok=True)


def _path(kind: str, key: tuple, suffix: str) -> Path:
    folder = DATA_DIR / "report_cache"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(repr((kind, key)).encode("utf-8")).hexdigest()
    return folder / f"{kind}-{digest}.{suffix}"


def _read(path: Path):
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            return pickle.load(handle)
    except (OSError, pickle.UnpicklingError, EOFError):
        path.unlink(missing_ok=True)
        return None


def _write(path: Path, payload) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    temporary.replace(path)
