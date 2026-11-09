"""Tests for the private-server / specific-server join-link feature.

These run offline: every Roblox call is replaced with a stub, so they can be
run without an account or a network. From the repository root:

    python -m unittest discover -s tests -v
"""

import unittest
from unittest import mock
import urllib.parse

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import roblox_api as api                       # noqa: E402
import multi_account_monitor_engine as engine  # noqa: E402
from rejoiner_constants import JOIN_VERIFY_MAX  # noqa: E402


def place_launcher(uri: str) -> str:
    """The place-launcher URL a launch URI points at (percent-decoded)."""
    part = uri.split("placelauncherurl:", 1)[1].split("+robloxLocale", 1)[0]
    return urllib.parse.unquote(part)

PLACE = "1234567890"
CODE = "ce65b2c80e8dae48866fe2f919966557"
JOB = "0f8fad5b-d9cb-469f-a165-70867728950e"


class ParseTests(unittest.TestCase):
    def test_link_formats(self):
        cases = [
            # (input, expected kind, place, link code / job)
            (f"https://www.roblox.com/games/{PLACE}/Some-Game?"
             f"privateServerLinkCode={CODE}", "private", PLACE, CODE),
            (f"https://roblox.com/games/{PLACE}/x?privateServerLinkCode={CODE}",
             "private", PLACE, CODE),
            ("roblox://experiences/start?placeId=1234567890&linkCode=abcdef123456",
             "private", PLACE, "abcdef123456"),
            (f"roblox-player:1+placelauncherurl:https://assetgame.roblox.com/game/"
             f"PlaceLauncher.ashx?request=RequestGameJob&placeId={PLACE}"
             f"&linkCode={CODE}",
             "private", PLACE, CODE),
            (f"https://www.roblox.com/share?code={CODE}&type=Server",
             "share", "", CODE),
            (f"https://www.roblox.com/share?code={CODE}", "share", "", CODE),
            (f"https://www.roblox.com/games/{PLACE}/x?gameInstanceId={JOB}",
             "job", PLACE, JOB),
            (f"https://www.roblox.com/games/{PLACE}", "open", PLACE, ""),
            (CODE, "share", "", CODE),
            (PLACE, "open", PLACE, ""),
        ]
        for raw, kind, place, code in cases:
            with self.subTest(raw=raw[:60]):
                parsed = api.parse_join_link(raw)
                self.assertIsNotNone(parsed, f"not recognised: {raw}")
                self.assertEqual(parsed.kind, kind)
                self.assertEqual(parsed.place, place)
                self.assertEqual(parsed.code or parsed.job_id or "", code)

    def test_rejects_junk(self):
        for raw in ("", "   ", "not a link", "hello there", "https://example.com"):
            with self.subTest(raw=raw):
                self.assertIsNone(api.parse_join_link(raw))


class LaunchUriTests(unittest.TestCase):
    def test_private_server_uses_link_code(self):
        uri = api.build_launch_uri("TICKET", PLACE, link_code=CODE)
        pl = place_launcher(uri)
        self.assertIn(f"placeId={PLACE}", pl)
        self.assertIn("request=RequestGameJob", pl)
        self.assertIn(f"linkCode={CODE}", pl)
        # The old bug: a link code sent as gameId silently used matchmaking.
        self.assertNotIn("gameId=", pl)

    def test_specific_server_uses_job_id(self):
        uri = api.build_launch_uri("TICKET", PLACE, instance_id=JOB)
        pl = place_launcher(uri)
        self.assertIn(f"gameId={JOB}", pl)
        self.assertNotIn("linkCode=", pl)

    def test_open_server_uses_matchmaking(self):
        uri = api.build_launch_uri("TICKET", PLACE)
        pl = place_launcher(uri)
        self.assertIn("request=RequestGame", pl)
        self.assertNotIn("linkCode=", pl)
        self.assertNotIn("gameId=", pl)

    def test_invalid_ids_return_none(self):
        self.assertIsNone(api.build_launch_uri("T", "abc&x=1"))
        self.assertIsNone(api.build_launch_uri("T", PLACE, link_code="bad+code"))
        self.assertIsNone(api.build_launch_uri("T", PLACE, instance_id="a&b"))
        self.assertIsNone(api.build_launch_uri("T", ""))
        # The URI never contains the raw ticket in the place-launcher part.
        self.assertIsNone(api.build_launch_uri("T", PLACE, link_code=CODE * 3))


