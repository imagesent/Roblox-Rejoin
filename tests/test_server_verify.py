"""The private-server join must not be called a public server when the game
moves the player around, or when Roblox restarts the private server.

Reported case: the client lands in the private server correctly, the game
then hands the player to another lobby/place (or to a reserved server for
them), the job id changes, and the old check rejoined the client over and
over - "4 disconnects", "try 3/3".
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import multi_account_monitor_engine as engine      # noqa: E402
import roblox_api as api                           # noqa: E402

PLACE = "8304191830"          # the place the private-server link points at
OTHER_PLACE = "1234567890"    # a sub-place of the same experience
JOB = "883350232537aaaa"      # the private server's job id at resolve time
JOB_2 = "883350232537bbbb"    # ...and after Roblox restarts it
PUBLIC = "ffffffffffffffff"   # somewhere public
ROOT = PLACE


def presence(state, current_place, job, root=ROOT):
    """The tuple presence_detail returns."""
    places = {str(current_place)}
    if str(root) != str(current_place):
        places.add(str(root))
    return state, places, job, root


class ServerVerifyTests(unittest.TestCase):
    def setUp(self):
        self.eng = engine.MonitorEngine(
            [], {"join_server": True, "auto_relaunch": True,
                 "kick_detect": True})
        self.eng.log = mock.MagicMock()
        self.eng.state = mock.MagicMock()
        self.eng.status = mock.MagicMock()
        self.acc = {"user_id": 7, "username": "HekigyokoOfficialBGS",
                    "cookie": "cookie", "rplace": PLACE, "runiverse": "",
                    "rdelay": 1, "rjoin": "the link", "join_on": True}
        self.target = api.JoinTarget(place=PLACE, kind="private",
                                     link_code="abcdef", job_id=JOB)
        self.eng._links["the link"] = {"target": self.target, "ref": JOB,
                                       "ref_at": 0.0, "tries": 1,
                                       "error": "", "gave_up": False}
        self.eng._targets[7] = self.eng._links["the link"]
        self.st = self.eng._new_state(self.acc)
        self.st["state"] = "running"
        self.st["pid"] = 1234
        # A real timestamp: presence checks only run once a client has been
        # up for PRESENCE_SETTLE seconds.
        self.st["open_since"] = 1.0
        self.st["has_connected"] = True
        self.st["was_ingame"] = True     # seen in game already

    # -- the reported bug -------------------------------------------------
    def test_teleport_to_another_place_is_not_a_wrong_server(self):
        """The game moving the player to its own sub-place must not rejoin."""
        st = self.st
        with mock.patch.object(engine, "presence_detail",
                               return_value=presence("ingame", OTHER_PLACE,
                                                     "some-other-job")), \
                mock.patch.object(engine, "scan_client_states",
                                  return_value={1234: {}}), \
                mock.patch.object(engine, "roblox_pids",
                                  return_value={1234}), \
                mock.patch.object(engine, "kill_pid") as kill, \
                mock.patch.object(engine, "probe_join_server") as probe:
            for tick in range(10):
                self.eng._watch_running(7, st, {1234}, {}, 100.0 + tick * 5,
                                        True, True)
        kill.assert_not_called()
        probe.assert_not_called()          # nothing to re-check at all
        self.assertEqual(st["wrong_server_tries"], 0)
        self.assertEqual(st["disconnects"], 0)

    def test_same_place_other_job_reprobes_before_rejoining(self):
        """A restarted private server is recognised instead of rejoined."""
        st = self.st
        with mock.patch.object(engine, "presence_detail",
                               return_value=presence("ingame", PLACE, JOB_2)), \
                mock.patch.object(engine, "scan_client_states",
                                  return_value={1234: {}}), \
                mock.patch.object(engine, "roblox_pids",
                                  return_value={1234}), \
                mock.patch.object(engine, "kill_pid") as kill, \
                mock.patch.object(engine, "probe_join_server",
                                  return_value=(JOB_2, 2, "")) as probe:
            self.eng._watch_running(7, st, {1234}, {}, 100.0, True, True)
        probe.assert_called_once()
        kill.assert_not_called()
        self.assertTrue(st["server_verified"])
        self.assertEqual(self.eng._links["the link"]["ref"], JOB_2)

    def test_real_wrong_server_still_rejoins(self):
        """Public matchmaking is still caught."""
        st = self.st
        with mock.patch.object(engine, "presence_detail",
                               return_value=presence("ingame", PLACE, PUBLIC)), \
                mock.patch.object(engine, "scan_client_states",
                                  return_value={1234: {}}), \
                mock.patch.object(engine, "roblox_pids",
                                  return_value={1234}), \
                mock.patch.object(engine, "kill_pid") as kill, \
                mock.patch.object(engine, "probe_join_server",
                                  return_value=(JOB, 2, "")):
            # First poll starts the grace window, the second acts on it.
            self.eng._watch_running(7, st, {1234}, {}, 100.0, True, True)
            kill.assert_not_called()
            self.eng._watch_running(7, st, {1234}, {}, 130.0, True, True)
        kill.assert_called()
        self.assertEqual(st["wrong_server_tries"], 1)
        self.assertEqual(st["disconnects"], 1)
        self.assertIn("different server", st["last_reason"])

    def test_gives_up_after_the_last_try(self):
        st = self.st
        st["wrong_server_tries"] = engine.JOIN_VERIFY_MAX
        with mock.patch.object(engine, "presence_detail",
                               return_value=presence("ingame", PLACE, PUBLIC)), \
                mock.patch.object(engine, "scan_client_states",
                                  return_value={1234: {}}), \
                mock.patch.object(engine, "roblox_pids",
                                  return_value={1234}), \
                mock.patch.object(engine, "kill_pid") as kill, \
                mock.patch.object(engine, "probe_join_server",
                                  return_value=(JOB, 2, "")):
            self.eng._watch_running(7, st, {1234}, {}, 100.0, True, True)
            self.eng._watch_running(7, st, {1234}, {}, 130.0, True, True)
        kill.assert_not_called()
        self.assertTrue(st["server_check_done"])

    def test_a_successful_check_is_not_repeated(self):
        st = self.st
        calls = []

        def fake_presence(*_a, **_k):
            calls.append(1)
            return presence("ingame", PLACE, JOB)

        with mock.patch.object(engine, "presence_detail", fake_presence), \
                mock.patch.object(engine, "scan_client_states",
                                  return_value={1234: {}}), \
                mock.patch.object(engine, "roblox_pids",
                                  return_value={1234}), \
                mock.patch.object(engine, "probe_join_server") as probe:
            self.eng._watch_running(7, st, {1234}, {}, 100.0, True, True)
            self.assertTrue(st["server_verified"])
            self.eng._watch_running(7, st, {1234}, {}, 120.0, True, True)
        probe.assert_not_called()
        self.assertEqual(st["server_verified"], True)

    def test_verification_resumes_after_a_launch(self):
        st = self.st
        st["server_verified"] = True
        engine.MonitorEngine._record_launch_success(st, 999, 500.0)
        self.assertFalse(st["server_verified"])

    def test_other_experience_is_still_wrong_game(self):
        st = self.st
        with mock.patch.object(engine, "presence_detail",
                               return_value=presence("ingame", "5555555555",
                                                     PUBLIC, root="5555555555")), \
                mock.patch.object(engine, "scan_client_states",
                                  return_value={1234: {}}), \
                mock.patch.object(engine, "roblox_pids",
                                  return_value={1234}), \
                mock.patch.object(engine, "probe_join_server"):
            self.eng._watch_running(7, st, {1234}, {}, 100.0, True, True)
            self.eng._watch_running(7, st, {1234}, {}, 130.0, True, True)
        self.assertIn("different game", st["last_reason"])

    # -- place helper ------------------------------------------------------
    def test_current_place_prefers_the_non_root_place(self):
        self.assertEqual(
            self.eng._current_place({OTHER_PLACE, ROOT}, ROOT), OTHER_PLACE)
        self.assertEqual(self.eng._current_place({ROOT}, ROOT), ROOT)
        self.assertEqual(self.eng._current_place({"9"}, None), "9")

    def test_in_target_place(self):
        self.assertTrue(self.eng._in_target_place(
            self.acc, self.target, {PLACE}, ROOT))
        self.assertFalse(self.eng._in_target_place(
            self.acc, self.target, {OTHER_PLACE, ROOT}, ROOT))
        self.assertFalse(self.eng._in_target_place(
            self.acc, {"rplace": PLACE}, set(), None))


if __name__ == "__main__":
    unittest.main()
