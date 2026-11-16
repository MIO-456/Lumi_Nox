"""Shared program contracts, using explicit clocks and in-memory activities."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import unittest

from core.program import (
    FlowDecision, TransitionHalted, TransitionHooks, activation_accepted,
    build_schedule_timeline, clock_snapshot, finish_program, next_decision,
    preparation_accepted, run_transition, stop_confirmed, target_index,
    tick_decision, transition_disposition,
)


class ProgramClockTests(unittest.TestCase):
    def test_timeline_keeps_input_and_crosses_midnight(self):
        @dataclass
        class Segment:
            name: str
            duration_min: int

        first = {"name": "Chat", "duration_min": 3}
        start = datetime(2026, 11, 16, 23, 58)
        timeline, end = build_schedule_timeline([first, Segment("Draw", 2)], start)
        self.assertEqual([(item["start_time"], item["end_time"]) for item in timeline],
                         [("23:58", "00:00"), ("00:01", "00:02")])
        self.assertEqual(end, datetime(2026, 11, 17, 0, 3))
        self.assertEqual(first, {"name": "Chat", "duration_min": 3})
        self.assertEqual(build_schedule_timeline([], start), ([], start))

    def test_clock_counts_cumulative_duration_and_exact_boundary(self):
        start = datetime(2026, 11, 16, 20, 0)
        args = dict(started_at=start, current_index=1, state="RUNNING",
                    revision=4, name="Drawing", activity_id="draw")
        before = clock_snapshot([30, 90], now=start + timedelta(seconds=119.2), **args)
        self.assertEqual(before["remaining_sec"], 1)
        self.assertFalse(before["boundary_reached"])
        self.assertEqual(before["clock_revision"], 4)
        self.assertEqual(before["segment_name"], "Drawing")
        self.assertEqual(before["game_id"], "draw")
        self.assertEqual(target_index(before, 2), 1)
        boundary = clock_snapshot([30, 90], now=start + timedelta(seconds=120), **args)
        self.assertEqual(boundary["remaining_sec"], 0)
        self.assertTrue(boundary["boundary_reached"])
        self.assertIsNone(target_index(boundary, 2))
        self.assertEqual(tick_decision(boundary, 2, state="RUNNING"), FlowDecision("finish"))

    def test_unstarted_clock_has_no_target_and_backward_time_cannot_add_time(self):
        start = datetime(2026, 11, 16, 20, 0)
        for durations, started_at, index in (([], start, 0), ([30], None, 0), ([30], start, 2)):
            with self.subTest(durations=durations, started=started_at, index=index):
                snapshot = clock_snapshot(durations, started_at=started_at, current_index=index,
                                          state="IDLE", now=start)
                self.assertIsNone(snapshot["remaining_sec"])
                self.assertIsNone(target_index(snapshot, len(durations)))
        snapshot = clock_snapshot([30], started_at=start, current_index=0, state="RUNNING",
                                  now=start - timedelta(seconds=5))
        self.assertEqual(snapshot["remaining_sec"], 30)

    def test_tick_transitions_solo_but_only_updates_together_timeline(self):
        snapshot = {"segment_index": 0, "boundary_reached": True}
        self.assertEqual(tick_decision(snapshot, 3, state="RUNNING"), FlowDecision("transition", 1))
        self.assertEqual(tick_decision(snapshot, 3, state="RUNNING", mode="together"),
                         FlowDecision("update", 1))
        self.assertEqual(tick_decision(snapshot, 3, state="IDLE"), FlowDecision("idle"))
        snapshot["boundary_reached"] = False
        self.assertEqual(tick_decision(snapshot, 3, state="RUNNING"), FlowDecision("current", 0))

    def test_pending_transition_waits_for_current_unit_and_keeps_selected_target(self):
        snapshot = {"segment_index": 0, "boundary_reached": True}
        self.assertEqual(tick_decision(snapshot, 4, state="PENDING_TRANSITION", pending_target=2,
                                       in_unit=True), FlowDecision("idle"))
        self.assertEqual(tick_decision(snapshot, 4, state="PENDING_TRANSITION", pending_target=2),
                         FlowDecision("transition", 2))
        self.assertEqual(tick_decision(snapshot, 4, state="PENDING_TRANSITION"), FlowDecision("idle"))
        self.assertEqual(transition_disposition("round_end", True), "wait")
        self.assertEqual(transition_disposition("round_end", False), "transition")
        self.assertEqual(transition_disposition("segment_deadline", True), "transition")

    def test_skip_and_force_use_reserved_or_pending_target_without_rewinding(self):
        args = dict(state="RUNNING", current_index=0, segment_count=4)
        self.assertEqual(next_decision(**args), FlowDecision("transition", 1))
        self.assertEqual(next_decision(**args, reserved_target=2), FlowDecision("transition", 3))
        self.assertEqual(next_decision(**args, in_transition=True), FlowDecision("idle"))
        args["state"] = "PENDING_TRANSITION"
        self.assertEqual(next_decision(**args, pending_target=2), FlowDecision("idle"))
        self.assertEqual(next_decision(**args, force=True, pending_target=2), FlowDecision("transition", 2))
        self.assertEqual(next_decision(**args, force=True, pending_target=1, reserved_target=2),
                         FlowDecision("transition", 3))

    def test_last_card_finishes_or_requests_required_cleanup_fallback(self):
        args = dict(state="RUNNING", current_index=1, segment_count=2)
        self.assertEqual(next_decision(**args), FlowDecision("finish"))
        self.assertEqual(next_decision(**args, force=True, needs_final_fallback=True), FlowDecision("fallback"))
        self.assertEqual(next_decision(state="RUNNING", current_index=0, segment_count=2,
                                       reserved_target=1, finish_reserved=True,
                                       needs_final_fallback=True), FlowDecision("finish"))


class TransitionContractTests(unittest.TestCase):
    def hooks(self, events, **overrides):
        phases = ("before", "enter", "announce", "stop", "prepare", "commit", "activate", "complete")
        callbacks = {name: (lambda phase=name: events.append(phase)) for name in phases}
        callbacks["failed"] = lambda error: events.append(("failed", type(error).__name__))
        callbacks.update(overrides)
        return TransitionHooks(**callbacks)

    def test_native_release_precedes_scene_and_speech_then_commit_precedes_activation(self):
        events = []
        receipt = {"foregroundReleased": True, "closed": False, "gameClosed": False}
        def release():
            self.assertTrue(stop_confirmed(receipt, "released"))
            self.assertFalse(stop_confirmed(receipt, "closed"))
            events.append("native_release")
        def complete():
            events.append("complete")
            return "committed"
        result = run_transition(self.hooks(events, before=release, complete=complete))
        self.assertEqual(result, "committed")
        self.assertEqual(events, ["native_release", "enter", "announce", "stop", "prepare",
                                  "commit", "activate", "complete"])

    def test_unconfirmed_native_release_cannot_change_scene_or_announce(self):
        events = []
        def release():
            events.append("release_requested")
            if not stop_confirmed({"foregroundReleased": False}, "released"):
                raise TransitionHalted("owner_retained")
        self.assertEqual(run_transition(self.hooks(events, before=release)), "owner_retained")
        self.assertEqual(events, ["release_requested"])

    def test_target_invalidated_during_prepare_cannot_commit_or_activate(self):
        events, valid = [], [True]
        def prepare():
            events.append("prepare")
            valid[0] = False
        result = run_transition(self.hooks(events, current=lambda: valid[0], prepare=prepare))
        self.assertIs(result, False)
        self.assertEqual(events, ["before", "enter", "announce", "stop", "prepare"])
        events.clear()
        self.assertIs(run_transition(self.hooks(events, current=lambda: False)), False)
        self.assertEqual(events, [])

    def test_halted_prepare_returns_receipt_without_recovery_or_later_phases(self):
        events = []
        def prepare():
            events.append("prepare")
            raise TransitionHalted("waiting_for_ready")
        self.assertEqual(run_transition(self.hooks(events, prepare=prepare)), "waiting_for_ready")
        self.assertEqual(events, ["before", "enter", "announce", "stop", "prepare"])

    def test_halt_bypasses_nested_activity_error_handler(self):
        events = []
        def prepare():
            try:
                events.append("retain_owner")
                raise TransitionHalted(False)
            except Exception:
                events.append("incorrect_second_recovery")
        self.assertIs(run_transition(self.hooks(events, prepare=prepare)), False)
        self.assertEqual(events, ["before", "enter", "announce", "stop", "retain_owner"])

    def test_failed_activation_runs_recovery_without_reporting_completion(self):
        events = []
        error = RuntimeError("fixture activation failed")
        def activate():
            events.append("activate")
            raise error
        def recover(received):
            self.assertIs(received, error)
            events.append("recover")
            return False
        self.assertIs(run_transition(self.hooks(events, activate=activate, failed=recover)), False)
        self.assertEqual(events, ["before", "enter", "announce", "stop", "prepare", "commit",
                                  "activate", "recover"])

    def test_strict_preparation_and_activation_receipts_are_distinct(self):
        for status in ("ready", "cold_start_required"):
            with self.subTest(accepted_prepare=status):
                self.assertTrue(preparation_accepted({"status": status}))
                self.assertFalse(activation_accepted({"status": status}))
        self.assertTrue(activation_accepted({"status": "active"}))
        for receipt in (None, True, {}, {"status": "active"}, {"status": "failed"},
                        {"status": "timeout"}, {"status": "stopped"}):
            with self.subTest(rejected_prepare=receipt):
                self.assertFalse(preparation_accepted(receipt))
        for receipt in (None, True, {}, {"status": "failed"}, {"status": "stopped"}):
            with self.subTest(rejected_activation=receipt):
                self.assertFalse(activation_accepted(receipt))

    def test_native_release_does_not_claim_complete_close(self):
        self.assertTrue(stop_confirmed({"status": "stopped"}))
        self.assertFalse(stop_confirmed({"status": "ready"}))
        closed = {"closed": True, "gameClosed": True}
        self.assertTrue(stop_confirmed(closed, "closed"))
        self.assertTrue(stop_confirmed(closed, "released"))
        self.assertTrue(stop_confirmed({"foregroundReleased": True}, "released"))
        for receipt in (None, True, {"status": "stopped"}, {"foregroundReleased": True},
                        {"closed": True}, {"gameClosed": True}, {"closed": 1, "gameClosed": True}):
            with self.subTest(rejected_close=receipt):
                self.assertFalse(stop_confirmed(receipt, "closed"))
        self.assertFalse(stop_confirmed({"foregroundReleased": 1}, "released"))

    def test_finish_closes_then_drains_before_announcing_completion(self):
        events = []
        self.assertTrue(finish_program(lambda: events.append("closed"),
                                       lambda: events.append("drained"),
                                       lambda: events.append("complete")))
        self.assertEqual(events, ["closed", "drained", "complete"])

    def test_unconfirmed_or_exceptional_cleanup_never_announces_completion(self):
        for phase in ("close", "drain"):
            for failure in (False, TransitionHalted(False), RuntimeError("fixture cleanup failed")):
                with self.subTest(phase=phase, failure=type(failure).__name__):
                    events = []
                    def close():
                        events.append("close")
                        if phase == "close":
                            if isinstance(failure, BaseException):
                                raise failure
                            return failure
                    def drain():
                        events.append("drain")
                        if phase == "drain":
                            if isinstance(failure, BaseException):
                                raise failure
                            return failure
                    complete = lambda: events.append("complete")
                    if isinstance(failure, RuntimeError):
                        with self.assertRaises(RuntimeError):
                            finish_program(close, drain, complete)
                    else:
                        self.assertIs(finish_program(close, drain, complete), False)
                    self.assertEqual(events, ["close"] if phase == "close" else ["close", "drain"])


if __name__ == "__main__":
    unittest.main()
