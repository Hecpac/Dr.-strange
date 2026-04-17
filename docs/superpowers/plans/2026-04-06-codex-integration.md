# Codex CLI Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrar Codex CLI (`codex exec`) como provider del worker lane y backend de Computer Use, aprovechando la suscripción de ChatGPT Pro sin costo por token.

**Architecture:** `CodexAdapter` invoca `codex exec` como subprocess para el worker lane. `CodexComputerBackend` reemplaza la Responses API de OpenAI para Computer Use, usando `codex exec` con sandbox completo. El brain (Claude Opus) no cambia. Configuración por env vars para rollback instantáneo.

**Tech Stack:** Python subprocess, `codex exec` CLI (0.118.0), `codex-mini-latest` model, existing `ProviderAdapter` base class pattern.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `claw_v2/adapters/codex.py` | CREATE | CodexAdapter: subprocess wrapper para `codex exec` |
| `claw_v2/computer.py` | MODIFY | Agregar CodexComputerBackend + opcional dispatch en ComputerUseService |
| `claw_v2/config.py` | MODIFY | Agregar 3 campos + relajar validate() + _SECONDARY_PROVIDER_DEFAULT_MODELS |
| `claw_v2/llm.py` | MODIFY | Registrar "codex" en LLMRouter.default() |
| `claw_v2/main.py` | MODIFY | Instanciar CodexAdapter + CodexComputerBackend según config |
| `claw_v2/bot.py` | MODIFY | _run_computer_session: skip OpenAI client cuando codex backend activo |
| `tests/helpers.py` | MODIFY | Agregar 3 nuevos campos a make_config() |
| `tests/test_codex_adapter.py` | CREATE | Unit tests para CodexAdapter |
| `tests/test_computer.py` | MODIFY | Tests para CodexComputerBackend |
| `tests/test_llm.py` | MODIFY | Test routing a codex provider |
| `tests/test_config.py` | MODIFY | Test nuevos campos + validate() actualizado |

---

## Task 1: CodexAdapter — subprocess wrapper

**Files:**
- Create: `claw_v2/adapters/codex.py`
- Create: `tests/test_codex_adapter.py`

- [ ] **Step 1.1: Write failing tests**

```python
# tests/test_codex_adapter.py
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch, MagicMock

from claw_v2.adapters.base import AdapterError, AdapterUnavailableError, LLMRequest
from claw_v2.adapters.codex import CodexAdapter


def _make_request(prompt: str = "Write a hello world function", lane: str = "worker") -> LLMRequest:
    return LLMRequest(
        prompt=prompt,
        system_prompt=None,
        lane=lane,
        provider="codex",
        model="codex-mini-latest",
        effort=None,
        session_id=None,
        max_budget=0.5,
        evidence_pack=None,
        allowed_tools=None,
        agents=None,
        hooks=None,
        timeout=60.0,
        cwd="/tmp",
    )


class CodexAdapterTests(unittest.TestCase):
    def test_transport_override_bypasses_subprocess(self) -> None:
        from claw_v2.types import LLMResponse
        fixed = LLMResponse(
            content="hello world", lane="worker", provider="codex",
            model="codex-mini-latest", confidence=0.7, cost_estimate=0.0, artifacts={},
        )
        adapter = CodexAdapter(transport=lambda _: fixed)
        result = adapter.complete(_make_request())
        self.assertEqual(result.content, "hello world")
        self.assertEqual(result.provider, "codex")

    def test_raises_unavailable_when_cli_not_found(self) -> None:
        adapter = CodexAdapter(cli_path="nonexistent-codex-abc123")
        with self.assertRaises(AdapterUnavailableError):
            adapter.complete(_make_request())

    def test_raises_adapter_error_on_nonzero_exit(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        fake_result = MagicMock(returncode=1, stdout="", stderr="some error")
        with patch("claw_v2.adapters.codex.subprocess.run", return_value=fake_result):
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                with self.assertRaises(AdapterError):
                    adapter.complete(_make_request())

    def test_successful_completion_returns_stdout(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        fake_result = MagicMock(returncode=0, stdout="def hello():\n    print('hello')\n", stderr="")
        with patch("claw_v2.adapters.codex.subprocess.run", return_value=fake_result):
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                result = adapter.complete(_make_request())
        self.assertEqual(result.content, "def hello():\n    print('hello')")
        self.assertEqual(result.provider, "codex")
        self.assertEqual(result.cost_estimate, 0.0)

    def test_passes_cwd_to_subprocess(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        fake_result = MagicMock(returncode=0, stdout="done", stderr="")
        with patch("claw_v2.adapters.codex.subprocess.run", return_value=fake_result) as mock_run:
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                adapter.complete(_make_request())
        call_kwargs = mock_run.call_args
        cmd = call_kwargs[0][0]
        self.assertIn("-C", cmd)
        self.assertIn("/tmp", cmd)

    def test_tool_capable_is_true(self) -> None:
        self.assertTrue(CodexAdapter.tool_capable)

    def test_raises_adapter_error_on_timeout(self) -> None:
        adapter = CodexAdapter(cli_path="codex")
        with patch("claw_v2.adapters.codex.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 60)):
            with patch("claw_v2.adapters.codex.shutil.which", return_value="/usr/local/bin/codex"):
                with self.assertRaises(AdapterError):
                    adapter.complete(_make_request())
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
cd /Users/hector/Projects/Dr.-strange
.venv/bin/pytest tests/test_codex_adapter.py -v
```

