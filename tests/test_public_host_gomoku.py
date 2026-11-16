"""Actual local game rules/lifecycle; web and speech adapters are explicit fakes."""

import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from core.event_bus import EventBus
from minigames.gomoku import GomokuAI, GomokuEngine, GomokuResultAnnouncer
from minigames.result_speech import ResultSpeechCoordinator
from public_host.activities.gomoku import GomokuActivity


ROOT = Path(__file__).resolve().parents[1]


class FakePlatform:
    def __init__(self):
        self.running = False
        self.port = 0
        self.host = "127.0.0.1"
        self.active_game_id = None
        self.state = None

    @property
    def url(self):
        return f"http://{self.host}:{self.port}/"

    def start(self, *, port, **kwargs):
        self.port, self.running = port, True

    def shutdown(self, **kwargs):
        self.running = False


class FakeBrowser:
    def __init__(self, html_path, *, game_id, platform):
        self.platform, self.game_id = platform, game_id

    @property
    def url(self):
        return self.platform.url

    @property
    def port(self):
        return self.platform.port

    def start(self, *, port, **kwargs):
        self.platform.start(port=port)
        self.platform.active_game_id = self.game_id

    def publish_state(self, state):
        self.platform.state = state

    def stop(self, **kwargs):
        self.platform.active_game_id = None


FAKE_WEB = SimpleNamespace(MinigamePlatformServer=FakePlatform, MinigameWebBridge=FakeBrowser)


