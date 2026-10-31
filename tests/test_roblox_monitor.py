import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"),
)

import roblox_monitor


class RobloxLogMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.log_path = os.path.join(self.temp_dir.name, "Player.log")
        with open(self.log_path, "w", encoding="utf-8"):
            pass
        self.config_patch = patch.multiple(
            roblox_monitor,
            PLACE_ID="100",
            UNIVERSE_ID=42,
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.monitor = roblox_monitor.RobloxLogMonitor(self.temp_dir.name)

    def _append_log(self, text):
        with open(self.log_path, "a", encoding="utf-8") as log_file:
            log_file.write(text)

    @patch("roblox_monitor.requests.get")
    def test_place_resolves_to_universe_id(self, mock_get):
        response = Mock()
        response.json.return_value = {"universeId": 42}
        mock_get.return_value = response

        self.assertEqual(roblox_monitor.get_universe_id("200"), 42)
        self.assertIn("/places/200/universe", mock_get.call_args.args[0])

    def test_same_universe_teleport_cancels_disconnect_marker(self):
        self._append_log(
            "Disconnect reason received\n"
            "Joining game ... place 200\n"
        )
        with (
            patch.object(roblox_monitor, "get_universe_id", return_value=42),
            patch.object(roblox_monitor.time, "monotonic", return_value=100),
        ):
            self.assertIsNone(self.monitor.check_for_disconnect())

        self.assertIsNone(self.monitor.pending_disconnect_reason)

    def test_same_universe_teleport_cancels_deferred_disconnect(self):
        self._append_log("Disconnect reason received\n")
        with patch.object(roblox_monitor.time, "monotonic", return_value=100):
            self.assertIsNone(self.monitor.check_for_disconnect())

        self._append_log("Joining game ... place 200\n")
        with (
            patch.object(roblox_monitor, "get_universe_id", return_value=42),
            patch.object(roblox_monitor.time, "monotonic", return_value=103),
        ):
            self.assertIsNone(self.monitor.check_for_disconnect())

        self.assertIsNone(self.monitor.pending_disconnect_reason)

    def test_other_universe_join_does_not_cancel_disconnect(self):
        self._append_log(
            "Disconnect reason received\n"
            "Joining game ... place 300\n"
        )
        with (
            patch.object(roblox_monitor, "get_universe_id", return_value=99),
            patch.object(roblox_monitor.time, "monotonic", return_value=100),
        ):
            self.assertIsNone(self.monitor.check_for_disconnect())

        with patch.object(roblox_monitor.time, "monotonic", return_value=111):
            self.assertEqual(
                self.monitor.check_for_disconnect(),
                "disconnect reason received",
            )


if __name__ == "__main__":
    unittest.main()