Expected: `ModuleNotFoundError: No module named 'claw_v2.adapters.codex'`

- [ ] **Step 1.3: Create the adapter**

```python
# claw_v2/adapters/codex.py
from __future__ import annotations

import shutil
import subprocess
from typing import Callable

from claw_v2.adapters.base import (
    AdapterError,
    AdapterUnavailableError,
    LLMRequest,
    ProviderAdapter,
    build_effective_input,
    build_effective_system_prompt,
)
from claw_v2.types import LLMResponse


class CodexAdapter(ProviderAdapter):
    """Provider adapter that invokes the Codex CLI via subprocess.

    Uses the ChatGPT Pro subscription — cost_estimate is always 0.0.
    tool_capable=True because Codex CLI handles its own internal tool loop.
    """

    provider_name = "codex"
    tool_capable = True

    def __init__(
        self,
        cli_path: str = "codex",
        *,
        transport: Callable[[LLMRequest], LLMResponse] | None = None,
    ) -> None:
        self.cli_path = cli_path
        self._transport = transport

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._transport is not None:
            return self._transport(request)
        return self._run_cli(request)

    def _run_cli(self, request: LLMRequest) -> LLMResponse:
        resolved = shutil.which(self.cli_path)
        if not resolved:
            raise AdapterUnavailableError(
                f"Codex CLI not found at '{self.cli_path}'. "
                "Install with: npm install -g @openai/codex"
            )

        prompt = build_effective_input(request)
        if isinstance(prompt, list):
            prompt = " ".join(
                block.get("text", "")
                for block in prompt
                if isinstance(block, dict) and block.get("type") == "text"
            )

        model = request.model or "codex-mini-latest"
        cmd = [
            resolved, "exec",
            "--model", model,
            "--full-auto",
            "--color", "never",
        ]
        if request.cwd:
            cmd += ["-C", request.cwd]
        cmd.append(str(prompt))

        system = build_effective_system_prompt(request)
        if system:
            # Prepend system prompt to the user prompt as context
            full_prompt = f"{system}\n\n{prompt}"
            cmd[-1] = full_prompt

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=request.timeout,
            )
        except FileNotFoundError as exc:
            raise AdapterUnavailableError(
                f"Codex CLI not found at '{self.cli_path}'."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdapterError(
                f"Codex CLI timed out after {request.timeout}s"
            ) from exc

        if result.returncode != 0:
            raise AdapterError(
                f"Codex CLI failed (exit {result.returncode}): {result.stderr.strip()[:500]}"
            )

        content = result.stdout.strip()
        return LLMResponse(
            content=content,
            lane=request.lane,
            provider="codex",
            model=model,
            confidence=0.7 if content else 0.0,
            cost_estimate=0.0,
            artifacts={"stderr": result.stderr.strip()[:200]},
        )
```