class GomokuRulesTests(unittest.TestCase):
    def test_real_rules_win_and_reject_further_move(self):
        engine = GomokuEngine()
        for move in [(7, 3), (0, 0), (7, 4), (0, 1), (7, 5), (0, 2), (7, 6), (1, 0), (7, 7)]:
            engine.place(*move)
        self.assertEqual(engine.winner, 1)
        self.assertEqual(engine.move_count, 9)
        self.assertEqual(engine.winning_line, [[7, column] for column in range(3, 8)])
        with self.assertRaisesRegex(ValueError, "finished"):
            engine.place(4, 4)

    def test_bot_blocks_opponent_immediate_win(self):
        engine = GomokuEngine()
        for column in range(4):
            engine.board[7][column] = 2
        engine.move_count = 4
        self.assertEqual(GomokuAI(seed=1).choose(engine), (7, 4))

    def test_optional_aiohttp_is_not_imported_by_host_or_rules(self):
        code = "import sys; import public_host.activities.gomoku; import minigames.gomoku; assert 'aiohttp' not in sys.modules"
        completed = subprocess.run([sys.executable, "-S", "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_missing_aiohttp_fails_with_install_hint_and_no_subscription_leak(self):
        code = """from core.event_bus import EventBus
from public_host.activities.gomoku import GomokuActivity
bus = EventBus()
activity = GomokuActivity(bus, ['Alpha', 'Beta'])
try:
    activity.prepare()
except RuntimeError as error:
    assert 'requirements-host.txt' in str(error), str(error)
else:
    raise AssertionError('optional dependency unexpectedly available under -S')
activity.close()
assert all(not callbacks for callbacks in bus._subscribers.values())
"""
        completed = subprocess.run([sys.executable, "-S", "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_two_distinct_configured_roles_required(self):
        for names in (["Only"], ["Same", "same"], ["One", "  "], ["One", "Two", "Three"]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                GomokuActivity(EventBus(), names)


class ResultSpeechTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.coordinator = ResultSpeechCoordinator(self.bus)
        self.addCleanup(self.coordinator.close)

    def dispatch(self, instruction, task_id="wanted"):
        self.bus.publish("director_instruction_dispatched", {
            "instruction_id": instruction.data["instruction_id"],
            "original_text": instruction.data["text"], "speaker": instruction.data["speaker"],
            "task_id": task_id,
        }, source="test")

    def test_only_matching_real_completion_releases_wait(self):
        dispatched = threading.Event()
        result = []
        def on_instruction(event):
            self.dispatch(event)
            self.bus.publish("tts_done", {"task_id": "other"}, source="test")
            dispatched.set()
        self.bus.subscribe("director_instruction", on_instruction)
        worker = threading.Thread(target=lambda: result.append(self.coordinator.speak(
            "result", speaker="Player", dispatch_timeout=1, speech_timeout=2)))
        worker.start()
        self.assertTrue(dispatched.wait(1))
        self.assertTrue(worker.is_alive())
        self.bus.publish("tts_done", {"task_id": "wanted"}, source="test")
        worker.join(2)
        self.assertEqual(result, [True])

    def test_cancelled_terminal_is_not_overwritten_by_late_success(self):
        def on_instruction(event):
            self.dispatch(event)
            self.bus.publish("speech_task_terminal", {"task_id": "wanted", "status": "cancelled"}, source="test")
            self.bus.publish("tts_done", {"task_id": "wanted"}, source="late")
        self.bus.subscribe("director_instruction", on_instruction)
        self.assertFalse(self.coordinator.speak("result", speaker="Player", speech_timeout=.1))

    def test_demo_terminal_finishes_without_claiming_audio(self):
        actual_audio = []
        self.bus.subscribe("tts_done", lambda event: actual_audio.append(event))
        def on_instruction(event):
            self.dispatch(event)
            self.bus.publish("tts_simulated", {"task_id": "wanted"}, source="demo")
        self.bus.subscribe("director_instruction", on_instruction)
        self.assertTrue(self.coordinator.speak("result", speaker="Player", speech_timeout=.1))
        self.assertEqual(actual_audio, [])

    def test_stale_instruction_id_cannot_claim_new_same_text(self):
        def on_instruction(event):
            self.bus.publish("director_instruction_dispatched", {
                "original_text": event.data["text"], "speaker": event.data["speaker"],
                "instruction_id": "previous-instruction", "task_id": "stale",
            }, source="test")
            self.bus.publish("tts_done", {"task_id": "stale"}, source="test")
        self.bus.subscribe("director_instruction", on_instruction)
        self.assertFalse(self.coordinator.speak("repeat", speaker="Player", dispatch_timeout=.03))

    def test_result_announcer_fails_when_line_not_completed(self):
        announcer = GomokuResultAnnouncer(SimpleNamespace(speak=lambda *a, **kw: False))
        with self.assertRaises(RuntimeError):
            announcer({"winner": "Alpha", "loser": "Beta", "scores": {"Alpha": 1, "Beta": 0}}, 1)


class GomokuLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.web_patch = patch.dict(sys.modules, {"minigames.web_bridge": FAKE_WEB})
        self.web_patch.start()
        self.addCleanup(self.web_patch.stop)
        self.bus = EventBus()
        self.activity = GomokuActivity(self.bus, ["Alpha", "Beta"], move_interval=.001, seed=5)
        self.addCleanup(self.activity.close)

    def test_prepare_does_not_start_round_activate_really_moves_stop_keeps_platform(self):
        moved = threading.Event()
        self.bus.subscribe("game_event", lambda e: moved.set() if e.data.get("event_type") == "stone_placed" else None)
        prepared = self.activity.prepare()
        self.assertEqual(prepared["status"], "ready")
        self.assertFalse(prepared["running"])
        self.assertEqual(prepared["session_id"], "")
        self.assertEqual(prepared["state"]["move_count"], 0)
        self.assertEqual(set(prepared["state"]["scores"]), {"Alpha", "Beta"})
        self.assertIsNone(self.activity._host._worker)
        activated = self.activity.activate()
        self.assertEqual(activated["status"], "active")
        self.assertTrue(activated["session_id"])
        self.assertTrue(moved.wait(2))
        stopped = self.activity.stop()
        self.assertTrue(stopped["stopped"])
        self.assertFalse(stopped["running"])
        self.assertTrue(stopped["platform_running"])
        self.assertIsNone(self.activity._host.browser.platform.active_game_id)
        old_id = activated["session_id"]
        self.activity.prepare()
        restarted = self.activity.activate()
        self.assertNotEqual(restarted["session_id"], old_id)

    def test_stop_timeout_remains_stopping_and_never_claims_closed(self):
        self.activity.prepare()
        entered, release = threading.Event(), threading.Event()
        original = self.activity._host.action_provider.choose_action
        def blocked(decision):
            entered.set()
            release.wait(3)
            return original(decision)
        self.activity._host.action_provider.choose_action = blocked
        self.activity.activate()
        self.assertTrue(entered.wait(2))
        worker = self.activity._host._worker
        try:
            with patch.object(worker, "join"), self.assertRaises(TimeoutError):
                self.activity.stop()
            state = self.activity.snapshot()
            self.assertEqual(state["phase"], "stopping")
            self.assertFalse(state["closed"])
            self.assertFalse(state["stopped"])
            self.assertTrue(state["running"])
        finally:
            release.set()
            worker.join(3)
        stopped = self.activity.stop()
        self.assertEqual(stopped["state"]["move_count"], 0)

    def test_cancel_before_prepare_creates_no_service(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(RuntimeError):
            self.activity.prepare(cancelled)
        self.assertIsNone(self.activity._host)

    def test_close_releases_server_and_bus_subscriptions(self):
        self.activity.prepare()
        self.activity.activate()
        state = self.activity.close()
        self.assertFalse(state["platform_running"])
        self.assertTrue(state["platform_closed"])
        self.assertTrue(all(not callbacks for callbacks in self.bus._subscribers.values()))

    def test_demo_round_finishes_only_after_both_simulated_result_lines(self):
        completed, terminal, spoken, real_audio = threading.Event(), [], [], []
        self.bus.subscribe("tts_done", lambda e: real_audio.append(e))
        def on_instruction(event):
            spoken.append(event.data["speaker"])
            task = event.data["instruction_id"]
            self.bus.publish("director_instruction_dispatched", {
                "instruction_id": task, "original_text": event.data["text"],
                "speaker": event.data["speaker"], "task_id": task,
            }, source="demo")
            self.bus.publish("tts_simulated", {"task_id": task}, source="demo")
            terminal.append(task)
        self.bus.subscribe("director_instruction", on_instruction)
        self.bus.subscribe("game_event", lambda e: completed.set() if e.data.get("event_type") == "round_complete" else None)
        self.activity.prepare()
        self.activity._host.round_timeout = .02
        self.activity._host.round_pause = 10
        self.activity.activate()
        self.assertTrue(completed.wait(3))
        self.assertEqual(spoken, ["Alpha", "Beta"])
        self.assertEqual(len(terminal), 2)
        self.assertEqual(real_audio, [])
        self.assertTrue(self.activity.snapshot()["result"]["draw"])

    def test_finalizing_uses_director_clock_and_does_not_count_incomplete_round(self):
        moved = threading.Event()
        self.bus.subscribe("game_event", lambda e: moved.set() if e.data.get("event_type") == "stone_placed" else None)
        self.activity.prepare()
        self.activity.activate()
        self.assertTrue(moved.wait(2))
        self.bus.publish("activity_finalizing", {"game_id": "gomoku", "segment_index": 2, "clock_revision": 1}, source="director")
        result = self.activity.snapshot()["result"]
        self.assertEqual(result["scores"], {"Alpha": 0, "Beta": 0})
        self.assertEqual(result["aborted_round_no"], 1)
        self.assertTrue(result["draw"])


@unittest.skipUnless(importlib.util.find_spec("aiohttp"), "optional aiohttp not installed")
class GomokuLoopbackTests(unittest.TestCase):
    def test_actual_http_serves_board_and_shutdown_releases_own_port(self):
        activity = GomokuActivity(EventBus(), ["Alpha", "Beta"], port=0)
        self.addCleanup(activity.close)
        ready = activity.prepare()
        with urlopen(ready["url"] + "games/gomoku/", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn('id="board"', page)
        self.assertNotIn("Lumi", page)
        self.assertNotIn("Nox", page)
        port = activity._host.browser.port
        activity.close()
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def test_port_conflict_fails_without_taking_other_service(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            activity = GomokuActivity(EventBus(), ["Alpha", "Beta"], port=port)
            self.addCleanup(activity.close)
            with self.assertRaisesRegex(RuntimeError, str(port)):
                activity.prepare()
            self.assertFalse(activity.snapshot()["ready"])
            self.assertFalse(activity.snapshot()["running"])
            self.assertEqual(occupied.getsockname()[1], port)


if __name__ == "__main__":
    unittest.main()
