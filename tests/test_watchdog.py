from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from claw_v2.watchdog import (
    WatchdogConfig,
    WatchdogState,
    decide,
    is_restartable,
    load_state,
    main,
    parse_etime_seconds,
    run_cycle,
    save_state,
)


def _critical(**overrides: object) -> dict:
    """A restartable critical report: critical + db readable + process down.
    Override individual checks to build other scenarios."""
    checks = {
        "status": "critical",
        "database_readable": True,
        "process_running": False,
        "port_listening": True,
        "heartbeat_stale": False,
        "web_transport_serving": True,
    }
    checks.update(overrides)
    return checks


class IsRestartableTests(unittest.TestCase):
    """Mirrors the exact condition the watchdog used inline before extraction
    (2026-06-13). Must NOT change: diagnostics critical is not weakened."""

    def test_critical_and_process_down(self) -> None:
        self.assertTrue(is_restartable(_critical(process_running=False)))

    def test_critical_and_port_down(self) -> None:
        self.assertTrue(
            is_restartable(_critical(process_running=True, port_listening=False))
        )

    def test_critical_and_heartbeat_stale(self) -> None:
        self.assertTrue(
            is_restartable(_critical(process_running=True, heartbeat_stale=True))
        )

    def test_critical_and_web_dead(self) -> None:
        self.assertTrue(
            is_restartable(_critical(process_running=True, web_transport_serving=False))
        )

    def test_critical_but_all_subchecks_ok_is_not_restartable(self) -> None:
        self.assertFalse(
            is_restartable(_critical(process_running=True))  # nothing else wrong
        )

    def test_attention_is_not_restartable(self) -> None:
        self.assertFalse(is_restartable(_critical(status="attention")))

    def test_critical_but_db_unreadable_is_not_restartable(self) -> None:
        # Never restart when the DB itself is unreadable (would lose data).
        self.assertFalse(is_restartable(_critical(database_readable=False)))


class DecideBootstrapGraceTests(unittest.TestCase):
    """Within the bootstrap grace window a restartable report is held, not
    acted on, so the watchdog never kills a daemon that is still starting."""

    def setUp(self) -> None:
        self.config = WatchdogConfig(bootstrap_grace_s=120.0, strikes_required=2)

    def test_holds_during_bootstrap_grace(self) -> None:
        d = decide(
            _critical(),
            process_uptime_s=30.0,
            state=WatchdogState(consecutive_restartable=5),
            config=self.config,
        )
        self.assertEqual(d.action, "hold")
        self.assertIn("grace", d.reason)
        # strikes reset so the bootstrap window never counts toward a restart
        self.assertEqual(d.state.consecutive_restartable, 0)

    def test_no_grace_when_uptime_unknown(self) -> None:
        # process down => uptime None => grace does not apply, strikes proceed
        d = decide(
            _critical(process_running=False),
            process_uptime_s=None,
            state=WatchdogState(consecutive_restartable=0),
            config=self.config,
        )
        self.assertEqual(d.action, "hold")  # first strike of 2
        self.assertEqual(d.state.consecutive_restartable, 1)


class DecideStrikesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = WatchdogConfig(bootstrap_grace_s=120.0, strikes_required=2)

    def test_ok_resets_strikes(self) -> None:
        d = decide(
            _critical(status="attention"),
            process_uptime_s=300.0,
            state=WatchdogState(consecutive_restartable=1),
            config=self.config,
        )
        self.assertEqual(d.action, "ok")
        self.assertEqual(d.state.consecutive_restartable, 0)

    def test_first_strike_holds(self) -> None:
        d = decide(
            _critical(),
            process_uptime_s=300.0,
            state=WatchdogState(consecutive_restartable=0),
            config=self.config,
        )
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.state.consecutive_restartable, 1)

    def test_second_consecutive_strike_restarts_and_resets(self) -> None:
        d = decide(
            _critical(),
            process_uptime_s=300.0,
            state=WatchdogState(consecutive_restartable=1),
            config=self.config,
        )
        self.assertEqual(d.action, "restart")
        self.assertEqual(d.state.consecutive_restartable, 0)

    def test_single_strike_config_restarts_immediately(self) -> None:
        d = decide(
            _critical(),
            process_uptime_s=300.0,
            state=WatchdogState(consecutive_restartable=0),
            config=WatchdogConfig(bootstrap_grace_s=120.0, strikes_required=1),
        )
        self.assertEqual(d.action, "restart")


class WatchdogConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = WatchdogConfig.from_env(env={})
        self.assertEqual(cfg.bootstrap_grace_s, 120.0)
        self.assertEqual(cfg.strikes_required, 2)

    def test_env_override(self) -> None:
        cfg = WatchdogConfig.from_env(
            env={"CLAW_WATCHDOG_BOOTSTRAP_GRACE_S": "45", "CLAW_WATCHDOG_STRIKES": "3"}
        )
        self.assertEqual(cfg.bootstrap_grace_s, 45.0)
        self.assertEqual(cfg.strikes_required, 3)

    def test_invalid_env_falls_back_to_defaults(self) -> None:
        cfg = WatchdogConfig.from_env(
            env={"CLAW_WATCHDOG_BOOTSTRAP_GRACE_S": "x", "CLAW_WATCHDOG_STRIKES": "0"}
        )
        self.assertEqual(cfg.bootstrap_grace_s, 120.0)
        self.assertEqual(cfg.strikes_required, 2)  # 0 is invalid -> safe default

    def test_negative_bootstrap_grace_falls_back_to_default(self) -> None:
        # A negative grace would silently disable the bootstrap window.
        cfg = WatchdogConfig.from_env(env={"CLAW_WATCHDOG_BOOTSTRAP_GRACE_S": "-1"})
        self.assertEqual(cfg.bootstrap_grace_s, 120.0)

    def test_zero_bootstrap_grace_is_allowed(self) -> None:
        # Zero is a legitimate "disable the grace window" choice, not a typo.
        cfg = WatchdogConfig.from_env(env={"CLAW_WATCHDOG_BOOTSTRAP_GRACE_S": "0"})
        self.assertEqual(cfg.bootstrap_grace_s, 0.0)


class WatchdogStatePersistenceTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with TemporaryDirectory() as d:
            p = Path(d) / "watchdog_state.json"
            save_state(p, WatchdogState(consecutive_restartable=3))
            self.assertEqual(load_state(p).consecutive_restartable, 3)

    def test_missing_file_is_zero(self) -> None:
        with TemporaryDirectory() as d:
            self.assertEqual(
                load_state(Path(d) / "nope.json").consecutive_restartable, 0
            )

    def test_corrupt_file_is_zero(self) -> None:
        with TemporaryDirectory() as d:
            p = Path(d) / "watchdog_state.json"
            p.write_text("{not json")
            self.assertEqual(load_state(p).consecutive_restartable, 0)

    def test_non_dict_json_file_is_zero(self) -> None:
        # Valid JSON but not a dict: must not raise AttributeError on .get().
        for payload in ("[1, 2, 3]", '"bad"', "5", "null"):
            with TemporaryDirectory() as d:
                p = Path(d) / "watchdog_state.json"
                p.write_text(payload)
                self.assertEqual(
                    load_state(p).consecutive_restartable, 0, f"payload={payload!r}"
                )