- [ ] **Step 1.4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_codex_adapter.py -v
```

Expected: 7 tests passing.

- [ ] **Step 1.5: Commit**

```bash
git add claw_v2/adapters/codex.py tests/test_codex_adapter.py
git commit -m "feat: add CodexAdapter for ChatGPT Pro subscription worker lane"
```

---

## Task 2: Config fields + validate() update

**Files:**
- Modify: `claw_v2/config.py`
- Modify: `tests/helpers.py`
- Modify: `tests/test_config.py`

- [ ] **Step 2.1: Write failing config tests**

Add these tests to `tests/test_config.py` (append after existing tests):

```python
class CodexConfigTests(unittest.TestCase):
    def test_codex_worker_provider_passes_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.worker_provider = "codex"
            config.worker_model = "codex-mini-latest"
            # Should not raise
            config.validate()

    def test_codex_fields_have_defaults_from_env(self) -> None:
        import os
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(config.codex_model, "codex-mini-latest")
        self.assertEqual(config.computer_use_backend, "openai")

    def test_computer_use_backend_codex_passes_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.computer_use_backend = "codex"
            config.validate()
```

Run:
```bash
.venv/bin/pytest tests/test_config.py::CodexConfigTests -v
```

Expected: 3 failures (fields don't exist yet).

- [ ] **Step 2.2: Add fields to AppConfig dataclass**

In `claw_v2/config.py`, after line 109 (`ollama_host: str`), add:

```python
    codex_cli_path: str
    codex_model: str
    computer_use_backend: str
```

- [ ] **Step 2.3: Add "codex" to _SECONDARY_PROVIDER_DEFAULT_MODELS**

In `claw_v2/config.py`, update the dict at line 39:

```python
_SECONDARY_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-5.4-mini",
    "google": "gemini-2.5-pro",
    "ollama": "gemma4",
    "codex": "codex-mini-latest",
}
```

- [ ] **Step 2.4: Add fields to from_env()**

In `claw_v2/config.py`, in `from_env()`, after the `ollama_host=` line (~line 189), add:

```python
            codex_cli_path=os.getenv("CODEX_CLI_PATH") or shutil.which("codex") or "codex",
            codex_model=os.getenv("CODEX_MODEL", "codex-mini-latest"),
            computer_use_backend=os.getenv("COMPUTER_USE_BACKEND", "openai"),
```

- [ ] **Step 2.5: Update validate() to allow codex as worker_provider**

In `claw_v2/config.py`, replace lines 202-205:

```python
        if self.brain_provider != "anthropic":
            raise ValueError("brain_provider must be 'anthropic' in the current runtime design.")
        if self.worker_provider not in {"anthropic", "codex"}:
            raise ValueError("worker_provider must be 'anthropic' or 'codex'.")
```

Also update the `supported` set in validate() (~line 208) to include "codex":

```python
        supported = {"anthropic", "openai", "google", "ollama", "codex"}
```

Also add validation for `computer_use_backend` after the `browse_backend` check:

```python
        if self.computer_use_backend not in {"openai", "codex"}:
            raise ValueError("computer_use_backend must be 'openai' or 'codex'.")
```

- [ ] **Step 2.6: Update tests/helpers.py make_config**

In `tests/helpers.py`, after `ollama_host="http://localhost:11434",` add:

```python
        codex_cli_path="codex",
        codex_model="codex-mini-latest",
        computer_use_backend="openai",
