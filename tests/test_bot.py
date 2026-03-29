from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.agents import AgentDefinition, ExperimentRecord
from claw_v2.eval_mocks import scripted_experiment_runner
from claw_v2.github import PullRequestResult
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


def fake_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content="handled",
        lane=request.lane,
        provider="anthropic",
        model=request.model,
    )


class BotTests(unittest.TestCase):
    def test_terminal_commands_delegate_to_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                bridge = MagicMock()
                bridge.list_sessions.return_value = [{"session_id": "sess-1", "status": "running"}]
                bridge.open.return_value = {"session_id": "sess-1", "tool": "codex"}
                bridge.status.return_value = {"session_id": "sess-1", "status": "running"}
                bridge.read.return_value = {"session_id": "sess-1", "offset": 12, "next_offset": 16, "output": "pong"}
                bridge.send.return_value = {"session_id": "sess-1", "bytes_written": 12}
                bridge.close.return_value = {"session_id": "sess-1", "status": "closing"}
                runtime.bot.terminal_bridge = bridge

                sessions = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_list"))
                self.assertEqual(sessions["sessions"][0]["session_id"], "sess-1")

                opened = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/terminal_open codex /tmp/work dir",
                    )
                )
                self.assertEqual(opened["tool"], "codex")
                bridge.open.assert_called_once_with("codex", cwd="/tmp/work dir")

                status = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_status sess-1")
                )
                self.assertEqual(status["status"], "running")
                bridge.status.assert_called_once_with("sess-1")

                read = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_read sess-1 12")
                )
                self.assertEqual(read["output"], "pong")
                bridge.read.assert_called_once_with("sess-1", offset=12, limit=3000)

                sent = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/terminal_send sess-1 hola codex",
                    )
                )
                self.assertEqual(sent["bytes_written"], 12)
                bridge.send.assert_called_once_with("sess-1", "hola codex")

                closed = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_close sess-1")
                )
                self.assertEqual(closed["status"], "closing")
                bridge.close.assert_called_once_with("sess-1")

    def test_terminal_command_usage_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.terminal_bridge = MagicMock()

                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_open"),
                    "usage: /terminal_open <claude|codex> [cwd]",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_status"),
                    "usage: /terminal_status <session_id>",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_read"),
                    "usage: /terminal_read <session_id> [offset]",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_send"),
                    "usage: /terminal_send <session_id> <text>",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_close"),
                    "usage: /terminal_close <session_id>",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_read sess-1 -1"),
                    "offset must be greater than or equal to 0",
                )

    def test_agent_create_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                created = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/agent_create researcher-2 researcher Investigate regressions carefully.",
                    )
                )
                self.assertEqual(created["name"], "researcher-2")
                self.assertEqual(created["agent_class"], "researcher")
                self.assertIn("WebSearch", created["allowed_tools"])
                self.assertNotIn("Write", created["allowed_tools"])

    def test_approvals_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                pending = runtime.approvals.create("deploy_prod", "high risk deploy")
                approvals_payload = runtime.bot.handle_text(user_id="123", session_id="s1", text="/approvals")
                parsed = json.loads(approvals_payload)
                self.assertEqual(parsed[0]["approval_id"], pending.approval_id)
                status = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text=f"/approval_status {pending.approval_id}",
                )
                self.assertEqual(status, "pending")

    def test_agent_control_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-1",
                        agent_class="operator",
                        instruction="Ship carefully.",
                    )
                )

                listing = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/agents"))
                self.assertEqual(listing[0]["name"], "operator-1")
                self.assertFalse(listing[0]["commit_on_promotion"])

                promote = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_promote operator-1 on")
                )
                self.assertTrue(promote["promote_on_improvement"])

                branch = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_branch operator-1 on")
                )
                self.assertTrue(branch["branch_on_promotion"])

                commit = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_commit operator-1 on")
                )
                self.assertTrue(commit["commit_on_promotion"])

                message = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/agent_commit_message operator-1 chore(claw): publish operator-1",
                    )
                )
                self.assertEqual(message["promotion_commit_message"], "chore(claw): publish operator-1")

                branch_name = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/agent_branch_name operator-1 claw/operator-1/review",
                    )
                )
                self.assertEqual(branch_name["promotion_branch_name"], "claw/operator-1/review")

                detail = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_status operator-1")
                )
                self.assertEqual(detail["instruction"], "Ship carefully.")
                self.assertTrue(detail["commit_on_promotion"])
                self.assertTrue(detail["branch_on_promotion"])

                cleared = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_commit_message operator-1 clear")
                )
                self.assertIsNone(cleared["promotion_commit_message"])
                cleared_branch = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_branch_name operator-1 clear")
                )
                self.assertIsNone(cleared_branch["promotion_branch_name"])
                duplicate = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/agent_create operator-1 operator Duplicate attempt",
                )
                self.assertEqual(duplicate, "agent already exists: operator-1")

    def test_agent_control_errors_are_user_facing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_status missing"),
                    "agent not found: missing",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_create ../bad operator Nope"),
                    "agent_name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_create bad-judge judge Nope"),
                    "agent_class must be one of: researcher, operator, deployer",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_branch_name missing claw/foo"),
                    "agent not found: missing",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_commit missing on"),
                    "agent not found: missing",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_commit foo maybe"),
                    "toggle must be one of: on, off",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_run foo 0"),
                    "max_experiments must be greater than 0",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_publish foo 0"),
                    "max_experiments must be greater than 0",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_run_until foo nope 3"),
                    "target_metric must be a number",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_history foo nope"),
                    "limit must be an integer",
                )
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-err",
                        agent_class="operator",
                        instruction="Check errors.",
                    )
                )
                self.assertEqual(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/agent_branch_name operator-err bad..branch",
                    ),
                    "promotion_branch_name is not a valid git branch name",
                )

    def test_agent_run_commands_execute_and_resume_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-2",
                        agent_class="operator",
                        instruction="Improve carefully.",
                    ),
                    state={"paused": True, "last_verified_state": {"metric": 0.1}},
                )
                runtime.auto_research.experiment_runner = scripted_experiment_runner(
                    [ExperimentRecord(1, 0.2, 0.1, "improved")]
                )

                run_payload = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_run operator-2 1")
                )
                self.assertEqual(run_payload["run_reason"], "completed")
                self.assertEqual(run_payload["experiments_run"], 1)
                self.assertEqual(run_payload["run_last_metric"], 0.2)
                self.assertFalse(run_payload["paused"])

                runtime.auto_research.experiment_runner = scripted_experiment_runner(
                    [
                        ExperimentRecord(1, 0.25, 0.2, "improved"),
                        ExperimentRecord(2, 0.35, 0.25, "improved"),
                    ]
                )
                run_until_payload = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_run_until operator-2 0.3 3")
                )
                self.assertEqual(run_until_payload["run_reason"], "target_reached")
                self.assertEqual(run_until_payload["experiments_run"], 2)
                self.assertEqual(run_until_payload["run_last_metric"], 0.35)

    def test_agent_pause_resume_and_history_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-4",
                        agent_class="operator",
                        instruction="Inspect history.",
                    )
                )
                runtime.auto_research.store.append_result("operator-4", ExperimentRecord(1, 0.2, 0.1, "improved", 0.01))
                runtime.auto_research.store.append_result(
                    "operator-4",
                    ExperimentRecord(2, 0.25, 0.2, "improved", 0.02, "abc1234", "claw/operator-4/abc1234"),
                )

                paused = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_pause operator-4")
                )
                self.assertTrue(paused["paused"])

                resumed = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_resume operator-4")
                )
                self.assertFalse(resumed["paused"])

                history = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_history operator-4 1")
                )
                self.assertEqual(history["history_limit"], 1)
                self.assertEqual(history["history_count"], 1)
                self.assertEqual(history["history"][0]["experiment_number"], 2)
                self.assertEqual(history["history"][0]["promotion_commit_sha"], "abc1234")
                self.assertEqual(history["history"][0]["promotion_branch_name"], "claw/operator-4/abc1234")

    def test_agent_publish_command_returns_publication_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-publish",
                        agent_class="operator",
                        instruction="Publish carefully.",
                    ),
                    state={"last_verified_state": {"metric": 0.1}},
                )
                runtime.auto_research.experiment_runner = scripted_experiment_runner(
                    [
                        ExperimentRecord(
                            1,
                            0.2,
                            0.1,
                            "executed",
                            0.03,
                            "deadbeef",
                            "claw/operator-publish/deadbee",
                        )
                    ]
                )

                payload = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_publish operator-publish 1")
                )
                self.assertEqual(payload["run_reason"], "completed")
                self.assertTrue(payload["publish_mode_updated"])
                self.assertTrue(payload["published"])
                self.assertEqual(payload["published_commit_sha"], "deadbeef")
                self.assertEqual(payload["published_branch_name"], "claw/operator-publish/deadbee")

    def test_agent_pr_command_returns_pull_request_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-pr",
                        agent_class="operator",
                        instruction="Open a pull request.",
                    ),
                    state={"last_verified_state": {"metric": 0.1}},
                )
                runtime.auto_research.experiment_runner = scripted_experiment_runner(
                    [
                        ExperimentRecord(
                            1,
                            0.2,
                            0.1,
                            "executed",
                            0.03,
                            "beadfeed",
                            "claw/operator-pr/beadfee",
                        )
                    ]
                )

                class FakePullRequests:
                    def create_pull_request(self, **kwargs) -> PullRequestResult:
                        self.kwargs = kwargs
                        return PullRequestResult(
                            url="https://github.com/acme/repo/pull/7",
                            branch_name=kwargs["branch_name"],
                            title=kwargs["title"],
                            number=7,
                            draft=kwargs["draft"],
                        )

                runtime.bot.pull_requests = FakePullRequests()
                payload = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_pr operator-pr 1")
                )
                self.assertTrue(payload["published"])
                self.assertTrue(payload["pull_request_created"])
                self.assertEqual(payload["pull_request_number"], 7)
                self.assertEqual(payload["pull_request_url"], "https://github.com/acme/repo/pull/7")
                self.assertTrue(payload["pull_request_draft"])


    def test_chrome_pages_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.browser.connect_to_chrome.return_value = [
                    {"url": "https://ads.google.com", "title": "Google Ads", "index": 0},
                ]
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/chrome_pages")
                parsed = json.loads(result)
                self.assertEqual(parsed["pages"][0]["url"], "https://ads.google.com")

    def test_chrome_browse_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                from claw_v2.browser import BrowseResult
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.browser.chrome_navigate.return_value = BrowseResult(
                    url="https://ads.google.com/campaigns",
                    title="Google Ads",
                    content="campaign data...",
                )
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/chrome_browse https://ads.google.com")
                self.assertIn("Google Ads", result)
                self.assertIn("campaign data", result)

    def test_natural_language_google_ads_shortcut_uses_chrome_browse(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                from claw_v2.browser import BrowseResult
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.browser.chrome_navigate.return_value = BrowseResult(
                    url="https://ads.google.com/campaigns",
                    title="Google Ads",
                    content="campaign data...",
                )
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Ya chrome esta abierto en Google Ads revisalo",
                )
                self.assertIn("Google Ads", result)
                runtime.bot.browser.chrome_navigate.assert_called_once_with("https://ads.google.com")

    def test_natural_language_url_uses_isolated_browse(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                from claw_v2.browser import BrowseResult
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.browser.browse.return_value = BrowseResult(
                    url="https://openai.com/pricing",
                    title="Pricing",
                    content="pricing details...",
                )
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Revisa https://openai.com/pricing",
                )
                self.assertIn("Pricing", result)
                runtime.bot.browser.browse.assert_called_once_with("https://openai.com/pricing")

    def test_natural_language_bare_domain_is_normalized_for_browse(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                from claw_v2.browser import BrowseResult
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.browser.browse.return_value = BrowseResult(
                    url="https://example.com/docs",
                    title="Docs",
                    content="documentation...",
                )
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="revisa example.com/docs",
                )
                self.assertIn("Docs", result)
                runtime.bot.browser.browse.assert_called_once_with("https://example.com/docs")

    def test_browse_command_normalizes_bare_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                from claw_v2.browser import BrowseResult
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.browser.browse.return_value = BrowseResult(
                    url="https://localhost:3000",
                    title="Local App",
                    content="local preview...",
                )
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/browse localhost:3000",
                )
                self.assertIn("Local App", result)
                runtime.bot.browser.browse.assert_called_once_with("https://localhost:3000")

    def test_natural_language_terminal_shortcut_opens_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.terminal_bridge = MagicMock()
                runtime.bot.terminal_bridge.open.return_value = {"session_id": "sess-1", "tool": "claude"}
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Abre Claude code en la terminal",
                )
                parsed = json.loads(result)
                self.assertEqual(parsed["tool"], "claude")
                runtime.bot.terminal_bridge.open.assert_called_once_with("claude", cwd=None)

    def test_chrome_command_returns_actionable_cdp_message_when_port_is_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.browser.chrome_navigate.side_effect = RuntimeError("connect ECONNREFUSED 127.0.0.1:9222")
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/chrome_browse https://ads.google.com",
                )
                self.assertIn("Chrome no esta exponiendo CDP", result)
                self.assertIn("remote-debugging-port=9222", result)
                self.assertIn("user-data-dir", result)
                self.assertIn("/computer", result)

    def test_screen_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.computer = MagicMock()
                runtime.bot.computer.capture_screenshot.return_value = {"data": "abc123base64data", "media_type": "image/png"}
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/screen")
                parsed = json.loads(result)
                self.assertIn("screenshot_data", parsed)

    def test_action_approve_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                pending = runtime.bot.approvals.create(action="click", summary="test")
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text=f"/action_approve {pending.approval_id} {pending.token}")
                self.assertEqual(result, "approved")

    def test_action_abort_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                pending = runtime.bot.approvals.create(action="click", summary="test")
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text=f"/action_abort {pending.approval_id}")
                self.assertEqual(result, "action rejected")
                self.assertEqual(runtime.bot.approvals.status(pending.approval_id), "rejected")


if __name__ == "__main__":
    unittest.main()
