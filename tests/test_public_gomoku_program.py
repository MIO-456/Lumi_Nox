"""Full programme with the real Gomoku worker and explicit silent demo services."""

from datetime import timedelta
import importlib.util
import threading
import time
import unittest

from public_host.activities.gomoku import GomokuActivity
from public_host.config import default_config, runtime_config
from public_host.playback import make_player
from public_host.program import Program
from public_host.providers import make_services
from public_host.session import Session


@unittest.skipUnless(importlib.util.find_spec("aiohttp"), "optional aiohttp not installed")
class RealGomokuProgramTests(unittest.TestCase):
    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            threading.Event().wait(.005)
        self.fail("real programme did not reach expected state")

    def start_program(self):
        config = default_config()
        config["mode"] = "demo"
        config["roles"][0]["name"] = "Alpha"
        config["roles"][1]["name"] = "Beta"
        config["roles"][1]["enabled"] = True
        config["program"]["auto_chat_interval"] = .1
        for stage in config["program"]["stages"]:
            stage["duration"] = 60 if stage["kind"] == "activity" else .12
        config = runtime_config(config)
        chat, synth = make_services(config)
        session = Session(config, chat=chat, synth=synth, player=make_player(config), managed=True)
        activities = []
        def factory(bus, roles, port):
            activity = GomokuActivity(bus, roles, port=0, move_interval=.005, round_timeout=.08, seed=6)
            activities.append(activity)
            return activity
        program = Program(config, session, activity_factory=factory, interval=.005)
        events = []
        for event_type in ("state_changed", "game_event", "segment_clock", "match_finalized", "tts_simulated", "tts_done"):
            program.bus.subscribe(event_type, lambda event: events.append((event.event_type, event.data)))
        def cleanup():
            program.stop()
            self.wait(lambda: not program.snapshot()["running"])
            self.wait(lambda: not session.snapshot()["running"])
            for activity in activities:
                activity.close()
        self.addCleanup(cleanup)
        session.start()
        program.start()
        self.wait(lambda: program.snapshot()["phase"] == "activity")
        self.wait(lambda: any(kind == "game_event" and data.get("event_type") == "stone_placed" for kind, data in events))
        self.assertIs(program.bus, session.bus)
        self.assertIs(program.state_machine, session.state_machine)
        self.assertEqual(session.snapshot()["state"], "PLAYING_MINIGAME")
        self.assertEqual(session.snapshot()["state_metadata"]["game_id"], "gomoku")
        self.assertEqual(session.snapshot()["state_metadata"]["instance_id"], activities[0].snapshot()["instance_id"])
        return program, session, activities[0], events

    def assert_states(self, events, expected):
        changed = [data for kind, data in events if kind == "state_changed"]
        self.assertEqual([data["new"] for data in changed if data["new"] != "TRANSITIONING"], expected)
        self.assertEqual(changed[0]["old"], "IDLE")
        self.assertEqual(changed[-1]["new"], "IDLE")
        for before, after in zip(changed, changed[1:]):
            self.assertEqual(before["new"], after["old"])
        active = next(data for data in changed if data["new"] == "PLAYING_MINIGAME")
        self.assertEqual(active["metadata"]["game_id"], "gomoku")
        self.assertTrue(active["metadata"]["instance_id"])

    def test_real_game_deadline_settles_then_chats_and_ends_without_claiming_audio(self):
        program, session, activity, events = self.start_program()
        self.assertTrue(activity.snapshot()["running"])
        self.wait(lambda: any(kind == "game_event" and data.get("event_type") == "round_complete" for kind, data in events))
        with program._lock:
            program._started_at -= timedelta(seconds=61)
        self.wait(lambda: not program.snapshot()["running"])
        status = program.snapshot()
        self.assertEqual(status["phase"], "finished", status)
        started = [e["data"]["index"] for e in status["events"] if e["event_type"] == "stage_started"]
        self.assertEqual(started, [0, 1, 2, 3, 4])
        results = [data for kind, data in events if kind == "match_finalized"]
        self.assertEqual(len(results), 1)
        self.assertEqual(set(results[0]["scores"]), {"Alpha", "Beta"})
        self.assertEqual(results[0]["segment_index"], 2)
        self.assertEqual(activity.snapshot()["result"], results[0])
        self.assertTrue(any(kind == "segment_clock" for kind, _ in events))
        self.assertTrue(any(kind == "tts_simulated" for kind, _ in events))
        settled = [e for e in status["events"] if e["event_type"] == "match_finalized"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["data"]["speech_outcome"], "simulated")
        self.assertFalse(any(kind == "tts_done" for kind, _ in events))
        self.assertFalse(activity.snapshot()["running"])
        self.assertFalse(activity.snapshot()["platform_running"])
        self.assertFalse(session.snapshot()["running"])
        self.assertEqual(session.snapshot()["state"], "IDLE")
        self.assertEqual(session.snapshot()["state_metadata"], {})
        self.assert_states(events, ["OPENING", "CHATTING", "PLAYING_MINIGAME", "CHATTING", "ENDING", "IDLE"])

    def test_skip_real_game_settles_before_next_chat(self):
        program, _, activity, events = self.start_program()
        program.next()
        self.wait(lambda: not program.snapshot()["running"])
        status = program.snapshot()
        self.assertEqual(status["phase"], "finished", status)
        results = [data for kind, data in events if kind == "match_finalized"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["scores"])
        self.assertFalse(activity.snapshot()["platform_running"])
        self.assert_states(events, ["OPENING", "CHATTING", "PLAYING_MINIGAME", "CHATTING", "ENDING", "IDLE"])

    def test_end_from_real_game_settles_then_enters_ending_on_shared_state(self):
        program, session, activity, events = self.start_program()
        program.end()
        self.wait(lambda: not program.snapshot()["running"])
        self.assertEqual(program.snapshot()["phase"], "finished")
        self.assertFalse(activity.snapshot()["platform_running"])
        self.assertEqual(session.snapshot()["state"], "IDLE")
        self.assert_states(events, ["OPENING", "CHATTING", "PLAYING_MINIGAME", "ENDING", "IDLE"])

    def test_immediate_stop_cancels_real_game_and_releases_platform(self):
        program, session, activity, events = self.start_program()
        program.stop()
        self.wait(lambda: not program.snapshot()["running"])
        self.assertEqual(program.snapshot()["phase"], "stopped")
        self.assertFalse(program.snapshot()["blocked"])
        self.assertFalse(activity.snapshot()["running"])
        self.assertFalse(activity.snapshot()["platform_running"])
        self.assertFalse(session.snapshot()["running"])
        self.assertEqual(session.snapshot()["state"], "IDLE")
        self.assert_states(events, ["OPENING", "CHATTING", "PLAYING_MINIGAME", "ENDING", "IDLE"])


if __name__ == "__main__":
    unittest.main()
