from __future__ import annotations

import builtins
import importlib
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest, LLMResponse
from claw_v2.observe import ObserveStream


@contextmanager
def _fresh_modules(*names: str):
    saved = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    saved_parent_attrs: dict[tuple[str, str], object] = {}
    missing = object()
    for name in names:
        if "." not in name:
            continue
        parent_name, _, attr = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            saved_parent_attrs[(parent_name, attr)] = getattr(parent, attr, missing)
            if hasattr(parent, attr):
                delattr(parent, attr)
    try:
        yield
    finally:
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(saved)
        for (parent_name, attr), value in saved_parent_attrs.items():
            parent = sys.modules.get(parent_name)
            if parent is None:
                continue
            if value is missing:
                if hasattr(parent, attr):
                    delattr(parent, attr)
            else:
                setattr(parent, attr, value)


@contextmanager
def _pyautogui_unavailable():
    real_import = builtins.__import__
    saved_pyautogui = sys.modules.pop("pyautogui", None)

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pyautogui" or name.startswith("pyautogui."):
            raise ModuleNotFoundError("No module named 'pyautogui'")
        return real_import(name, globals, locals, fromlist, level)

    try:
        with patch("builtins.__import__", side_effect=guarded_import):
            yield
    finally:
        if saved_pyautogui is not None:
            sys.modules["pyautogui"] = saved_pyautogui


def _fake_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(content="<response>ok</response>", lane=request.lane, provider="anthropic")


def _runtime_env(root: Path, **overrides: str) -> dict[str, str]:
    env = {
        "DB_PATH": str(root / "data" / "claw.db"),
        "WORKSPACE_ROOT": str(root / "workspace"),
        "AGENT_STATE_ROOT": str(root / "agents"),
        "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
        "APPROVALS_ROOT": str(root / "approvals"),
        "PIPELINE_STATE_ROOT": str(root / "pipeline"),
        "EVAL_ON_SELF_IMPROVE": "false",
        "CHROME_CDP_ENABLED": "false",
        "CLAW_AUTONOMOUS_MAINTENANCE": "false",
        "CLAW_AUTONOMOUS_MAINTENANCE_ENABLED": "false",
    }
    env.update(overrides)
    return env


class ComputerImportSafetyTests(unittest.TestCase):
    def test_import_computer_does_not_require_pyautogui(self) -> None:
        with _fresh_modules("claw_v2.computer", "pyautogui"), _pyautogui_unavailable():
            module = importlib.import_module("claw_v2.computer")

        self.assertTrue(hasattr(module, "ComputerUseService"))

    def test_import_main_does_not_require_pyautogui_or_display(self) -> None:
        with (
            _fresh_modules("claw_v2.main", "claw_v2.computer", "pyautogui"),
            _pyautogui_unavailable(),
        ):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DISPLAY", None)
                module = importlib.import_module("claw_v2.main")

        self.assertTrue(hasattr(module, "build_runtime"))

    def test_load_pyautogui_sets_runtime_safety_options(self) -> None:
        from claw_v2 import computer

        fake = SimpleNamespace(FAILSAFE=False, PAUSE=0.0)
        with (
            patch.dict(sys.modules, {"pyautogui": fake}),
            patch.object(computer, "pyautogui", None),
        ):
            loaded = computer._load_pyautogui()

        self.assertIs(loaded, fake)
        self.assertTrue(fake.FAILSAFE)
        self.assertEqual(fake.PAUSE, 0.1)

    def test_disabled_computer_use_does_not_import_or_construct_pyautogui_backend(self) -> None:
        from claw_v2 import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = _runtime_env(root, COMPUTER_USE_ENABLED="false")
            with patch.dict(os.environ, env, clear=False), _pyautogui_unavailable():
                with patch.object(main, "ComputerUseService") as ctor:
                    runtime = main.build_runtime(anthropic_executor=_fake_anthropic)

        ctor.assert_not_called()
        self.assertIsNone(runtime.bot.computer)
        event_types = [event["event_type"] for event in runtime.observe.recent_events(limit=20)]
        self.assertIn("computer_use_disabled", event_types)

    def test_required_computer_use_fails_explicitly_without_pyautogui_backend(self) -> None:
        from claw_v2 import main
        from claw_v2.computer import ComputerUseUnavailable

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "data" / "claw.db"
            env = _runtime_env(
                root,
                COMPUTER_USE_ENABLED="true",
                COMPUTER_USE_REQUIRED="true",
                COMPUTER_USE_BACKEND="openai",
                OPENAI_API_KEY="test-key",
            )
            with patch.dict(os.environ, env, clear=False):
                with patch.object(
                    main, "_probe_pyautogui_display", side_effect=ComputerUseUnavailable("missing")
                ):
                    with self.assertRaisesRegex(RuntimeError, "computer_display"):
                        main.build_runtime(anthropic_executor=_fake_anthropic)

            observe = ObserveStream(db_path)
            events = observe.recent_events(limit=10, event_type="computer_use_required_failed")

        self.assertTrue(events)
        self.assertEqual(events[0]["payload"]["backend"], "openai")

    def test_codex_backend_does_not_probe_pyautogui(self) -> None:
        from claw_v2 import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = _runtime_env(
                root,
                COMPUTER_USE_ENABLED="true",
                COMPUTER_USE_BACKEND="codex",
                CODEX_CLI_PATH="missing-codex-for-test",
            )
            with patch.dict(os.environ, env, clear=False):
                with patch.object(
                    main, "_probe_pyautogui_display", side_effect=AssertionError("unexpected probe")
                ):
                    runtime = main.build_runtime(anthropic_executor=_fake_anthropic)

        self.assertIsNotNone(runtime.bot.computer)
        self.assertIsNotNone(runtime.bot.computer.codex_backend)

    def test_pyautogui_probe_times_out(self) -> None:
        from claw_v2.main import _probe_pyautogui_display

        class HangingPyAutoGUI:
            def size(self):
                time.sleep(1.0)
                return SimpleNamespace(width=1280, height=800)

        with patch("claw_v2.main._load_pyautogui", return_value=HangingPyAutoGUI()):
            with self.assertRaisesRegex(TimeoutError, "timed out"):
                _probe_pyautogui_display(timeout_s=0.01)