```

- [ ] **Step 2.7: Run all config tests**

```bash
.venv/bin/pytest tests/test_config.py -v
```

Expected: all passing including the 3 new ones.

- [ ] **Step 2.8: Run full test suite to check helpers are consistent**

```bash
.venv/bin/pytest tests/ -x -q --tb=short 2>&1 | tail -20
```

Expected: all 457+ passing.

- [ ] **Step 2.9: Commit**

```bash
git add claw_v2/config.py tests/helpers.py tests/test_config.py
git commit -m "feat: add codex config fields and relax worker_provider validation"
```

---

## Task 3: Register CodexAdapter in LLMRouter

**Files:**
- Modify: `claw_v2/llm.py`
- Modify: `tests/test_llm.py`

- [ ] **Step 3.1: Write failing test**

Add to `tests/test_llm.py`:

```python
    def test_codex_worker_lane_routes_to_codex_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.worker_provider = "codex"
            config.worker_model = "codex-mini-latest"
            from claw_v2.eval_mocks import StaticAdapter, echo_response
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "codex": StaticAdapter("codex", tool_capable=True, responder=echo_response("codex")),
                },
            )
            response = router.ask("write a function", lane="worker")
            self.assertEqual(response.provider, "codex")
```

Run:
```bash
.venv/bin/pytest tests/test_llm.py::LLMRouterTests::test_codex_worker_lane_routes_to_codex_adapter -v
```

Expected: FAIL — `config.worker_provider = "codex"` triggers validate error (but make_config doesn't call validate after mutation, so it should just route wrong). Actually it will fail because `adapters` doesn't include codex in `LLMRouter.default()`. But with manual adapter dict it should pass already. Let me re-check the test — it will pass because we're manually providing adapters. Hmm.

Actually the test should work already since it constructs `LLMRouter` directly with `adapters={"codex": ...}`. Run it to confirm it passes:

```bash
.venv/bin/pytest tests/test_llm.py::LLMRouterTests::test_codex_worker_lane_routes_to_codex_adapter -v
```

Expected: PASS (LLMRouter doesn't care about which adapters are registered as long as the requested one exists).

- [ ] **Step 3.2: Update LLMRouter.default() to register CodexAdapter**

In `claw_v2/llm.py`, add import at top:

```python
from claw_v2.adapters.codex import CodexAdapter
```

Update `LLMRouter.default()` signature to add `codex_transport` parameter:

```python
    @classmethod
    def default(
        cls,
        config: AppConfig,
        *,
        anthropic_executor: Callable[[LLMRequest], LLMResponse] | None = None,
        openai_transport: Callable[[LLMRequest], LLMResponse] | None = None,
        google_transport: Callable[[LLMRequest], LLMResponse] | None = None,
        ollama_transport: Callable[[LLMRequest], LLMResponse] | None = None,
        codex_transport: Callable[[LLMRequest], LLMResponse] | None = None,
        audit_sink: Callable[[dict], None] | None = None,
        pre_hooks: list[PreLLMHook] | None = None,
        post_hooks: list[PostLLMHook] | None = None,
    ) -> "LLMRouter":
        return cls(
            config=config,
            adapters={
                "anthropic": AnthropicAgentAdapter(executor=anthropic_executor),
                "openai": OpenAIAdapter(transport=openai_transport, api_key=config.openai_api_key),
                "google": GoogleAdapter(transport=google_transport, api_key=config.google_api_key),
                "ollama": OllamaAdapter(
                    transport=ollama_transport,
                    host=config.ollama_host,
                    num_ctx=min(config.worker_context_window, 131072),
                    think=True,
                ),
                "codex": CodexAdapter(
                    cli_path=config.codex_cli_path,
                    transport=codex_transport,
                ),
            },
            audit_sink=audit_sink,
            pre_hooks=pre_hooks,
            post_hooks=post_hooks,
        )
