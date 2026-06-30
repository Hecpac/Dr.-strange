from __future__ import annotations

import importlib
import unittest
from typing import Any


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


class BrowserPolicyPDPContracts(unittest.TestCase):
    """Normative red contracts for the canonical browser ActionPolicyEngine.

    These contracts pin the canonical PDP used by browser adapters before real
    primitives execute.
    """

    def _policy_module(self) -> Any:
        try:
            return importlib.import_module("claw_v2.automation_policy")
        except ModuleNotFoundError as exc:
            self.fail(
                "Missing canonical PDP module claw_v2.automation_policy; "
                "browser adapters must not authorize real actions directly."
            )
            raise exc

    def _engine(self) -> Any:
        module = self._policy_module()
        engine_cls = getattr(module, "ActionPolicyEngine", None)
        if engine_cls is None:
            self.fail("claw_v2.automation_policy must expose ActionPolicyEngine")
        return engine_cls()

    def _params_hash(self, params: dict[str, Any]) -> str:
        module = self._policy_module()
        hasher = getattr(module, "canonical_params_hash", None)
        if hasher is None:
            self.fail("claw_v2.automation_policy must expose canonical_params_hash")
        return str(hasher(params))

    def _normalize_origin(self, url: str) -> str:
        module = self._policy_module()
        normalizer = getattr(module, "normalize_origin", None)
        if normalizer is None:
            self.fail("claw_v2.automation_policy must expose normalize_origin")
        return str(normalizer(url))

    def _make_approval(self, **kwargs: Any) -> dict[str, Any]:
        module = self._policy_module()
        maker = getattr(module, "make_approval_scope", None)
        if maker is None:
            self.fail("claw_v2.automation_policy must expose make_approval_scope")
        return maker(**kwargs).to_dict()

    def _approval(
        self,
        *,
        action_name: str,
        params: dict[str, Any],
        current_url: str,
        target_url: str,
        task_id: str = "task-1",
        browser_context_id: str = "ctx-1",
        now: float = 1_900_000_000.0,
    ) -> dict[str, Any]:
        return {
            "action_name": action_name,
            "params_hash": self._params_hash(params),
            "current_origin": self._normalize_origin(current_url),
            "target_origin": self._normalize_origin(target_url),
            "task_id": task_id,
            "browser_context_id": browser_context_id,
            "expires_at": now + 60,
            "nonce": "nonce-1",
            "approved_by": "hector",
        }

    def _evaluate(
        self,
        *,
        action_name: str,
        params: dict[str, Any],
        current_url: str = "https://example.com/dashboard",
        target_url: str = "https://example.com/dashboard",
        task_id: str = "task-1",
        browser_context_id: str = "ctx-1",
        approval: dict[str, Any] | None = None,
        auto_approved: bool = False,
        now: float = 1_900_000_000.0,
    ) -> Any:
        try:
            return self._engine().evaluate(
                action_name=action_name,
                params=params,
                current_url=current_url,
                target_url=target_url,
                task_id=task_id,
                browser_context_id=browser_context_id,
                approval=approval,
                auto_approved=auto_approved,
                now=now,
            )
        except TypeError as exc:
            self.fail(
                "ActionPolicyEngine.evaluate must accept action_name, params, "
                "current_url, target_url, task_id, browser_context_id, approval, "
                f"auto_approved, and now. TypeError: {exc}"
            )

    def _assert_allowed(self, decision: Any) -> None:
        self.assertTrue(
            bool(_field(decision, "allowed")),
            f"expected policy decision to allow action, got {decision!r}",
        )

    def _assert_blocked(self, decision: Any, *, reason_code: str | None = None) -> None:
        self.assertFalse(
            bool(_field(decision, "allowed")),
            f"expected policy decision to block action, got {decision!r}",
        )
        if reason_code is not None:
            self.assertEqual(_field(decision, "reason_code"), reason_code)

    def _exact_evaluate_approval(self) -> dict[str, Any]:
        params = {"script": "document.title"}
        return self._approval(
            action_name="evaluate",
            params=params,
            current_url="https://example.com/dashboard",
            target_url="https://example.com/dashboard",
        )

    def test_policy_unknown_action_fails_closed_with_reason_code(self) -> None:
        decision = self._evaluate(
            action_name="teleport",
            params={"destination": "https://example.com"},
            auto_approved=True,
        )

        self._assert_blocked(decision, reason_code="unknown_action")

    def test_policy_origin_normalization_includes_scheme_host_and_effective_port(self) -> None:
        self.assertEqual(
            self._normalize_origin("https://Example.com/reports"),
            "https://example.com:443",
        )
        self.assertEqual(
            self._normalize_origin("http://example.com/reports"),
            "http://example.com:80",
        )
        self.assertEqual(
            self._normalize_origin("https://example.com:8443/reports"),
            "https://example.com:8443",
        )
        self.assertEqual(
            self._normalize_origin("https://bücher.example/reports"),
            "https://xn--bcher-kva.example:443",
        )

    def test_policy_params_hash_is_canonical_and_semantic(self) -> None:
        self.assertEqual(
            self._params_hash({"b": 2, "a": "x"}),
            self._params_hash({"a": "x", "b": 2}),
        )
        self.assertEqual(
            self._params_hash({"url": "https://Example.com/report"}),
            self._params_hash({"url": "https://example.com:443/report"}),
        )
        self.assertNotEqual(
            self._params_hash({"script": "document.title"}),
            self._params_hash({"script": "document.body.innerText"}),
        )

    def test_policy_params_hash_redacts_secret_fields_before_hashing(self) -> None:
        self.assertEqual(
            self._params_hash({"api_key": "sk-first-secret", "url": "https://example.com"}),
            self._params_hash({"api_key": "sk-second-secret", "url": "https://example.com"}),
        )

    def test_policy_action_matrix_classifies_required_browser_use_actions(self) -> None:
        module = self._policy_module()
        actions = getattr(module, "BROWSER_ACTION_DEFINITIONS")

        self.assertEqual(actions["evaluate"].risk, "high")
        self.assertEqual(actions["save_as_pdf"].risk, "high")
        self.assertEqual(actions["upload_file"].risk, "high")
        self.assertEqual(actions["write_file"].risk, "high")
        self.assertEqual(actions["replace_file"].risk, "high")
        self.assertEqual(actions["read_file"].risk, "high")
        self.assertEqual(actions["read_long_content"].risk, "high")
        self.assertEqual(actions["extract"].risk, "high")
        self.assertIn(actions["goto"].risk, {"low", "medium"})
        self.assertIn(actions["screenshot"].risk, {"low", "medium"})

    def test_policy_decision_audit_uses_hash_not_raw_params(self) -> None:
        decision = self._evaluate(
            action_name="evaluate",
            params={"script": "document.title", "api_key": "sk-secret-value"},
            approval=None,
        )

        payload = decision.to_audit_dict()

        self.assertEqual(payload["decision"], "deny")
        self.assertEqual(payload["reason_code"], "approval_required")
        self.assertEqual(payload["action_name"], "evaluate")
        self.assertIn("params_hash", payload)
        self.assertNotIn("params", payload)
        self.assertNotIn("sk-secret-value", repr(payload))

    def test_policy_subdomain_is_not_authorized_without_explicit_scope(self) -> None:
        params = {"script": "document.title"}
        approval = self._approval(
            action_name="evaluate",
            params=params,
            current_url="https://example.com/reports",
            target_url="https://example.com/reports",
        )

        self._assert_allowed(
            self._evaluate(
                action_name="evaluate",
                params=params,
                current_url="https://example.com/reports",
                target_url="https://example.com/reports",
                approval=approval,
            )
        )
        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params=params,
                current_url="https://app.example.com/reports",
                target_url="https://app.example.com/reports",
                approval=approval,
            )
        )

    def test_policy_goto_approval_does_not_authorize_evaluate(self) -> None:
        goto_params = {"url": "https://example.com/dashboard"}
        approval = self._approval(
            action_name="goto",
            params=goto_params,
            current_url="https://example.com/",
            target_url="https://example.com/dashboard",
        )

        self._assert_allowed(
            self._evaluate(
                action_name="goto",
                params=goto_params,
                current_url="https://example.com/",
                target_url="https://example.com/dashboard",
                approval=approval,
            )
        )
        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                current_url="https://example.com/dashboard",
                target_url="https://example.com/dashboard",
                approval=approval,
            )
        )

    def test_policy_evaluate_approval_is_bound_to_exact_script_params_hash(self) -> None:
        approval = self._exact_evaluate_approval()

        self._assert_allowed(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                approval=approval,
            )
        )
        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.body.innerText"},
                approval=approval,
            )
        )

    def test_policy_params_hash_change_invalidates_approval(self) -> None:
        params = {"script": "document.title", "timeout_ms": 1_000}
        approval = self._approval(
            action_name="evaluate",
            params=params,
            current_url="https://example.com/dashboard",
            target_url="https://example.com/dashboard",
        )

        self._assert_allowed(
            self._evaluate(action_name="evaluate", params=params, approval=approval)
        )
        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title", "timeout_ms": 2_000},
                approval=approval,
            )
        )

    def test_policy_current_origin_change_invalidates_approval(self) -> None:
        approval = self._exact_evaluate_approval()

        self._assert_allowed(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                current_url="https://example.com/dashboard",
                approval=approval,
            )
        )
        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                current_url="http://example.com/dashboard",
                target_url="https://example.com/dashboard",
                approval=approval,
            )
        )

    def test_policy_target_origin_change_invalidates_approval(self) -> None:
        approval = self._exact_evaluate_approval()

        self._assert_allowed(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                target_url="https://example.com/dashboard",
                approval=approval,
            )
        )
        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                current_url="https://example.com/dashboard",
                target_url="https://example.com:444/dashboard",
                approval=approval,
            )
        )

    def test_policy_task_id_change_invalidates_approval(self) -> None:
        approval = self._exact_evaluate_approval()

        self._assert_allowed(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                task_id="task-1",
                approval=approval,
            )
        )
        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                task_id="task-2",
                approval=approval,
            )
        )

    def test_policy_browser_context_id_change_invalidates_approval(self) -> None:
        approval = self._exact_evaluate_approval()

        self._assert_allowed(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                browser_context_id="ctx-1",
                approval=approval,
            )
        )
        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                browser_context_id="ctx-2",
                approval=approval,
            )
        )

    def test_policy_make_approval_scope_authorizes_exact_high_action(self) -> None:
        approval = self._make_approval(
            action_name="evaluate",
            params={"script": "document.title"},
            current_url="https://example.com/dashboard",
            target_url="https://example.com/dashboard",
            task_id="task-1",
            browser_context_id="ctx-1",
            approved_by="hector",
            nonce="nonce-1",
            now=1_900_000_000.0,
            ttl_seconds=60,
        )

        decision = self._evaluate(
            action_name="evaluate",
            params={"script": "document.title"},
            approval=approval,
            now=1_900_000_001.0,
        )

        self._assert_allowed(decision)
        self.assertTrue(decision.to_audit_dict()["approval_scope_present"])
        self.assertTrue(decision.to_audit_dict()["approval_scope_match"])

    def test_policy_expired_approval_scope_denies(self) -> None:
        approval = self._make_approval(
            action_name="evaluate",
            params={"script": "document.title"},
            current_url="https://example.com/dashboard",
            target_url="https://example.com/dashboard",
            task_id="task-1",
            browser_context_id="ctx-1",
            approved_by="hector",
            nonce="nonce-1",
            now=1_900_000_000.0,
            ttl_seconds=1,
        )

        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                approval=approval,
                now=1_900_000_002.0,
            ),
            reason_code="approval_expired",
        )

    def test_policy_missing_nonce_or_approved_by_denies(self) -> None:
        missing_nonce = self._exact_evaluate_approval()
        missing_nonce["nonce"] = ""
        missing_approver = self._exact_evaluate_approval()
        missing_approver["approved_by"] = ""

        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                approval=missing_nonce,
            ),
            reason_code="approval_scope_incomplete",
        )
        self._assert_blocked(
            self._evaluate(
                action_name="evaluate",
                params={"script": "document.title"},
                approval=missing_approver,
            ),
            reason_code="approval_scope_incomplete",
        )

    def test_policy_auto_approve_does_not_authorize_evaluate(self) -> None:
        decision = self._evaluate(
            action_name="evaluate",
            params={"script": "document.title"},
            approval=None,
            auto_approved=True,
        )

        self._assert_blocked(decision)

    def test_policy_auto_approve_does_not_authorize_save_as_pdf(self) -> None:
        decision = self._evaluate(
            action_name="save_as_pdf",
            params={"path": "/tmp/report.pdf"},
            approval=None,
            auto_approved=True,
        )

        self._assert_blocked(decision)


if __name__ == "__main__":
    unittest.main()
