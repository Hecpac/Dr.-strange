from __future__ import annotations

import functools
import inspect
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar


class IdempotencyInProgress(RuntimeError):
    """Raised when a duplicate operation finds an uncompleted reservation."""


_T = TypeVar("_T")


class IdempotencyStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()

    def reserve(self, key: str) -> tuple[str, Any | None]:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO idempotency_keys (key, status) VALUES (?, 'running')",
                    (key,),
                )
                self._conn.commit()
                return "reserved", None
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT status, result FROM idempotency_keys WHERE key = ?",
                    (key,),
                ).fetchone()
        if row is None:
            raise IdempotencyInProgress(f"idempotency key disappeared: {key}")
        if row["status"] == "completed":
            return "completed", json.loads(row["result"]) if row["result"] else None
        raise IdempotencyInProgress(f"idempotency key is already running: {key}")

    def complete(self, key: str, result: Any) -> None:
        encoded = json.dumps(result, default=str)
        with self._lock:
            self._conn.execute(
                """
                UPDATE idempotency_keys
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP, result = ?
                WHERE key = ?
                """,
                (encoded, key),
            )
            self._conn.commit()


def idempotent(
    *,
    store: IdempotencyStore,
    key_fn: Callable[..., str],
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    def _decorate(fn: Callable[..., _T]) -> Callable[..., _T]:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                key = key_fn(*args, **kwargs)
                status, stored = store.reserve(key)
                if status == "completed":
                    return stored
                result = await fn(*args, **kwargs)
                store.complete(key, result)
                return result

            return _async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            key = key_fn(*args, **kwargs)
            status, stored = store.reserve(key)
            if status == "completed":
                return stored
            result = fn(*args, **kwargs)
            store.complete(key, result)
            return result

        return _sync_wrapper  # type: ignore[return-value]

    return _decorate
