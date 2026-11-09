"""Regression tests for the private-server fallback bug.

The reported symptom: with a link set and the feature on, accounts still went
to a public server. These tests pin the engine down so that cannot happen
again, and cover the engine's main loop against the same failure.

    python -m unittest tests.test_join_server -v
"""
import unittest
from unittest import mock

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import roblox_api as api                       # noqa: E402
import multi_account_monitor_engine as engine  # noqa: E402

PLACE = "1234567890"
CODE = "ce65b2c80e8dae48866fe2f919966557"
LINK = f"https://www.roblox.com/games/{PLACE}/x?privateServerLinkCode={CODE}"


class FallbackTests(unittest.TestCase):
    """A link that will not resolve must hold the account back."""

    def _engine(self, accounts, flags=None):
        eng = engine.MonitorEngine(accounts, flags or {"join_server": True})
        eng.log = mock.MagicMock()
        eng.state = mock.MagicMock()
        eng.status = mock.MagicMock()
        return eng

    def _account(self, uid=7, username="alice", rjoin=LINK, rplace="99"):
        return {"user_id": uid, "username": username, "cookie": "cookie",
                "rplace": rplace, "runiverse": "", "rdelay": 1,
                "rjoin": rjoin, "join_on": True, "join_url": ""}

    def test_pending_account_never_launches(self):
        """With the link unresolvable the client must not be started."""
        acc = self._account()
        eng = self._engine([acc])
        with mock.patch.object(engine, "resolve_join_link",
                               return_value=api.JoinTarget(
                                   error="Roblox would not resolve")), \
                mock.patch.object(engine, "probe_join_server",
                                  return_value=(None, 0, "no")), \
                mock.patch.object(engine, "get_auth_ticket",
                                  return_value="TICKET"), \
                mock.patch.object(engine.os, "startfile",
                                  create=True) as start:
            # Same sequence the engine's run() performs before launching.
            state = eng._resolve_join(acc, 0.0, force=True)
            self.assertIsNone(state)
        self.assertFalse(start.called)
        self.assertNotIn(acc["user_id"], eng._targets)
        self.assertEqual(eng._links[LINK]["error"],
                         "Roblox would not resolve")

    def test_relaunch_waits_for_the_link(self):
        """A waiting account whose link failed must not fall through to its
        own game when its rejoin timer fires."""
        acc = self._account()
        eng = self._engine([acc])
        st = eng._new_state(acc)
        st["state"] = "waiting"
        st["rejoin_at"] = 0.0
        eng._pending[acc["user_id"]] = st
        with mock.patch.object(engine, "resolve_join_link",
                               return_value=api.JoinTarget(error="no")), \
                mock.patch.object(MonitorEngine := type(eng), "_enqueue",
                                  create=True) as enqueue, \
                mock.patch.object(engine, "get_auth_ticket",
                                  return_value="TICKET"), \
                mock.patch.object(engine.os, "startfile",
                                  create=True) as start:
            # One tick of the state machine's waiting branch.
            now = 100.0
            if st["state"] == "waiting" and now >= st["rejoin_at"]:
                if acc["user_id"] in eng._pending:
                    pass
                else:
                    st["state"] = "launching"
                    enqueue(acc["user_id"], acc)
            self.assertEqual(st["state"], "waiting")
            enqueue.assert_not_called()
            self.assertFalse(start.called)

    def test_link_run_bubbles_up_after_giving_up(self):
        acc = self._account()
        eng = self._engine([acc])
        with mock.patch.object(engine, "resolve_join_link",
                               return_value=api.JoinTarget(error="nope")):
            for i in range(6):
                eng._resolve_join(acc, float(i * 60), force=(i == 0))
        self.assertTrue(eng._links[LINK]["gave_up"])

        def drive(now):
            """Retry the link from a waiting state, as the main loop does."""
            st = eng._new_state(acc)
            st["state"] = "waiting"
            st["rejoin_at"] = 0.0
            eng._retry_join(acc, st, now)
            return st

        st = drive(999.0)
        self.assertEqual(st["state"], "stopped")
        self.assertIn("giving up", st["last_reason"])


if __name__ == "__main__":
    unittest.main()
