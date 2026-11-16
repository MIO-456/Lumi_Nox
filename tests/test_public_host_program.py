"""Program behavior using the real speech worker and isolated fake activities."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest

from public_host.config import ConfigStore, default_config, runtime_config, validate_program
from public_host.program import Program
from public_host.providers import ProviderError
from public_host.session import Session


class Services:
    def __init__(self, outcome="complete"):
        self.calls = []
        self.outcome = outcome
        self.hold_call = None
        self.hold_text = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, cancel):
        self.calls.append(messages)
        if len(self.calls) == self.hold_call or (self.hold_text and self.hold_text in messages[-1]["content"]):
            self.entered.set()
            if not self.release.wait(4):
                raise AssertionError("held test request was not released")
        return "A short reply."

    def synthesize(self, text, voice, cancel):
        return text

    def play(self, clip, cancel, on_level):
        return self.outcome


class Activity:
    def __init__(self, bus, roles, port):
        self.bus, self.roles, self.port = bus, roles, port
        self.calls = []
        self.state = {"status": "idle", "url": "http://127.0.0.1:8791/", "last_error": ""}
        self.state["session_id"] = "fake-game-instance"
        self.fail_ready = False
        self.fail_stop = False
        bus.subscribe("activity_finalizing", self.finalize)

    def finalize(self, event):
        self.state["result"] = {"phase": "match_over", "session_id": self.state["session_id"],
            "segment_index": event.data["segment_index"], "clock_revision": event.data["clock_revision"],
            "scores": dict.fromkeys(self.roles, 0), "winner": None, "draw": True}

    def prepare(self, cancel_event=None):
        self.calls.append("prepare")
        self.state["status"] = "failed" if self.fail_ready else "ready"
        return dict(self.state)

    def activate(self, cancel_event=None):
        self.calls.append("activate")
        self.state["status"] = "active"
        return dict(self.state)

    def stop(self, reason="stopped"):
        self.calls.append("stop")
        if self.fail_stop:
            return {"status": "stopping", "closed": False, "gameClosed": False}
        self.state["status"] = "stopped"
        return {**self.state, "closed": True, "gameClosed": True}

    def close(self):
        self.calls.append("close")

    def snapshot(self):
        return dict(self.state)


def short_config():
    config = default_config()
    config["mode"] = "demo"
    config["roles"][1]["enabled"] = True
    for stage in config["program"]["stages"]:
        stage["duration"] = 0.12
    config["program"]["auto_chat_interval"] = 0.1
    return runtime_config(config)


class PublicProgramTests(unittest.TestCase):
    def wait(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            threading.Event().wait(0.005)
        self.fail("program did not reach expected state")

    def start_program(self, config=None, services=None, activity_setup=None):
        config, services = config or short_config(), services or Services()
        session = Session(config, chat=services, synth=services, player=services, managed=True)
        activities = []

        def factory(*args):
            activity = Activity(*args)
            if activity_setup:
                activity_setup(activity)
            activities.append(activity)
            return activity

        program = Program(config, session, activity_factory=factory, interval=0.005)

        def cleanup():
            services.release.set()
            for activity in activities:
                activity.fail_stop = False
            program.stop()
            self.wait(lambda: not program.snapshot()["running"])
            if program.snapshot()["blocked"]:
                program.stop()
                self.wait(lambda: not program.snapshot()["running"])
            self.wait(lambda: not session.snapshot()["running"])

        self.addCleanup(cleanup)
        session.start()
        program.start()
        return program, session, services, activities

    def test_complete_program_uses_ready_activate_stop_before_following_chat(self):
        program, _, services, activities = self.start_program()
        self.wait(lambda: not program.snapshot()["running"])
        status = program.snapshot()
        self.assertEqual(status["phase"], "finished", status)
        stages = [e["data"]["index"] for e in status["events"] if e["event_type"] == "stage_started"]
        self.assertEqual(stages, [0, 1, 2, 3, 4])
        self.assertEqual(activities[0].calls, ["prepare", "activate", "stop", "close"])
        self.assertGreaterEqual(len(services.calls), 4)

    def test_auto_chat_has_only_one_inflight_and_stop_waits_for_real_worker(self):
        config, services = short_config(), Services()
        config["program"]["stages"][1]["duration"] = 30
        services.hold_call = 2
        program, session, _, _ = self.start_program(config, services)
        self.assertTrue(services.entered.wait(3))
        time.sleep(0.15)
        self.assertEqual(len(services.calls), 2)
        self.assertEqual(session.snapshot()["queue_size"], 0)
        program.stop()
        self.assertTrue(program.snapshot()["running"])
        services.release.set()
        self.wait(lambda: not program.snapshot()["running"])
        self.assertEqual(program.snapshot()["phase"], "stopped")
        self.assertFalse(program.snapshot()["blocked"])

    def test_failed_prepare_does_not_activate_or_enter_following_chat(self):
        program, _, _, activities = self.start_program(activity_setup=lambda a: setattr(a, "fail_ready", True))
        self.wait(lambda: not program.snapshot()["running"])
        status = program.snapshot()
        self.assertEqual(status["phase"], "failed", status)
        self.assertNotIn("activate", activities[0].calls)
        self.assertIn("close", activities[0].calls)
        self.assertEqual(status["stage_index"], 1)

    def test_session_failure_while_preparing_prevents_activity_activation(self):
        holder = {}

        def setup(activity):
            original = activity.prepare

            def prepare(cancel_event=None):
                holder["session"].stop()
                self.wait(lambda: not holder["session"].snapshot()["running"])
                return original(cancel_event)
            activity.prepare = prepare

        program, session, _, activities = self.start_program(activity_setup=setup)
        holder["session"] = session
        self.wait(lambda: not program.snapshot()["running"])
        self.assertEqual(program.snapshot()["phase"], "failed")
        self.assertNotIn("activate", activities[0].calls)
        self.assertEqual(program.snapshot()["stage_index"], 1)

    def test_final_score_speech_must_finish_before_activity_stop_and_next_chat(self):
        services = Services()
        services.hold_text = "最终比分"
        program, _, _, activities = self.start_program(services=services)
        self.assertTrue(services.entered.wait(3))
        self.assertEqual(program.snapshot()["phase"], "finalizing")
        self.assertEqual(program.snapshot()["stage_index"], 2)
        self.assertNotIn("stop", activities[0].calls)
        services.release.set()
        self.wait(lambda: not program.snapshot()["running"])
        self.assertEqual(program.snapshot()["phase"], "finished")
        finalized = [e for e in program.snapshot()["events"] if e["event_type"] == "match_finalized"]
        self.assertEqual(finalized[0]["data"]["speech_outcome"], "complete")

    def test_unconfirmed_stop_blocks_next_stage_and_can_retry_cleanup(self):
        program, session, _, activities = self.start_program(activity_setup=lambda a: setattr(a, "fail_stop", True))
        self.wait(lambda: not program.snapshot()["running"])
        status = program.snapshot()
        self.assertEqual(status["phase"], "cleanup_failed", status)
        self.assertTrue(status["blocked"])
        self.assertEqual(status["stage_index"], 2)
        self.assertNotIn("close", activities[0].calls)
        self.assertEqual(session.snapshot()["state"], "TRANSITIONING")
        self.assertEqual(session.snapshot()["state_metadata"]["from_game_id"], "gomoku")
        self.assertEqual(session.snapshot()["state_metadata"]["to"], "CHATTING")
        activities[0].fail_stop = False
        program.stop()
        self.wait(lambda: not program.snapshot()["running"])
        self.assertFalse(program.snapshot()["blocked"])
        self.assertIn("close", activities[0].calls)
        self.assertEqual(session.snapshot()["state"], "IDLE")

    def test_exact_game_speech_terminal_is_distinct_from_demo(self):
        config = short_config()
        config["program"]["stages"][2]["duration"] = 30
        program, _, _, activities = self.start_program(config, Services("simulated"))
        self.wait(lambda: program.snapshot()["phase"] == "activity")
        dispatched, real_done, simulated = [], [], []
        bus = activities[0].bus
        bus.subscribe("director_instruction_dispatched", lambda e: dispatched.append(e.data))
        bus.subscribe("tts_done", lambda e: real_done.append(e.data))
        bus.subscribe("tts_simulated", lambda e: simulated.append(e.data))
        bus.publish("director_instruction", {"text": "game result", "speaker": program.roles[0],
                    "instruction_id": "round-1"}, source="test")
        self.wait(lambda: simulated)
        self.assertEqual(dispatched[0]["instruction_id"], "round-1")
        self.assertEqual(simulated[0]["task_id"], dispatched[0]["task_id"])
        self.assertEqual(real_done, [])
        old_callback = program._instruction_callback
        program.next()
        self.wait(lambda: program.snapshot()["stage_index"] == 3)
        before = len(dispatched)
        event = type("Event", (), {"data": {"text": "late", "speaker": program.roles[0]}})()
        old_callback(event)
        time.sleep(0.03)
        self.assertEqual(len(dispatched), before)

    def test_end_requests_ending_once_and_finishes_after_its_speech(self):
        config = short_config()
        config["program"]["stages"][1]["duration"] = 30
        program, _, _, activities = self.start_program(config)
        self.wait(lambda: program.snapshot()["phase"] == "chat")
        program.end()
        program.end()
        self.wait(lambda: not program.snapshot()["running"])
        started = [e["data"]["index"] for e in program.snapshot()["events"] if e["event_type"] == "stage_started"]
        self.assertEqual(started, [0, 1, 4])
        self.assertEqual(activities, [])


class ProgramConfigTests(unittest.TestCase):
    def test_legacy_config_load_and_save_preserve_keys_roles_and_rundown(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            old = default_config()
            old.pop("program")
            old["llm"]["api_key"] = "private-test-key"
            old["roles"][0]["voice"] = "own-voice"
            store.path.write_text(json.dumps(old), encoding="utf-8")
            before = store.path.read_bytes()
            migrated = store.public()
            self.assertIn("program", migrated)
            self.assertEqual(store.path.read_bytes(), before)
            migrated["program"]["auto_chat_interval"] = 7
            store.save(migrated)
            legacy_client = store.public()
            legacy_client.pop("program")
            store.save(legacy_client)
            restored = store.load()
            self.assertEqual(restored["llm"]["api_key"], "private-test-key")
            self.assertEqual(restored["roles"][0]["voice"], "own-voice")
            self.assertEqual(restored["program"]["auto_chat_interval"], 7)

    def test_gomoku_preflight_requires_two_enabled_roles(self):
        config = default_config()
        config["mode"] = "demo"
        runtime = runtime_config(config)
        with self.assertRaisesRegex(ProviderError, "两个不同角色"):
            validate_program(runtime["program"], roles=runtime["roles"])


if __name__ == "__main__":
    unittest.main()
