"""Wave 3.7: dashboard endpoints under /api/think/*.

Existing /api/chat keeps working. The new endpoints serve JSON for
spending, recent events, observation window state, and projects. Each
returns 503 when its backing service is missing so the chat API stays
operational without observability wired in.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.chat_api import LocalChatAPI
from claw_v2.task_board import TaskBoard


class ThinkSpendingTests(unittest.TestCase):
    def test_returns_spending_today_and_per_agent_costs(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "1"
        observe = MagicMock()
        observe.spending_today.return_value = {"total": 1.42, "by_lane": {"worker": 1.0}}
        observe.cost_per_agent_today.return_value = {"rook": 1.0, "alma": 0.42}
        api = LocalChatAPI(bot_service=bot_service, observe=observe)

        status, _, body = api.handle_http(method="GET", path="/api/think/spending")

        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["spending_today"]["total"], 1.42)
        self.assertEqual(payload["cost_per_agent_today"]["rook"], 1.0)

    def test_returns_503_when_observe_stream_missing(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "1"
        bot_service.observe = None
        api = LocalChatAPI(bot_service=bot_service, observe=None)
        status, _, body = api.handle_http(method="GET", path="/api/think/spending")
        self.assertEqual(status, 503)
        self.assertIn("observe stream unavailable", json.loads(body)["error"])


class ThinkRecentTests(unittest.TestCase):
    def test_returns_recent_events_with_limit(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "1"
        observe = MagicMock()
        observe.recent_events.return_value = [
            {"event_type": "dispatch_decision", "payload": {"x": 1}},
            {"event_type": "llm_response", "payload": {}},
        ]
        api = LocalChatAPI(bot_service=bot_service, observe=observe)
        status, _, body = api.handle_http(method="GET", path="/api/think/recent?limit=2")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(len(payload["events"]), 2)
        self.assertEqual(payload["limit"], 2)

    def test_filters_by_event_type(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "1"
        observe = MagicMock()
        observe.recent_events.return_value = [
            {"event_type": "dispatch_decision", "payload": {}},
            {"event_type": "dispatch_decision", "payload": {}},
        ]
        api = LocalChatAPI(bot_service=bot_service, observe=observe)
        status, _, body = api.handle_http(
            method="GET", path="/api/think/recent?type=dispatch_decision"
        )
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        observe.recent_events.assert_called_once_with(limit=50, event_type="dispatch_decision")
        self.assertEqual(payload["filter_type"], "dispatch_decision")
        self.assertTrue(all(ev["event_type"] == "dispatch_decision" for ev in payload["events"]))


class ThinkCircuitTests(unittest.TestCase):
    def test_returns_observation_window_status(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "1"
        observation_window = MagicMock()
        observation_window.status_payload.return_value = {
            "frozen": False,
            "rolling_cost_per_hour": 0.0,
            "thresholds": {"cost_per_hour": 10.0},
        }
        api = LocalChatAPI(
            bot_service=bot_service,
            observation_window=observation_window,
        )
        status, _, body = api.handle_http(method="GET", path="/api/think/circuit")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertFalse(payload["frozen"])
        self.assertEqual(payload["thresholds"]["cost_per_hour"], 10.0)

    def test_returns_503_when_observation_window_missing(self) -> None:
        bot_service = MagicMock()
        bot_service.observation_window = None
        bot_service.allowed_user_id = "1"
        api = LocalChatAPI(bot_service=bot_service, observation_window=None)
        status, _, body = api.handle_http(method="GET", path="/api/think/circuit")
        self.assertEqual(status, 503)


class ThinkProjectsTests(unittest.TestCase):
    def test_returns_projects_with_task_summary(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "1"
        with tempfile.TemporaryDirectory() as tmp:
            board = TaskBoard(board_root=Path(tmp))
            project = board.publish_project("Land $10K MRR", success_criteria=["3 paying customers"])
            board.publish("step 1", "do step 1", project_id=project.id)
            board.publish("step 2", "do step 2", project_id=project.id)
            api = LocalChatAPI(bot_service=bot_service, task_board=board)

            status, _, body = api.handle_http(method="GET", path="/api/think/projects")

            self.assertEqual(status, 200)
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(len(payload["projects"]), 1)
            self.assertEqual(payload["projects"][0]["title"], "Land $10K MRR")
            self.assertEqual(payload["projects"][0]["task_summary"]["total"], 2)

    def test_returns_503_when_task_board_missing(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "1"
        api = LocalChatAPI(bot_service=bot_service, task_board=None)
        status, _, body = api.handle_http(method="GET", path="/api/think/projects")
        self.assertEqual(status, 503)


class ThinkMethodsTests(unittest.TestCase):
    def test_post_returns_405(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "1"
        api = LocalChatAPI(bot_service=bot_service, observe=MagicMock())
        status, _, _ = api.handle_http(method="POST", path="/api/think/spending")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
