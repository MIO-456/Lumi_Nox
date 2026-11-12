"""Offline regressions for the public Buckshot protocol and decision engines."""

import json
import unittest
from unittest.mock import Mock, patch

from games.buckshot import bot as bot_module
from games.buckshot import bridge as bridge_module
from games.buckshot.bot import HybridDecisionEngine
from games.buckshot.bridge import BuckshotBridge, DeterministicEngine, GameState, build_game_tools
from games.buckshot.duel_engine import DuelState, duel_command, make_engines, route_decision
from games.buckshot.duel_protocol import format_duel_game_event
from games.buckshot.state_semantics import (
    BuckshotEventCausality,
    BuckshotStateLifecycle,
    classify_buckshot_outcome,
)


class BuckshotKnowledgeTests(unittest.TestCase):
    def make_engine(self, kind):
        # The legacy hybrid engine normally creates a cloud client in __init__.
        engine = object.__new__(kind)
        engine.known_shell = None
        engine.known_positions = {}
        return engine

    def test_inverter_does_not_reveal_an_unknown_shell(self):
        for kind in (HybridDecisionEngine, DeterministicEngine):
            with self.subTest(engine=kind.__name__):
                engine = self.make_engine(kind)
                engine.on_action_executed({"item": "inverter", "revealed_shell": "live"})
                self.assertIsNone(engine.known_shell)
                self.assertEqual(engine.known_positions, {})

    def test_inverter_flips_only_prior_current_shell_knowledge(self):
        for kind in (HybridDecisionEngine, DeterministicEngine):
            with self.subTest(engine=kind.__name__):
                engine = self.make_engine(kind)
                engine.known_shell = "blank"
                engine.known_positions = {0: "blank", 2: "live"}
                engine.on_action_executed({"item": "inverter", "revealed_shell": "blank"})
                self.assertEqual(engine.known_shell, "live")
                self.assertEqual(engine.known_positions, {0: "live", 2: "live"})

    def test_two_players_keep_separate_private_intel(self):
        first, second = make_engines(engine_factory=DeterministicEngine)
        first.on_action_executed({"item": "magnifying glass", "revealed_shell": "live"})
        self.assertIsNot(first, second)
        self.assertEqual(first.known_shell, "live")
        self.assertIsNone(second.known_shell)
        self.assertEqual(second.known_positions, {})

    def test_public_item_commentary_omits_private_shell(self):
        provider = lambda slot: {1: "Player A", 3: "Player B"}.get(slot, "")
        text = format_duel_game_event(
            {"event": "item_used", "slot": 1, "item": "magnifying glass", "revealed_shell": "live"},
            provider,
        )
        self.assertIn("Player A", text)
        self.assertNotIn("实弹", text)
        self.assertEqual(format_duel_game_event({"event": "turn_changed", "current_slot": 5}, provider), "")


class BuckshotProtocolTests(unittest.TestCase):
    def test_default_bridge_connects_in_solo_mode(self):
        sock = Mock()
        bridge = BuckshotBridge()
        with patch.object(bridge_module.socket, "socket", return_value=sock):
            self.assertTrue(bridge.connect())
        commands = [json.loads(call.args[0]) for call in sock.sendall.call_args_list]
        self.assertFalse(bridge.duel_mode)
        self.assertEqual(commands[0], {"action": "set_auto_mode", "enabled": True})
        self.assertNotIn("start_duel", [command["action"] for command in commands])

    def test_duel_is_explicit_and_sends_its_own_start_command(self):
        sock = Mock()
        bridge = BuckshotBridge(duel_mode=True)
        with patch.object(bridge_module.socket, "socket", return_value=sock):
            self.assertTrue(bridge.connect())
        commands = [json.loads(call.args[0]) for call in sock.sendall.call_args_list]
        self.assertEqual(commands, [{"action": "start_duel"}])

    def test_standalone_default_remains_solo_with_cloud_decision_option(self):
        fake_bot = Mock(sock=None, godot_process=None, wins=0, losses=0)
        with patch.object(bot_module, "BuckshotBot", return_value=fake_bot) as constructor, \
                patch.object(bot_module.sys, "argv", ["games.buckshot.bot"]), patch("builtins.input", return_value=""):
            bot_module.main()
        constructor.assert_called_once_with(watch_only=False, auto_mode=True, hybrid=True, duel_mode=False)
        fake_bot.run.assert_called_once()

    def test_duel_completion_requires_matching_game_ack(self):
        events = []
        bridge = BuckshotBridge(duel_mode=True, event_callback=lambda kind, data: events.append((kind, data)))
        bridge._current_duel_slot = 1
        decision = {"action": "shoot", "target": "dealer", "action_id": "action-1", "round_index": 1}
        with patch.object(bridge, "send_command"):
            bridge._dispatch_action(decision)
        self.assertEqual([kind for kind, _ in events], ["decision_dispatched"])
        bridge._handle_action_executed({"action": "shoot", "action_id": "unrelated-action"})
        self.assertIsNotNone(bridge._pending_execution)
        self.assertEqual(len(events), 1)
        bridge._handle_action_executed({"action": "shoot", "action_id": "action-1"})
        self.assertIsNone(bridge._pending_execution)
        self.assertEqual([kind for kind, _ in events], ["decision_dispatched", "decision_executed"])

    def test_unknown_actor_cannot_route_to_another_player(self):
        engines = {1: Mock(), 3: Mock()}
        with self.assertRaisesRegex(ValueError, "unknown duel slot"):
            route_decision(DuelState(5, {}), engines)
        for engine in engines.values():
            engine.decide.assert_not_called()

    def test_actor_perspective_and_routing_agree(self):
        state = DuelState(3, {1: {"health": 4}, 3: {"health": 2}}, {"live": 1, "blank": 2})
        engines = {1: Mock(), 3: Mock()}
        route_decision(state, engines)
        self.assertEqual((state.health_player, state.health_opponent), (2, 4))
        engines[3].decide.assert_called_once_with(state)
        engines[1].decide.assert_not_called()

    def test_command_preserves_execution_identity_and_adrenaline_target(self):
        self.assertEqual(
            duel_command({"action": "use_item", "item": "adrenaline", "steal_item": "handsaw",
                          "round_index": 2, "action_id": "test-action"}, 3),
            {"action": "use_item", "item": "adrenaline", "steal_item": "handsaw",
             "round_index": 2, "action_id": "test-action", "slot": 3},
        )
        self.assertEqual(duel_command({"action": "shoot", "target": "dealer"}, 3),
                         {"action": "shoot", "slot": 3, "target_slot": 1})

    def test_duel_tools_hide_single_player_and_useless_items(self):
        state = GameState(health_player=4, max_health=4, player_items=["adrenaline", "handcuffs"],
                          dealer_items=["cigarettes", "jammer", "magnifying glass", "handcuffs"],
                          opponent_jammed=True)
        tools = build_game_tools(state.player_items, state, opponent_name="Player B", duel_mode=True)
        actions = tools[0]["function"]["parameters"]["properties"]["动作"]["enum"]
        self.assertNotIn("使用手铐", actions)
        adrenaline = next(tool for tool in tools if tool["function"]["name"] == "use_buckshot_adrenaline")
        self.assertEqual(adrenaline["function"]["parameters"]["properties"]["目标道具"]["enum"], ["放大镜"])


