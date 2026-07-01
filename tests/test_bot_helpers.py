from __future__ import annotations

import unittest
from pathlib import Path

from claw_v2.bot_helpers import (
    _build_coordinator_tasks,
    _chat_response_has_internal_leak,
    _extract_option_reference,
    _extract_ratio_context_from_text,
    _looks_like_ratio_reference_request,
    _sanitize_chat_response,
    _should_use_browser_executor,
)
from claw_v2.capability_preflight import CommandSpec, preflight_command, preflight_objective
from claw_v2.sandbox import SandboxPolicy


class BotHelperRegressionTests(unittest.TestCase):
    def test_dame_los_2_ratios_is_not_option_2(self) -> None:
        self.assertIsNone(_extract_option_reference("Dame los 2 ratios"))
        self.assertTrue(_looks_like_ratio_reference_request("Dame los 2 ratios"))

    def test_vamos_con_la_2_selects_option_2(self) -> None:
        self.assertEqual(_extract_option_reference("Vamos con la 2"), 2)
        self.assertEqual(_extract_option_reference("vamos con opción 2"), 2)

    def test_letter_option_selects_option(self) -> None:
        self.assertEqual(_extract_option_reference("Opción A"), 1)
        self.assertEqual(_extract_option_reference("opcion b"), 2)

    def test_ratio_context_extracts_both_pending_ratios(self) -> None:
        text = "Te tiro los otros 2 ratios: 9:16 vertical para Reels y 1:1 cuadrado para feed."
        self.assertEqual(
            _extract_ratio_context_from_text(text),
            ["9:16 vertical", "1:1 cuadrado"],
        )

    def test_visible_chat_response_suppresses_internal_runtime_details(self) -> None:
        text = (
            "Message ID 123 a tu chat tg-abc. localhost:8765 terminal bridge "
            "message_id 99 chat_id 456 nlm-abc blocked model response /Users/hector/private/file.txt"
        )
        sanitized = _sanitize_chat_response(text)

        self.assertFalse(_chat_response_has_internal_leak(sanitized))
        self.assertNotIn("Message ID", sanitized)
        self.assertNotIn("message_id", sanitized)
        self.assertNotIn("chat_id", sanitized)
        self.assertNotIn("tg-", sanitized)
        self.assertNotIn("nlm-", sanitized)
        self.assertNotIn("localhost", sanitized)
        self.assertNotIn("terminal bridge", sanitized.lower())
        self.assertNotIn("/Users/hector", sanitized)

    def test_visible_chat_response_redacts_outbound_secrets_and_tracebacks(self) -> None:
        # #9: token-shaped secrets and Python tracebacks must never reach the user.
        secrets = (
            "sk-proj-ABCDEFGHIJKLMNOP0123",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_ABCDEFGHIJ0123456789abcdefXYZ",
            "AIzaSyABCDEFGHIJKLMNOPQRSTUVWX0123456",
            "Authorization: " + "Bearer " + "eyJhbGciOi.payload.signature123",
        )
        for raw in secrets:
            sanitized = _sanitize_chat_response(f"valor: {raw} fin")
            self.assertNotIn(raw, sanitized)
            self.assertIn("[secreto omitido]", sanitized)

        tb = 'Traceback (most recent call last):\n  File "x.py", line 1\nRuntimeError: boom'
        sanitized = _sanitize_chat_response(tb)
        self.assertNotIn("RuntimeError: boom", sanitized)
        self.assertIn("[traza interna omitida]", sanitized)

    def test_visible_chat_response_keeps_word_bearer_in_prose(self) -> None:
        # The outbound secret filter must not mangle ordinary prose.
        text = "El bearer del mensaje quedó claro y todo en orden."
        sanitized = _sanitize_chat_response(text)
        self.assertNotIn("[secreto omitido]", sanitized)
        self.assertIn("bearer", sanitized.lower())

    def test_visible_chat_response_replaces_internal_trace_fallback_text(self) -> None:
        text = (
            "La salida del modelo contenía trazas internas de herramientas y la oculté. "
            "Repite la instrucción y la ejecuto limpio."
        )
        sanitized = _sanitize_chat_response(text)
        lowered = sanitized.lower()

        self.assertFalse(_chat_response_has_internal_leak(sanitized))
        self.assertNotIn("salida del modelo", lowered)
        self.assertNotIn("trazas internas", lowered)
        self.assertNotIn("herramientas internas", lowered)
        self.assertNotIn("la oculté", lowered)
        self.assertNotIn("respuesta bloqueada", lowered)
        self.assertNotIn("sanitizer", lowered)
        self.assertNotIn("blocked model response", lowered)
        self.assertNotIn("repite la instrucción", lowered)

    def test_visible_chat_response_replaces_raw_tool_trace(self) -> None:
        sanitized = _sanitize_chat_response('to=functions.exec_command {"cmd":"pwd"}')

        self.assertFalse(_chat_response_has_internal_leak(sanitized))
        self.assertNotIn("to=functions", sanitized)

    def test_visible_chat_response_redacts_task_ledger_internals(self) -> None:
        text = (
            "Ledger:\n"
            "Task: tg-574707975:evidence-gate:1779207757786634000 "
            "brain-tooluse:tg-574707975:1779208007773945000 "
            "status=running_needs_verification "
            "terminal=completed_unverified "
            "tables=observe_stream,agent_tasks "
            "reason=brain_tooluse_with_manifest_pending_verification "
            "error=runtime lost authoritative backing state"
        )

        sanitized = _sanitize_chat_response(text)

        self.assertFalse(_chat_response_has_internal_leak(sanitized))
        self.assertNotIn("evidence-gate", sanitized)
        self.assertNotIn("brain-tooluse", sanitized)
        self.assertNotIn("needs_verification", sanitized)
        self.assertNotIn("completed_unverified", sanitized)
        self.assertNotIn("observe_stream", sanitized)
        self.assertNotIn("agent_tasks", sanitized)
        self.assertNotIn("Ledger:", sanitized)
        self.assertNotIn("[tarea interna omitida]", sanitized)
        self.assertNotIn("runtime lost authoritative backing state", sanitized)
        self.assertIn("pendiente de verificacion", sanitized)
        self.assertIn("completada sin verificacion final", sanitized)
        self.assertIn("telemetria local", sanitized)
        self.assertIn("registro de tareas", sanitized)
        self.assertIn("Historial de tareas:", sanitized)

    def test_visible_chat_response_redacts_raw_sandbox_host_diagnostics(self) -> None:
        text = (
            "Sigue bloqueado. El error es `binary 'brew' requires higher privilege level "
            "(not in the allowed whitelist)`. El edit a `~/.claude/settings.json` "
            "con `sandbox.excludedCommands` no era la capa correcta. Probablemente "
            "Seatbelt OS-level o el runtime host de la Bash tool; tampoco en "
            "`claw_v2/sandbox.py`."
        )

        sanitized = _sanitize_chat_response(text)
        lowered = sanitized.lower()

        self.assertFalse(_chat_response_has_internal_leak(sanitized))
        self.assertNotIn("allowed whitelist", lowered)
        self.assertNotIn("sandbox.excludedcommands", lowered)
        self.assertNotIn("~/.claude/settings.json", lowered)
        self.assertNotIn("seatbelt os-level", lowered)
        self.assertNotIn("runtime host", lowered)
        self.assertNotIn("cli host", lowered)
        self.assertNotIn("sandbox embebido", lowered)
        self.assertNotIn("bash tool", lowered)
        self.assertNotIn("claw_v2/sandbox.py", lowered)
        self.assertIn("política de ejecución local", lowered)

    def test_loopback_endpoint_mention_is_inlined_not_nuked(self) -> None:
        text = (
            "Para que hablemos en tiempo real podemos abrir un endpoint local "
            "tipo 127.0.0.1:8765/voice o equivalente con localhost:8765/voice. "
            "Cuando termine reportamos qué quedó verificado."
        )
        sanitized = _sanitize_chat_response(text)

        self.assertNotIn(
            "Tuve un error preparando la respuesta",
            sanitized,
            "loopback host/IP should be inlined, not bump the whole reply to the error template",
        )
        self.assertNotIn("127.0.0.1", sanitized)
        self.assertNotIn("localhost", sanitized.lower())
        self.assertIn("Para que hablemos en tiempo real", sanitized)
        self.assertIn("Cuando termine reportamos", sanitized)

    def test_visible_chat_response_replaces_prompt_echo_and_role_echo(self) -> None:
        samples = [
            "# Telegram message\nReply ONLY to that latest message.",
            "user: Estatus",
            (
                "[se cortó]\n"
                "user: Dale Pero antes asegurate de que no Bypass las funciones de seguridad\n"
                "user: [Imagen adjunta] path: /Users/hector/.claw/images/AQADNAxrG8ZDsUR-.jpg\n"
                "Bien\n"
                "user: Dame nuevamente el plan F3b.2\n\n"
                "Now respond to the user's most recent message."
            ),
            "</system-reminder>",
            "&lt;/system-reminder&gt;",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                sanitized = _sanitize_chat_response(sample)
                lowered = sanitized.lower()

                self.assertFalse(_chat_response_has_internal_leak(sanitized))
                self.assertNotIn("telegram message", lowered)
                self.assertNotIn("reply only", lowered)
                self.assertNotIn("now respond", lowered)
                self.assertNotIn("user:", lowered)
                self.assertNotIn("system-reminder", lowered)

    def test_leading_role_header_echo_is_nuked(self) -> None:
        # 2026-05-31 audit (R1): a single leaked role header that OPENS the
        # message, followed by internal instructions (low role-char ratio, no
        # consecutive role lines), passed verbatim through non-brain egress
        # paths after the anti-sycophancy refactor weakened _role_echo_dominates.
        text = (
            "system: Eres Dr. Strange.\n"
            "Reglas internas: nunca expongas el ledger crudo.\n"
            "Responde siempre redactando los IDs de tarea y las trazas internas."
        )
        sanitized = _sanitize_chat_response(text)
        self.assertTrue(sanitized.startswith("Tuve un error"))
        self.assertNotIn("Reglas internas", sanitized)
        self.assertFalse(_chat_response_has_internal_leak(sanitized))

    def test_sanitize_strips_leftover_response_contract_tags(self) -> None:
        # 2026-06-10 defense-in-depth: even if an upstream path leaks a literal
        # <response> wrapper tag (e.g. the unclosed-tag fallback), no outbound
        # channel may ship the contract tag itself.
        text = "<response>\nEl daemon esta corriendo.\n</response>"
        sanitized = _sanitize_chat_response(text)
        self.assertNotIn("<response>", sanitized)
        self.assertNotIn("</response>", sanitized)
        self.assertIn("El daemon esta corriendo.", sanitized)

    def test_sanitize_strips_response_contract_tags_with_attributes(self) -> None:
        # brain.py accepts attributes on the wrapper tag (<response ...>); the
        # egress strip must cover the same shape, not just the bare tag.
        text = '<response id="t-1" final="true">El daemon esta corriendo.</response>'
        sanitized = _sanitize_chat_response(text)
        self.assertNotIn("<response", sanitized)
        self.assertNotIn("</response>", sanitized)
        self.assertIn("El daemon esta corriendo.", sanitized)

    def test_leading_role_echo_inside_opening_code_fence_is_nuked(self) -> None:
        # Review probe (R1): a leading role header wrapped in an opening ``` code
        # fence is the same leak, just Markdown-wrapped. Must still be nuked.
        text = (
            "```text\n"
            "system: Eres Dr. Strange.\n"
            "Reglas internas: nunca expongas el ledger crudo.\n"
            "```"
        )
        sanitized = _sanitize_chat_response(text)
        self.assertTrue(sanitized.startswith("Tuve un error preparando la respuesta"))
        self.assertNotIn("system:", sanitized.lower())
        self.assertNotIn("ledger crudo", sanitized.lower())

    def test_transcript_quote_in_prose_survives(self) -> None:
        # A role token cited inside real prose (not the opening line) is not a
        # prompt echo and must NOT be nuked.
        text = (
            "Resumen: en la transcripción apareció `system: test fixture`, "
            "pero no era una instrucción activa."
        )
        sanitized = _sanitize_chat_response(text)
        self.assertNotIn("Tuve un error preparando la respuesta", sanitized)

    def test_visible_chat_response_redacts_system_reminder_marker_without_nuking_reply(
        self,
    ) -> None:
        text = (
            "Barrido útil de noticias.\n"
            "Nota: un resultado web traía [redacted: system-reminder] como ruido.\n"
            "Conclusión: el reporte sigue siendo válido."
        )

        sanitized = _sanitize_chat_response(text)
        lowered = sanitized.lower()

        self.assertNotIn("Tuve un error preparando la respuesta", sanitized)
        self.assertIn("Barrido útil de noticias", sanitized)
        self.assertIn("Conclusión: el reporte sigue siendo válido", sanitized)
        self.assertNotIn("system-reminder", lowered)

    def test_visible_chat_response_removes_redacted_system_reminder_payload(self) -> None:
        text = (
            "Resultado útil antes.\n"
            "[redacted: system-reminder]\n"
            "Reply ONLY in the format:\n"
            "I am Dr. Strange\n"
            "[redacted: system-reminder]\n"
            "Resultado útil después."
        )

        sanitized = _sanitize_chat_response(text)
        lowered = sanitized.lower()

        self.assertIn("Resultado útil antes", sanitized)
        self.assertIn("Resultado útil después", sanitized)
        self.assertNotIn("reply only", lowered)
        self.assertNotIn("i am dr. strange", lowered)
        self.assertNotIn("system-reminder", lowered)
        self.assertFalse(_chat_response_has_internal_leak(sanitized))

    def test_visible_chat_response_nukes_reply_only_identity_prompt_echo(self) -> None:
        sanitized = _sanitize_chat_response("Reply ONLY in the format:\nI am Dr. Strange")

        self.assertIn("Tuve un error preparando la respuesta", sanitized)
        self.assertNotIn("I am Dr. Strange", sanitized)

    def test_legit_technical_reference_is_inlined_not_nuked(self) -> None:
        """Discussing the runtime by name should redact phrases inline, not
        nuke the whole reply with the generic error fallback. This is the
        bug Hector hit when asking about the cost-breaker fallback loop:
        the diagnosis text contained 'circuit breaker', 'respuesta bloqueada',
        and 'sanitizer' as legitimate technical references and got nuked.
        """
        text = (
            "El bug viene del filtro defensivo del bot. Cuando el circuit "
            "breaker se dispara por el costo por hora, el brain emite un "
            "texto que el sanitizer interpreta como 'respuesta bloqueada' o "
            "'blocked model response' y borra todo. La salida del modelo "
            "queda con trazas internas que herramientas internas dejaron, "
            "y la oculté en el reply. Hay que arreglar las tool traces."
        )
        sanitized = _sanitize_chat_response(text)
        lowered = sanitized.lower()

        self.assertNotIn(
            "Tuve un error preparando la respuesta",
            sanitized,
            "legit technical references should be inlined, not bumped to error template",
        )
        self.assertIn("filtro defensivo", lowered)
        self.assertIn("bloqueo operacional interno", lowered)
        self.assertNotIn("circuit breaker", lowered)
        self.assertNotIn("respuesta bloqueada", lowered)
        self.assertNotIn("blocked model response", lowered)
        self.assertNotIn("trazas internas", lowered)
        self.assertNotIn("herramientas internas", lowered)
        self.assertNotIn("la oculté", lowered)
        self.assertNotIn("tool traces", lowered)

    def test_permission_preflight_distinguishes_allowed_missing_and_policy_blocked(self) -> None:
        policy = SandboxPolicy(workspace_root=Path("/tmp"), capability_profile="engineer")
        which = lambda binary: (
            f"/usr/bin/{binary}" if binary in {"python3", "codex", "claude"} else None
        )

        allowed = preflight_command(
            CommandSpec("python3 --version", "python_check"),
            policy=policy,
            which=which,
        )
        missing = preflight_command(
            CommandSpec("poetry --version", "poetry_check"),
            policy=policy,
            which=which,
        )
        version_only = preflight_command(
            CommandSpec("codex --version", "codex_check"),
            policy=policy,
            which=which,
        )
        claude_version = preflight_command(
            CommandSpec("claude --version", "claude_check"),
            policy=policy,
            which=which,
        )

        self.assertEqual(allowed.status, "allowed")
        self.assertEqual(missing.status, "command_not_found")
        self.assertIn("command_not_found:poetry", missing.blocker)
        self.assertEqual(version_only.status, "allowed")
        self.assertEqual(claude_version.status, "allowed")

    def test_qts_lock_preflight_records_poetry_blocker(self) -> None:
        result = preflight_objective(
            "Regenera el lock del PR QTS",
            workspace_root=Path("/tmp"),
            capability_profile="engineer",
            which=lambda binary: f"/usr/bin/{binary}" if binary in {"python3", "git"} else None,
        )

        self.assertEqual(result.task_kind, "qts_lock_regeneration")
        self.assertFalse(result.allowed)
        self.assertTrue(any("poetry" in blocker for blocker in result.blockers))


class CoordinatorTaskBuilderTests(unittest.TestCase):
    def test_build_coordinator_tasks_ops_publish_browse_build_worker_implementation(self) -> None:
        for mode in ("ops", "publish", "browse"):
            research, implementation, verification = _build_coordinator_tasks(
                mode, "Publica el grid en @pachanodesign"
            )
            self.assertEqual([task.lane for task in research], ["research"], mode)
            self.assertIsNotNone(implementation, mode)
            self.assertEqual(len(implementation), 1, mode)
            self.assertEqual(implementation[0].lane, "worker", mode)
            self.assertEqual(implementation[0].name, "execute_operation", mode)
            self.assertIn("## Actions", implementation[0].instruction, mode)
            self.assertIn("## Verify", implementation[0].instruction, mode)
            self.assertIn("## Evidence", implementation[0].instruction, mode)
            self.assertEqual(verification[0].lane, "verifier", mode)
            self.assertIn("Verification Status:", verification[0].instruction, mode)

    def test_build_coordinator_tasks_publish_mentions_approval_gate_discipline(self) -> None:
        _, implementation, _ = _build_coordinator_tasks("publish", "Postea el reel")
        self.assertIn("never bypass the gate", implementation[0].instruction)

    def test_build_coordinator_tasks_research_and_coding_unchanged(self) -> None:
        research, implementation, verification = _build_coordinator_tasks(
            "research", "Investiga el tema"
        )
        self.assertIsNone(implementation)
        self.assertEqual(len(research), 2)
        self.assertEqual(verification[0].name, "verify_findings")

        research, implementation, verification = _build_coordinator_tasks(
            "coding", "Arregla el bug"
        )
        self.assertIsNotNone(implementation)
        self.assertEqual(implementation[0].name, "implement_change")

    def test_build_coordinator_tasks_long_browser_timeout(self) -> None:
        # browse always gets the long browser/CDP timeout + the guard directive.
        _, implementation, _ = _build_coordinator_tasks(
            "browse", "Abre la página y extrae la tabla"
        )
        self.assertEqual(implementation[0].timeout_seconds, 1200.0)
        self.assertIn("browser/CDP guard", implementation[0].instruction)

        # ops/publish get it only when the objective signals browser/CDP work.
        _, impl_cdp, _ = _build_coordinator_tasks(
            "ops", "Driver Chrome CDP en localhost:9250 y screenshotea"
        )
        self.assertEqual(impl_cdp[0].timeout_seconds, 1200.0)

        _, impl_nlm, _ = _build_coordinator_tasks("publish", "Genera el podcast en NotebookLM")
        self.assertEqual(impl_nlm[0].timeout_seconds, 1200.0)

        # Instagram publishing is CDP-based (claw_v2/instagram_publish.py) even
        # when the objective only says "Instagram"/"reel", not "Chrome/CDP".
        _, impl_ig, _ = _build_coordinator_tasks(
            "publish", "Publica el reel en Instagram @pachanodesign"
        )
        self.assertEqual(impl_ig[0].timeout_seconds, 1200.0)

        # plain ops without browser signals keeps the default (no override).
        _, impl_plain, _ = _build_coordinator_tasks("ops", "Corre el script de backup y reporta")
        self.assertIsNone(impl_plain[0].timeout_seconds)
        self.assertNotIn("browser/CDP guard", impl_plain[0].instruction)

    def test_should_use_browser_executor(self) -> None:
        # browse always routes to the in-process browser executor.
        self.assertTrue(_should_use_browser_executor("browse", "abre la página"))
        # ops/publish only when the objective signals browser/CDP work.
        self.assertTrue(_should_use_browser_executor("ops", "driver Chrome CDP en localhost:9250"))
        self.assertTrue(_should_use_browser_executor("publish", "publica el reel en Instagram"))
        # plain ops/publish and code modes stay on the Codex coordinator.
        self.assertFalse(_should_use_browser_executor("ops", "corre el script de backup"))
        self.assertFalse(_should_use_browser_executor("coding", "arregla el bug"))
        self.assertFalse(_should_use_browser_executor("research", "investiga el tema"))


if __name__ == "__main__":
    unittest.main()
