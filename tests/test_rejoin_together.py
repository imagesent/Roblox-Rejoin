"""Error-code 529 (HTTP join error) handling, orphan dialogs, and the
"rejoin together" option requested from Discord.

Cases:
- the "A Http error has occurred ... (Error Code: 529)" dialog is recognised
  and classified as retryable, so the client is closed and rejoined;
- a dialog shown by a helper process (not the tracked pid) is followed to its
  owner window, and adopted when only one client is still connecting;
- with Rejoin together on, one account dropping sends the rest back to the
  same server - and with it off, nothing else is touched.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import multi_account_monitor_engine as engine      # noqa: E402
import roblox_error_codes as ec                    # noqa: E402
import roblox_windows as rw                        # noqa: E402

# The dialog from the report, flattened the way scan_client_states sees it.
JOIN_ERROR_529 = ("Join Error | A Http error has occurred. Please close the "
                  "client and try again. (Error Code: 529) | Cancel | Retry")


class ErrorCodeTests(unittest.TestCase):
    def test_529_is_recognised(self):
        self.assertTrue(rw._KICK_TEXT_RE.search(JOIN_ERROR_529))

    def test_529_reason_and_category(self):
        reason, code = ec.reason_from_dialog(JOIN_ERROR_529)
        self.assertEqual(code, 529)
        self.assertIn("529", reason)
        self.assertIn("overloaded", reason)
        category, _label = ec.classify(529)
        self.assertEqual(category, ec.CATEGORY_RETRY)
        self.assertFalse(ec.is_stop_category(category))

    def test_http_error_without_a_code(self):
        reason, code = ec.reason_from_dialog(
            "Join Error | A Http error has occurred. Please close the client "
            "and try again. | Cancel | Retry")
        self.assertIsNone(code)
        self.assertTrue(reason)

    def test_http_status_codes_are_all_retryable(self):
        for code in (500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526,
                     527, 529, 530):
            with self.subTest(code=code):
                category, _label = ec.classify(code)
                self.assertEqual(category, ec.CATEGORY_RETRY)
                self.assertFalse(ec.is_stop_category(category))

    def test_still_stops_for_real_restrictions(self):
        for code in (146, 272, 292):
            with self.subTest(code=code):
                category, _label = ec.classify(code)
                self.assertTrue(ec.is_stop_category(category))


class OrphanDialogTests(unittest.TestCase):
    """A dialog from a helper process must still reach the right client."""

    def setUp(self):
        self.eng = engine.MonitorEngine(
            [], {"join_server": False, "auto_relaunch": True,
                 "kick_detect": True})
        self.eng.log = mock.MagicMock()
        self.eng.state = mock.MagicMock()
        self.eng.status = mock.MagicMock()

    def _state(self, uid, pid, username="u"):
        acc = {"user_id": uid, "username": username, "cookie": "c",
               "rplace": "1", "runiverse": "", "rdelay": 1, "join_on": False}
        st = self.eng._new_state(acc)
        st["state"] = "running"
        st["pid"] = pid
        st["open_since"] = 1.0
        return st

    def test_one_waiting_client_adopts_the_dialog(self):
        rt = {1: self._state(1, 100), 2: self._state(2, 200)}
        rt[1]["was_ingame"] = True          # already playing, not the one
        win_states = {100: {"dialog": None, "dialog_code": None, "hung": False},
                      200: {"dialog": None, "dialog_code": None, "hung": False}}
        orphans = [{"pid": 999, "owner_pid": 0, "title": "Join Error",
                    "reason": "Roblox is overloaded (HTTP error) (Error Code: 529)",
                    "code": 529}]
        self.eng._adopt_orphan_dialogs(rt, win_states, orphans)
        self.assertIn("529", win_states[200]["dialog"])
        self.assertEqual(win_states[200]["dialog_code"], 529)
        self.assertIsNone(win_states[100]["dialog"])

    def test_ambiguous_dialog_is_only_reported(self):
        rt = {1: self._state(1, 100), 2: self._state(2, 200)}
        win_states = {100: {"dialog": None, "dialog_code": None, "hung": False},
                      200: {"dialog": None, "dialog_code": None, "hung": False}}
        orphans = [{"pid": 999, "owner_pid": 0, "title": "Join Error",
                    "reason": "x (Error Code: 529)", "code": 529}]
        self.eng._adopt_orphan_dialogs(rt, win_states, orphans)
        self.assertIsNone(win_states[100]["dialog"])
        self.assertIsNone(win_states[200]["dialog"])
        self.assertTrue(any("doesn't belong" in c.args[0]
                            for c in self.eng.log.emit.call_args_list))

    def test_a_dialog_that_already_has_a_home_is_left_alone(self):
        rt = {1: self._state(1, 100)}
        win_states = {100: {"dialog": "kicked (Error Code: 267)",
                            "dialog_code": 267, "hung": False}}
        orphans = [{"pid": 999, "owner_pid": 0, "title": "Join Error",
                    "reason": "other (Error Code: 529)", "code": 529}]
        self.eng._adopt_orphan_dialogs(rt, win_states, orphans)
        self.assertEqual(win_states[100]["dialog_code"], 267)


class RejoinTogetherTests(unittest.TestCase):
    def setUp(self):
        self.eng = engine.MonitorEngine(
            [], {"join_server": True, "auto_relaunch": True,
                 "kick_detect": True, "rejoin_all": True})
        self.eng.log = mock.MagicMock()
        self.eng.state = mock.MagicMock()
        self.eng.status = mock.MagicMock()

    def _rt(self, n=3):
        rt = {}
        for i in range(1, n + 1):
            acc = {"user_id": i, "username": f"acct{i}", "cookie": "c",
                   "rplace": "1", "runiverse": "", "rdelay": 1,
                   "join_on": True, "rjoin": "link"}
            st = self.eng._new_state(acc)
            st["state"] = "running"
            st["pid"] = 1000 + i
            st["open_since"] = 1.0
            st["was_ingame"] = True
            rt[i] = st
        self.eng._rt = rt
        return rt

    def test_everyone_follows_a_crash(self):
        rt = self._rt()
        with mock.patch.object(engine, "kill_pid") as kill:
            self.eng._remove_from_game(rt[1], 100.0, "client crashed")
            self.assertEqual(rt[1]["state"], "leaving")
            self.assertEqual(rt[2]["state"], "leaving")
            self.assertEqual(rt[3]["state"], "leaving")
            # The crash keeps its own disconnect; the followers do not.
            self.assertEqual(rt[1]["disconnects"], 1)
            self.assertEqual(rt[2]["disconnects"], 0)
            self.assertEqual(kill.call_count, 3)
        self.assertIn("rejoining together", rt[2]["last_reason"])

    def test_followers_are_not_double_counted(self):
        rt = self._rt()
        with mock.patch.object(engine, "kill_pid"):
            self.eng._remove_from_game(rt[1], 100.0, "client crashed")
            # A second removal (e.g. another crash in the same tick) must not
            # drag the already-leaving accounts again.
            self.eng._remove_from_game(rt[2], 100.0, "also crashed")
        self.assertEqual(rt[3]["state"], "leaving")

    def test_off_means_nothing_else_moves(self):
        self.eng.flags["rejoin_all"] = False
        rt = self._rt()
        with mock.patch.object(engine, "kill_pid"):
            self.eng._remove_from_game(rt[1], 100.0, "client crashed")
        self.assertEqual(rt[1]["state"], "leaving")
        self.assertEqual(rt[2]["state"], "running")
        self.assertEqual(rt[3]["state"], "running")

    def test_manual_kill_does_not_drag_the_group(self):
        """The Kill button is deliberate: it must not move everyone."""
        rt = self._rt()
        # The manual path calls _begin_leave, not _remove_from_game.
        self.eng._begin_leave(rt[1], 100.0)
        self.assertEqual(rt[1]["state"], "leaving")
        self.assertEqual(rt[2]["state"], "running")

    def test_following_flag_clears_after_a_relaunch(self):
        rt = self._rt()
        rt[2]["following"] = True
        engine.MonitorEngine._record_launch_success(rt[2], 5000, 200.0)
        self.assertFalse(rt[2]["following"])

    def test_leftovers_do_not_block_a_later_rejoin(self):
        rt = self._rt()
        with mock.patch.object(engine, "kill_pid"):
            self.eng._remove_from_game(rt[1], 100.0, "client crashed")
        # acct2 is leaving; when it lands again it must be able to follow.
        engine.MonitorEngine._record_launch_success(rt[2], 5000, 200.0)
        rt[1]["state"] = "running"
        with mock.patch.object(engine, "kill_pid"):
            self.eng._remove_from_game(rt[1], 300.0, "crashed again")
        self.assertEqual(rt[2]["state"], "leaving")


if __name__ == "__main__":
    unittest.main()