class ResolveTests(unittest.TestCase):
    def test_direct_link_needs_no_network(self):
        link = f"https://www.roblox.com/games/{PLACE}/x?privateServerLinkCode={CODE}"
        with mock.patch.object(api, "resolve_share_api") as called:
            target = api.resolve_join_link("cookie", link)
        called.assert_not_called()
        self.assertTrue(target.ok)
        self.assertEqual(target.kind, "private")
        self.assertEqual(target.link_code, CODE)
        self.assertEqual(target.place, PLACE)

    def test_share_link_is_resolved_and_retried(self):
        calls = {"n": 0}

        def fake_api(cookie, code, link_type, log=None):
            calls["n"] += 1
            if calls["n"] < 3:
                return None                      # transient Roblox failure
            return api.JoinTarget(place=PLACE, kind="private", link_code=CODE)

        link = f"https://www.roblox.com/share?code={CODE}&type=Server"
        with mock.patch.object(api, "resolve_share_api", fake_api), \
                mock.patch.object(api.time, "sleep", lambda _s: None):
            target = api.resolve_join_link("cookie", link)
        self.assertTrue(target.ok)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(target.link_code, CODE)

    def test_unresolvable_link_fails_loudly(self):
        link = f"https://www.roblox.com/share?code={CODE}&type=Server"
        with mock.patch.object(api, "resolve_share_api", return_value=None), \
                mock.patch.object(api.time, "sleep", lambda _s: None):
            target = api.resolve_join_link("cookie", link)
        self.assertFalse(target.ok)
        self.assertTrue(target.error)
        # The whole point: no place to fall back into public matchmaking.
        self.assertEqual(target.place, "")

    def test_private_response_is_searched_by_key_name(self):
        body = ('{"privateServerInviteData":{"status":"Valid",'
                f'"ownerUserId":45348281,"universeId":6430220996,'
                '"privateServerId":1630945839,'
                '"linkCode":"677714137474916842868586666824242",'
                f'"placeId":{PLACE}}}}}')
        with mock.patch.object(api, "_post") as post, \
                mock.patch.object(api, "get_csrf", return_value="csrf"):
            post.return_value = mock.MagicMock(
                read=lambda: body.encode())
            target = api.resolve_share_api("cookie", CODE, "Server")
        self.assertIsNotNone(target)
        self.assertEqual(target.place, PLACE)
        self.assertEqual(target.kind, "private")
        self.assertTrue(target.link_code)

    def test_experience_invite_returns_open_target(self):
        body = '{"experienceInviteData":{"placeId":%s,"universeId":643}}' % PLACE
        with mock.patch.object(api, "_post") as post, \
                mock.patch.object(api, "get_csrf", return_value="csrf"):
            post.return_value = mock.MagicMock(
                read=lambda: body.encode())
            target = api.resolve_share_api("cookie", CODE, "ExperienceInvite")
        self.assertEqual(target.place, PLACE)
        self.assertEqual(target.kind, "open")
        self.assertFalse(target.link_code)