class BuckshotLifecycleTests(unittest.TestCase):
    def test_old_magazine_is_hidden_until_empty_then_reloaded(self):
        guard = BuckshotStateLifecycle()

        def state(round_no, shells, phase="player_turn"):
            return {"batch": 0, "round": round_no, "phase": phase, "shells_sequence": shells}

        self.assertTrue(guard.normalize(state(0, ["live", "blank"]))["shells_ready"])
        stale = state(1, ["live", "blank"])
        self.assertEqual(guard.normalize(stale)["shells_sequence"], [])
        self.assertEqual(stale["shells_sequence"], ["live", "blank"])
        self.assertFalse(guard.normalize(state(1, []))["shells_ready"])
        fresh = guard.normalize(state(1, ["blank", "live", "blank"]))
        self.assertTrue(fresh["shells_ready"])
        self.assertEqual(fresh["shells_remaining"], 3)

    def test_shot_result_requires_matching_command_and_cannot_repeat(self):
        guard = BuckshotEventCausality()
        result = {"event": "shot_result", "from_slot": 1, "to_slot": 3, "shell": "blank", "damage": 0}
        self.assertFalse(guard.validate_event(result, phase="player_turn")[0])
        guard.record_player_command({"action": "shoot", "slot": 1, "target_slot": 3})
        self.assertFalse(guard.validate_event({**result, "damage": 1}, phase="player_turn")[0])
        self.assertTrue(guard.validate_event(result, phase="player_turn")[0])
        self.assertFalse(guard.validate_event(result, phase="player_turn")[0])

    def test_item_animation_is_not_a_shot(self):
        guard = BuckshotEventCausality()
        guard.record_player_command({"action": "shoot", "target": "dealer"})
        guard.record_player_command({"action": "use_item", "item": "beer"})
        self.assertFalse(guard.validate_event({"event": "player_shot_dealer_blank"}, phase="waiting")[0])
        self.assertFalse(guard.validate_event({"event": "dealer_shot_player"}, phase="waiting")[0])

    def test_only_final_standard_batch_is_early_victory(self):
        for batch, endless in ((0, False), (1, False), (2, True), (2, None), ("2", False)):
            with self.subTest(batch=batch, endless=endless):
                signal = classify_buckshot_outcome("batch_won", batch=batch, endless=endless, current_status="进行中")
                self.assertEqual(signal.final_status, "")
        signal = classify_buckshot_outcome("batch_won", batch=2, endless=False, current_status="进行中")
        self.assertTrue(signal.early_victory)
        self.assertFalse(signal.notify_director)
        late = classify_buckshot_outcome("victory", batch=2, endless=False, current_status=signal.final_status)
        self.assertEqual(late.commentary_event, "")
        self.assertTrue(late.notify_director)

    def test_new_match_clears_terminal_result_and_private_intel(self):
        events = []
        bridge = BuckshotBridge(event_callback=lambda kind, data: events.append((kind, data)))
        bridge._game_status = "胜利"
        bridge._last_action_result = "previous result"
        bridge.engine.known_shell = "live"
        bridge.engine.known_positions = {1: "blank"}
        bridge._reset_for_new_game({"event": "starting_game"})
        self.assertEqual(bridge._game_status, "进行中")
        self.assertEqual(bridge._last_action_result, "")
        self.assertIsNone(bridge.engine.known_shell)
        self.assertEqual(bridge.engine.known_positions, {})
        self.assertEqual(events[-1][1]["event"], "starting_game")


if __name__ == "__main__":
    unittest.main()