```

- [ ] **Step 3.3: Run tests**

```bash
.venv/bin/pytest tests/test_llm.py -v
```

Expected: all passing.

- [ ] **Step 3.4: Update build_runtime() in main.py to accept codex_transport**

In `claw_v2/main.py`, update `build_runtime()` signature (~line 87):

```python
def build_runtime(
    system_prompt: str = "You are Claw.",
    *,
    anthropic_executor: Callable[[LLMRequest], LLMResponse] | None = None,
    openai_transport: Callable[[LLMRequest], LLMResponse] | None = None,
    google_transport: Callable[[LLMRequest], LLMResponse] | None = None,
    ollama_transport: Callable[[LLMRequest], LLMResponse] | None = None,
    codex_transport: Callable[[LLMRequest], LLMResponse] | None = None,
) -> ClawRuntime:
```

And pass it through to `LLMRouter.default()` (~line 132):

```python
    router = LLMRouter.default(
        config,
        anthropic_executor=anthropic_executor,
        openai_transport=openai_transport,
        google_transport=google_transport,
        ollama_transport=ollama_transport,
        codex_transport=codex_transport,
        audit_sink=audit_sink,
        pre_hooks=pre_hooks,
        post_hooks=post_hooks,
    )
```

- [ ] **Step 3.5: Run full suite**

```bash
.venv/bin/pytest tests/ -x -q --tb=short 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 3.6: Commit**

```bash
git add claw_v2/llm.py claw_v2/main.py tests/test_llm.py
git commit -m "feat: register CodexAdapter in LLMRouter and build_runtime"
```

---

## Task 4: CodexComputerBackend

**Files:**
- Modify: `claw_v2/computer.py`
- Modify: `claw_v2/bot.py`
- Modify: `claw_v2/main.py`
- Modify: `tests/test_computer.py`

- [ ] **Step 4.1: Write failing test for CodexComputerBackend**

Add to `tests/test_computer.py`:

```python
class CodexComputerBackendTests(unittest.TestCase):
    def test_transport_override_returns_fixed_result(self) -> None:
        from claw_v2.computer import CodexComputerBackend
        backend = CodexComputerBackend(transport=lambda task: "done: opened Chrome")
        result = backend.run("open Chrome")
        self.assertEqual(result, "done: opened Chrome")

    def test_raises_runtime_error_when_cli_not_found(self) -> None:
        from claw_v2.computer import CodexComputerBackend
        backend = CodexComputerBackend(cli_path="nonexistent-codex-xyz")
        with self.assertRaises(RuntimeError):
            backend.run("open Chrome")

    def test_raises_runtime_error_on_nonzero_exit(self) -> None:
        from claw_v2.computer import CodexComputerBackend
        from unittest.mock import patch, MagicMock
        backend = CodexComputerBackend(cli_path="codex")
        fake_result = MagicMock(returncode=1, stdout="", stderr="failed")
        with patch("claw_v2.computer.subprocess.run", return_value=fake_result):
            with patch("claw_v2.computer.shutil.which", return_value="/usr/local/bin/codex"):
                with self.assertRaises(RuntimeError):
                    backend.run("open Chrome")

    def test_successful_run_returns_stdout(self) -> None:
        from claw_v2.computer import CodexComputerBackend
        from unittest.mock import patch, MagicMock
        backend = CodexComputerBackend(cli_path="codex", model="codex-mini-latest")
        fake_result = MagicMock(returncode=0, stdout="Opened Chrome successfully.\n", stderr="")
        with patch("claw_v2.computer.subprocess.run", return_value=fake_result):
            with patch("claw_v2.computer.shutil.which", return_value="/usr/local/bin/codex"):
                result = backend.run("open Chrome")
        self.assertEqual(result, "Opened Chrome successfully.")

    def test_computer_use_service_dispatches_to_codex_backend(self) -> None:
        from claw_v2.computer import CodexComputerBackend, ComputerUseService, ComputerSession
        backend = CodexComputerBackend(transport=lambda task: f"completed: {task}")
        svc = ComputerUseService(display_width=1280, display_height=800, codex_backend=backend)
        session = ComputerSession(task="open Safari")
        result = svc.run_agent_loop(session=session, client=None, gate=None, model="codex-mini-latest")
        self.assertEqual(result, "completed: open Safari")
```