class RunCyclePersistenceTests(unittest.TestCase):
    """Each watchdog invocation is a separate process: the strike counter must
    persist to disk across run_cycle calls for the N-strikes debounce to work."""

    def setUp(self) -> None:
        self.config = WatchdogConfig(bootstrap_grace_s=120.0, strikes_required=2)
        self._dir = TemporaryDirectory()
        self.state_path = Path(self._dir.name) / "watchdog_state.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _cycle(self, checks: dict, uptime: float | None):
        return run_cycle(
            {"checks": checks},
            uptime_s=uptime,
            state_path=self.state_path,
            config=self.config,
        )

    def test_two_consecutive_critical_runs_restart_on_second(self) -> None:
        first = self._cycle(_critical(), 300.0)
        self.assertEqual(first.action, "hold")  # strike 1 persisted
        second = self._cycle(_critical(), 300.0)
        self.assertEqual(second.action, "restart")  # strike 2 reached
        third = self._cycle(_critical(), 300.0)
        self.assertEqual(third.action, "hold")  # counter reset after restart

    def test_ok_run_between_strikes_resets_counter(self) -> None:
        self.assertEqual(self._cycle(_critical(), 300.0).action, "hold")  # strike 1
        self.assertEqual(self._cycle(_critical(status="attention"), 300.0).action, "ok")
        # counter reset -> next critical is strike 1 again, not a restart
        self.assertEqual(self._cycle(_critical(), 300.0).action, "hold")

    def test_bootstrap_grace_never_restarts(self) -> None:
        for _ in range(5):
            d = self._cycle(_critical(), 10.0)  # always within grace
            self.assertEqual(d.action, "hold")


class ParseEtimeTests(unittest.TestCase):
    """`ps -o etime=` formats: [[DD-]HH:]MM:SS."""

    def test_mm_ss(self) -> None:
        self.assertEqual(parse_etime_seconds("05:30"), 330.0)

    def test_hh_mm_ss(self) -> None:
        self.assertEqual(parse_etime_seconds("01:05:30"), 3930.0)

    def test_dd_hh_mm_ss(self) -> None:
        self.assertEqual(parse_etime_seconds("2-03:04:05"), 183845.0)

    def test_whitespace_padding(self) -> None:
        self.assertEqual(parse_etime_seconds("   05:30 "), 330.0)

    def test_empty_is_none(self) -> None:
        self.assertIsNone(parse_etime_seconds(""))
        self.assertIsNone(parse_etime_seconds("   "))

    def test_garbage_is_none(self) -> None:
        self.assertIsNone(parse_etime_seconds("garbage"))


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.state_path = str(Path(self._dir.name) / "watchdog_state.json")
        self.env = {
            "CLAW_WATCHDOG_STATE_PATH": self.state_path,
            "CLAW_WATCHDOG_STRIKES": "2",
            "CLAW_WATCHDOG_BOOTSTRAP_GRACE_S": "120",
        }

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _run(self, report: dict, uptime: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(
                ["--uptime", uptime],
                stdin=io.StringIO(json.dumps(report)),
                env=self.env,
            )
        self.assertEqual(rc, 0)
        return buf.getvalue().strip()

    def test_prints_hold_then_restart_across_invocations(self) -> None:
        report = {"checks": _critical()}
        self.assertEqual(self._run(report, "05:00"), "hold")  # uptime 300s > grace
        self.assertEqual(self._run(report, "05:00"), "restart")

    def test_bootstrap_grace_prints_hold(self) -> None:
        self.assertEqual(self._run({"checks": _critical()}, "00:10"), "hold")

    def test_unparseable_stdin_is_ok_not_restart(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(
                ["--uptime", "05:00"], stdin=io.StringIO("{bad json"), env=self.env
            )
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "ok")

    def test_unparseable_stdin_resets_existing_strike(self) -> None:
        # A non-confirmatory (unreadable) read between two critical reads must
        # break the consecutive chain, so the next critical is strike 1 again.
        report = {"checks": _critical()}
        self.assertEqual(self._run(report, "05:00"), "hold")  # strike 1 persisted
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--uptime", "05:00"], stdin=io.StringIO("{bad"), env=self.env)
        self.assertEqual(buf.getvalue().strip(), "ok")  # strike reset
        self.assertEqual(self._run(report, "05:00"), "hold")  # strike 1, not restart

    def test_attention_prints_ok(self) -> None:
        self.assertEqual(
            self._run({"checks": _critical(status="attention")}, "05:00"), "ok"
        )


if __name__ == "__main__":
    unittest.main()
