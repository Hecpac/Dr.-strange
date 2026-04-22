from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from claw_v2.idempotency import IdempotencyInProgress, IdempotencyStore, idempotent
from claw_v2.memory import MemoryStore


def _store(tmp_path: Path) -> IdempotencyStore:
    db_path = tmp_path / "claw.db"
    MemoryStore(db_path)
    return IdempotencyStore(db_path)


def test_idempotent_sync_call_returns_stored_result_without_reexecution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []

    @idempotent(store=store, key_fn=lambda value: f"telegram:{value}")
    def send_message(value: str) -> dict[str, str]:
        calls.append(value)
        return {"message_id": value}

    first = send_message("123")
    second = send_message("123")

    assert first == {"message_id": "123"}
    assert second == {"message_id": "123"}
    assert calls == ["123"]


def test_idempotent_async_call_returns_stored_result_without_reexecution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []

    @idempotent(store=store, key_fn=lambda value: f"github:{value}")
    async def create_pr(value: str) -> dict[str, str]:
        calls.append(value)
        return {"pr_url": value}

    async def run() -> tuple[dict[str, str], dict[str, str]]:
        return await create_pr("1"), await create_pr("1")

    first, second = asyncio.run(run())

    assert first == {"pr_url": "1"}
    assert second == {"pr_url": "1"}
    assert calls == ["1"]


def test_duplicate_running_key_fails_closed_without_executing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    assert store.reserve("terminal:cmd") == ("reserved", None)

    @idempotent(store=store, key_fn=lambda: "terminal:cmd")
    def run_terminal() -> str:
        calls.append("executed")
        return "ok"

    with pytest.raises(IdempotencyInProgress):
        run_terminal()

    assert calls == []