class EngineTargetTests(unittest.TestCase):
    def _engine(self, accounts, flags=None, app_icon=""):
        eng = engine.MonitorEngine(accounts, flags or {"join_server": True},
                                   app_icon=app_icon)
        eng.log = mock.MagicMock()
        eng.state = mock.MagicMock()
        eng.status = mock.MagicMock()
        return eng

    def _account(self, uid="1", username="alice", rjoin="", rplace="1"):
        return {"user_id": uid, "username": username, "cookie": "cookie",
                "rplace": rplace, "runiverse": "", "rdelay": 1,
                "rjoin": rjoin, "join_on": True}

    def test_account_with_link_launches_into_private_server(self):
        link = f"https://www.roblox.com/games/{PLACE}/x?privateServerLinkCode={CODE}"
        acc = self._account(rjoin=link)
        eng = self._engine([acc])
        target = api.JoinTarget(place=PLACE, kind="private", link_code=CODE)
        with mock.patch.object(engine, "resolve_join_link",
                               return_value=target), \
                mock.patch.object(engine, "probe_join_server",
                                  return_value=(JOB, 2, "")), \
                mock.patch.object(engine, "get_auth_ticket",
                                  return_value="TICKET"), \
                mock.patch.object(engine, "roblox_pids",
                                  side_effect=[set(), {4242}]), \
                mock.patch.object(engine.os, "startfile",
                                  create=True) as start:
            state = eng._resolve_join(acc, 0.0, force=True)
            self.assertIsNotNone(state)
            self.assertEqual(acc["rplace"], PLACE)
            pid, err = eng._launch_one(acc)
        self.assertIsNone(err)
        self.assertIsNotNone(pid)
        uri = start.call_args[0][0]
        pl = place_launcher(uri)
        self.assertIn(f"placeId={PLACE}", pl)
        self.assertIn(f"linkCode={CODE}", pl)
        self.assertNotIn("gameId=", pl)
        # The expected server is remembered for wrong-server detection.
        self.assertEqual(eng._links[link]["ref"], JOB)

    def test_unresolvable_link_holds_the_account_back(self):
        link = "https://www.roblox.com/share?code=abc&type=Server"
        acc = self._account(rjoin=link)
        eng = self._engine([acc])
        with mock.patch.object(engine, "resolve_join_link",
                               return_value=api.JoinTarget(
                                   error="Roblox would not resolve")), \
                mock.patch.object(engine, "get_auth_ticket",
                                  return_value="TICKET"), \
                mock.patch.object(engine.os, "startfile",
                                  create=True) as start:
            state = eng._resolve_join(acc, 0.0, force=True)
        self.assertIsNone(state)
        self.assertFalse(start.called)     # never launches into a public server
        self.assertEqual(acc["rplace"], "1")
        self.assertEqual(eng._links[link]["tries"], 1)

    def test_give_up_after_five_attempts(self):
        link = "https://www.roblox.com/share?code=abc&type=Server"
        acc = self._account(rjoin=link)
        eng = self._engine([acc])
        with mock.patch.object(engine, "resolve_join_link",
                               return_value=api.JoinTarget(error="nope")):
            for i in range(6):
                eng._resolve_join(acc, float(i * 60), force=(i == 0))
        self.assertGreaterEqual(eng._links[link]["tries"], 5)
        tries = eng._links[link]["tries"]
        st = {"state": "waiting", "rejoin_at": 0.0}
        eng._retry_join(acc, st, 999.0)
        self.assertEqual(eng._links[link]["tries"], tries)
        self.assertEqual(eng._links[link]["gave_up"], True)
        self.assertEqual(st["state"], "stopped")

    def test_embed_icon_is_the_app_badge(self):
        """Embeds are branded with the app's own icon, not a game thumbnail."""
        with tempfile.TemporaryDirectory() as tmp:
            icon = Path(tmp) / "icon.png"
            icon.write_bytes(b"\x89PNG app badge")
            eng = self._engine([], app_icon=str(icon))
            eng._load_icon()
            self.assertEqual(eng._icon_bytes, b"\x89PNG app badge")
        empty = self._engine([], app_icon="")
        empty._load_icon()
        self.assertIsNone(empty._icon_bytes)

    def test_no_link_when_join_server_is_off(self):
        link = f"https://www.roblox.com/games/{PLACE}/x?privateServerLinkCode={CODE}"
        acc = self._account(rjoin=link)
        eng = self._engine([acc], flags={"join_server": False})
        self.assertIsNone(eng._resolve_join(acc, 0.0))
        self.assertEqual(eng._targets.get(acc["user_id"]), None)

    def test_majority_server_beats_a_stale_lookup(self):
        """Two *accounts* in one server out-vote a stale link lookup."""
        eng = self._engine([])
        key = "a link"
        eng._links[key] = {"ref": "stale-job"}
        eng._note_server(key, 1, "fresh-job", 1.0)
        eng._note_server(key, 2, "fresh-job", 2.0)
        self.assertEqual(eng._server_reference(key), ("fresh-job", 2))

    def test_single_account_uses_the_lookup(self):
        eng = self._engine([])
        key = "a link"
        eng._links[key] = {"ref": JOB}
        eng._note_server(key, 1, JOB, 1.0)
        self.assertEqual(eng._server_reference(key), (JOB, 1))

    def test_one_account_cannot_vote_itself_correct(self):
        """The reported false positive: repeated polls from a single account
        must never turn its own (public) server into the reference."""
        eng = self._engine([])
        key = "a link"
        eng._links[key] = {"ref": JOB}
        for tick in range(5):
            eng._note_server(key, 7, "public-job", float(tick))
        self.assertEqual(eng._server_reference(key), (JOB, 1))

    def test_an_account_moving_servers_updates_its_vote(self):
        eng = self._engine([])
        key = "a link"
        eng._links[key] = {"ref": ""}
        eng._note_server(key, 1, "job-a", 1.0)
        eng._note_server(key, 1, "job-b", 2.0)
        eng._note_server(key, 2, "job-b", 3.0)
        self.assertEqual(eng._server_reference(key), ("job-b", 2))


if __name__ == "__main__":
    unittest.main()
