"""Portable startup and gameplay regressions; no game, keys or HTTP required."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from games.kingdom_rush import bot as bot_module
from games.kingdom_rush import startup as startup_module
from games.kingdom_rush.bridge import KingdomRushBridge


class FakeSocket:
    def __init__(self, on_connect=None, error=None):
        self.on_connect = on_connect
        self.error = error
        self.events = []
        self.closed = False

    def settimeout(self, value):
        self.events.append(("timeout", value))

    def connect(self, address):
        self.events.append(("connect", address))
        if self.on_connect:
            self.on_connect()
        if self.error:
            raise self.error

    def shutdown(self, how):
        self.events.append(("shutdown", how))

    def close(self):
        self.closed = True


class StartupTests(unittest.TestCase):
    def test_all_stages_share_one_deadline(self):
        with patch.object(startup_module.time, "monotonic", return_value=100.0) as clock:
            session = startup_module.StartupControl(timeout=60)
            clock.return_value = 155.0
            self.assertEqual(session.remaining(10), 5)
            clock.return_value = 161.0
            with self.assertRaises(startup_module.StartupTimeout):
                session.run(Mock())

    def test_cancelled_start_cannot_spawn_a_late_process(self):
        session = startup_module.StartupControl()
        session.cancel()
        with patch.object(startup_module.subprocess, "Popen") as popen:
            with self.assertRaises(startup_module.StartupCancelled):
                session.spawn(["must-not-launch"])
            popen.assert_not_called()

    def test_cancel_only_terminates_its_owned_process(self):
        process = Mock(pid=321)
        process.poll.return_value = None
        process.terminate.side_effect = lambda: setattr(process.poll, "return_value", 0)
        with patch.object(startup_module.subprocess, "Popen", return_value=process):
            session = startup_module.StartupControl()
            session.spawn(["fake-game"])
            session.cancel()
            session.cancel()
        self.assertEqual(session.process_id, 321)
        process.terminate.assert_called_once()
        process.kill.assert_not_called()

    def test_cancel_can_leave_owned_game_running(self):
        process = Mock(pid=321)
        with patch.object(startup_module.subprocess, "Popen", return_value=process):
            session = startup_module.StartupControl()
            session.spawn(["fake-game"])
            session.cancel(kill_game=False)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()


class SocketLifecycleTests(unittest.TestCase):
    def test_socket_timeout_precedes_connect_and_uses_remaining_budget(self):
        sock = FakeSocket()
        with patch.object(startup_module.time, "monotonic", return_value=100.0) as clock:
            session = startup_module.StartupControl(timeout=60)
            clock.return_value = 159.75
            with patch.object(bot_module.socket, "socket", return_value=sock):
                bot = bot_module.KingdomRushBot()
                self.assertTrue(bot.connect(retries=1, startup=session))
        self.assertEqual(sock.events[0], ("timeout", 0.25))
        self.assertEqual(sock.events[1][0], "connect")

    def test_failed_candidate_is_closed(self):
        sock = FakeSocket(error=ConnectionRefusedError())
        with patch.object(bot_module.socket, "socket", return_value=sock):
            bot = bot_module.KingdomRushBot()
            self.assertFalse(bot.connect(retries=1, quiet=True))
        self.assertTrue(sock.closed)
        self.assertIsNone(bot.sock)
        self.assertFalse(bot.connected)

    def test_cancel_during_connect_does_not_accept_socket(self):
        session = startup_module.StartupControl()
        sock = FakeSocket(on_connect=session.cancel)
        with patch.object(bot_module.socket, "socket", return_value=sock):
            bot = bot_module.KingdomRushBot()
            with self.assertRaises(startup_module.StartupCancelled):
                bot.connect(retries=1, startup=session)
        self.assertTrue(sock.closed)
        self.assertFalse(bot.connected)

    def test_close_during_connect_cannot_resurrect_connection(self):
        bot = bot_module.KingdomRushBot()
        sock = FakeSocket(on_connect=bot.close)
        with patch.object(bot_module.socket, "socket", return_value=sock):
            self.assertFalse(bot.connect(retries=1))
        self.assertTrue(sock.closed)
        self.assertIsNone(bot.sock)
        self.assertFalse(bot.connected)

    def test_old_attempt_cleanup_does_not_close_new_socket(self):
        bot = bot_module.KingdomRushBot()
        newer = FakeSocket()
        older = FakeSocket(on_connect=lambda: bot.connect(retries=1))
        with patch.object(bot_module.socket, "socket", side_effect=[older, newer]):
            self.assertFalse(bot.connect(retries=1))
        self.assertTrue(older.closed)
        self.assertFalse(newer.closed)
        self.assertIs(bot.sock, newer)
        self.assertTrue(bot.connected)


class BridgeHandshakeTests(unittest.TestCase):
    def run_fake_start(self, bridge, *, probe=None, welcome=None):
        calls = []

        class FakeBot:
            def connect(self, **kwargs):
                calls.append("probe")
                return probe() if probe else True

            def receive(self, **kwargs):
                return welcome

            def close(self):
                calls.append("close")

        fake_ai = ModuleType("games.kingdom_rush.ai")
        fake_ai.launch_game = Mock(return_value=False)
        fake_ai.trigger_launcher_start = Mock()
        fake_ai.auto_loop = Mock()
        with patch.object(bot_module, "KingdomRushBot", FakeBot), patch.dict(sys.modules, {fake_ai.__name__: fake_ai}):
            bridge.start()
            bridge._thread.join(timeout=2)
            self.assertFalse(bridge._thread.is_alive())
        return calls, fake_ai

    def test_tcp_without_bridge_welcome_is_not_ready(self):
        events = []
        bridge = KingdomRushBridge(event_callback=lambda kind, data: events.append(data))
        _, fake_ai = self.run_fake_start(bridge, welcome={"type": "unrelated-service"})
        self.assertFalse(bridge._startup.ready)
        self.assertIn("欢迎消息", bridge._startup.error)
        self.assertFalse(any(event.get("event") == "connected" for event in events))
        fake_ai.auto_loop.assert_not_called()

    def test_stop_during_probe_prevents_late_launch(self):
        bridge = KingdomRushBridge()

        def cancel_probe():
            bridge.stop(kill_game=False)
            return False

        _, fake_ai = self.run_fake_start(bridge, probe=cancel_probe)
        fake_ai.launch_game.assert_not_called()
        fake_ai.auto_loop.assert_not_called()
        self.assertFalse(bridge.running)


def load_offline_ai():
    """Load real decision code, replacing only its cloud boundary temporarily.

    The module is not registered globally, and patch.dict restores sys.modules;
    later tests cannot accidentally inherit the cloud adapter replacement.
    """
    name = "games.kingdom_rush.strategy_llm"
    boundary = ModuleType(name)
    boundary.call_llm_for_strategy = Mock(side_effect=AssertionError("cloud call in offline test"))
    path = Path(__file__).resolve().parents[1] / "games" / "kingdom_rush" / "ai.py"
    spec = importlib.util.spec_from_file_location("games.kingdom_rush._offline_test_ai", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: boundary}):
        spec.loader.exec_module(module)
    return module


class GameplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_offline_ai()

    def test_fire_uses_real_dense_enemy_group_without_progress_gate(self):
        ai = self.module.KingdomRushAI(bot=None, dry_run=True)
        enemies = [{"x": 100, "y": 100, "path_progress": 0.1, "hp": 200} for _ in range(5)]
        enemies += [{"x": 600, "y": 600, "path_progress": 0.9, "hp": 200} for _ in range(2)]
        target = ai._find_best_fire_target(enemies)
        self.assertEqual(target[:2], (100, 100))

    def test_reinforcement_uses_enemy_coordinate(self):
        ai = self.module.KingdomRushAI(bot=SimpleNamespace(get_path_points=lambda *args: None), dry_run=True)
        enemies = [{"x": 120, "y": 180, "path_progress": 0.1, "hp": 200, "speed": 60, "lives_cost": 1}]
        self.assertEqual(ai._find_reinforce_junction(enemies, [])[:2], (120, 180))

    def test_power_is_reported_only_after_success_acknowledgment(self):
        events = []
        bot = SimpleNamespace(use_power=Mock(return_value={"type": "error", "message": "cooldown"}))
        ai = self.module.KingdomRushAI(bot=bot, event_callback=lambda kind, data: events.append(data))
        ai.do_use_power(1, 100, 100, "offline check")
        self.assertEqual(events, [])
        bot.use_power.return_value = {"type": "ok"}
        ai.do_use_power(1, 100, 100, "offline check")
        self.assertEqual(events[-1]["event"], "power_used")
        self.assertTrue(events[-1]["text"].startswith("已释放火雨"))


if __name__ == "__main__":
    unittest.main()
