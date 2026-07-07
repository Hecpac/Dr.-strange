from __future__ import annotations

import unittest

from claw_v2.bot_helpers import _should_use_browser_executor
from claw_v2.computer_handler import ComputerHandler

# CB0 evidence gate: lock the CURRENT computer-vs-browser delegation routing so
# the ADR's premise is executable, and so any future change to the routing (e.g.
# adding a codex-desktop lane) is a deliberate, test-visible edit rather than a
# silent drift. These assert real routing functions, not documentation.


class CB0RoutingMatrixTests(unittest.TestCase):
    def test_browse_mode_routes_to_browser_executor(self) -> None:
        self.assertTrue(_should_use_browser_executor("browse", "cualquier objetivo"))

    def test_browser_objective_routes_to_browser_executor(self) -> None:
        # ops/publish with a browser signal → in-process browser executor.
        self.assertTrue(_should_use_browser_executor("ops", "navega a x.com y publica el post"))

    def test_desktop_gui_objective_has_no_browser_executor_home(self) -> None:
        # A pure desktop / computer-use objective (no browser signal) is NOT
        # routed to the browser executor — so a delegated version falls to the
        # Codex coordinator, which runs --sandbox workspace-write (no network)
        # and cannot drive the desktop GUI. THIS IS THE CB0 GAP: delegated
        # computer-use has no destination that can actually execute it.
        for objective in (
            "abre la app Calculadora del escritorio y toma un screenshot",
            "usa el escritorio para abrir Notas y escribir un recordatorio",
        ):
            self.assertFalse(_should_use_browser_executor("ops", objective), objective)

    def test_browser_is_delegable_but_computer_use_is_not(self) -> None:
        # The locked asymmetry the ADR rests on: the computer handler runs
        # delegated BROWSER tasks (browser is delegable), but there is no
        # delegated COMPUTER task runner (computer-use is inline-only). If a
        # codex-desktop lane is ever built, this test must be updated
        # deliberately — that is the point.
        self.assertTrue(hasattr(ComputerHandler, "run_delegated_browser_task"))
        self.assertFalse(hasattr(ComputerHandler, "run_delegated_computer_task"))


if __name__ == "__main__":
    unittest.main()