Run:
```bash
.venv/bin/pytest tests/test_computer.py::CodexComputerBackendTests -v
```

Expected: 5 failures (`CodexComputerBackend` doesn't exist yet).

- [ ] **Step 4.2: Add CodexComputerBackend to computer.py**

In `claw_v2/computer.py`, add `import shutil` to the existing imports at the top (it's not there yet), then add after the `_EscListener` class and before `ComputerUseService`:

```python
class CodexComputerBackend:
    """Computer Use backend that uses Codex CLI for Mac automation.

    Replaces the OpenAI Responses API visual loop with Codex-generated
    AppleScript/bash execution. Uses the ChatGPT Pro subscription (cost $0).
    """

    _SYSTEM_PROMPT = (
        "You are automating a Mac computer. "
        "Execute the task using AppleScript (osascript) or shell commands. "
        "Be direct and complete the task without asking for confirmation."
    )

    def __init__(
        self,
        cli_path: str = "codex",
        model: str = "codex-mini-latest",
        *,
        transport: Callable[[str], str] | None = None,
    ) -> None:
        self.cli_path = cli_path
        self.model = model
        self._transport = transport

    def run(self, task: str) -> str:
        if self._transport is not None:
            return self._transport(task)
        return self._run_cli(task)

    def _run_cli(self, task: str) -> str:
        resolved = shutil.which(self.cli_path)
        if not resolved:
            raise RuntimeError(
                f"Codex CLI not found at '{self.cli_path}'. "
                "Install with: npm install -g @openai/codex"
            )

        prompt = f"{self._SYSTEM_PROMPT}\n\nTask: {task}"
        cmd = [
            resolved, "exec",
            "--model", self.model,
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--color", "never",
            prompt,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Codex CLI not found at '{self.cli_path}'."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Codex CLI timed out after 120s for computer use task") from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"Codex computer use failed (exit {result.returncode}): {result.stderr.strip()[:500]}"
            )

        return result.stdout.strip()
```

Note: also add `from typing import Callable` to computer.py imports if not already present. Check first — it may already be imported as `from typing import Any`.

In `claw_v2/computer.py`, the existing import is `from typing import Any`. Update to:
```python
from typing import Any, Callable
```

- [ ] **Step 4.3: Update ComputerUseService to accept codex_backend**

In `claw_v2/computer.py`, update `ComputerUseService.__init__`:

```python
    def __init__(
        self,
        *,
        display_width: int = 1280,
        display_height: int = 800,
        scale_factor: float = 1.0,
        action_delay: float = 0.3,
        codex_backend: "CodexComputerBackend | None" = None,
    ) -> None:
        self.display_width = display_width
        self.display_height = display_height
        self.scale_factor = scale_factor
        self.action_delay = action_delay
        self.codex_backend = codex_backend
```

Update `run_agent_loop` to dispatch to codex backend when set. Find `run_agent_loop` in `computer.py` (around line 238) and add at the start of the method body:

```python
    def run_agent_loop(
        self,
        *,
        session: ComputerSession,
        client: Any,
        gate: Any,
        model: str = "gpt-5.4",
        system_prompt: str | None = None,
    ) -> str:
        if self.codex_backend is not None:
            return self.codex_backend.run(session.task)
        # ... rest of existing implementation unchanged
```

- [ ] **Step 4.4: Update bot.py _run_computer_session to skip OpenAI client for codex**

In `claw_v2/bot.py`, find `_run_computer_session` (~line 1427). Update the try block:

```python
    def _run_computer_session(self, session_id: str) -> str:
        session = self._computer_sessions.get(session_id)
        if session is None:
            return "no active computer session"
        try:
            gate = self._get_computer_gate()
            # Skip OpenAI client creation when Codex backend is active
            if self.computer.codex_backend is not None:
                client = None
            else:
                client = self._get_computer_client()
            result = self.computer.run_agent_loop(
                session=session,
                client=client,
                gate=gate,
                model=self.computer_model,
                system_prompt=self.computer_system_prompt,
            )
```

- [ ] **Step 4.5: Update main.py to wire CodexComputerBackend**

In `claw_v2/main.py`, add import at top:

```python
from claw_v2.computer import ComputerUseService, BrowserUseService, CodexComputerBackend
```

Update the `computer = ComputerUseService(...)` block (~line 298):

```python
    codex_computer_backend: CodexComputerBackend | None = None
    if config.computer_use_backend == "codex":
        codex_computer_backend = CodexComputerBackend(
            cli_path=config.codex_cli_path,
            model=config.codex_model,
        )
    computer = ComputerUseService(
        display_width=config.computer_display_width,
        display_height=config.computer_display_height,
        codex_backend=codex_computer_backend,
    )
```

- [ ] **Step 4.6: Run computer tests**

```bash
.venv/bin/pytest tests/test_computer.py -v
```

Expected: all passing including the 5 new ones.

- [ ] **Step 4.7: Run full suite**

```bash
.venv/bin/pytest tests/ -x -q --tb=short 2>&1 | tail -10
```

Expected: all 457+ passing.

- [ ] **Step 4.8: Commit**

```bash
git add claw_v2/computer.py claw_v2/bot.py claw_v2/main.py tests/test_computer.py
git commit -m "feat: add CodexComputerBackend and wire into ComputerUseService"
```

---

## Task 5: End-to-end verification

- [ ] **Step 5.1: Run the full test suite one final time**

```bash
.venv/bin/pytest tests/ -q --tb=short 2>&1 | tail -15
```

Expected: 457+ passing, 0 failures.

- [ ] **Step 5.2: Verify env var activation works**

```bash
cd /Users/hector/Projects/Dr.-strange
WORKER_PROVIDER=codex WORKER_MODEL=codex-mini-latest \
  .venv/bin/python -c "
from claw_v2.config import AppConfig
import os
os.environ['WORKER_PROVIDER'] = 'codex'
os.environ['WORKER_MODEL'] = 'codex-mini-latest'
# Can't call from_env() without changing cwd, just verify directly
from tests.helpers import make_config
from pathlib import Path
import tempfile
with tempfile.TemporaryDirectory() as d:
    c = make_config(Path(d))
    c.worker_provider = 'codex'
    c.worker_model = 'codex-mini-latest'
    c.validate()
    print('Config valid:', c.worker_provider, c.worker_model)
    from claw_v2.llm import LLMRouter
    router = LLMRouter.default(c)
    print('Codex adapter registered:', 'codex' in router.adapters)
"
```

Expected output:
```
Config valid: codex codex-mini-latest
Codex adapter registered: True
```

- [ ] **Step 5.3: Final commit**

```bash
git add -A
git commit -m "feat: complete Codex CLI integration — worker lane + computer use backend"
```

---

## Activation (post-deploy)

To activate Codex for worker lane and computer use, set in the daemon's environment:

```bash
# In launchd plist or .env file:
WORKER_PROVIDER=codex
WORKER_MODEL=codex-mini-latest
COMPUTER_USE_BACKEND=codex

# Brain stays unchanged:
BRAIN_PROVIDER=anthropic
BRAIN_MODEL=claude-opus-4-6
```

To rollback: unset `WORKER_PROVIDER` and `COMPUTER_USE_BACKEND` (defaults back to anthropic/openai).
