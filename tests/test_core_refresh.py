"""Offline regressions for the public event and speech helpers."""
import threading
import unittest

from event_bus import EventBus
from speech_output_arbiter import POLICY_INTERRUPT, SpeechOutputArbiter
from viewer_name import fold_enter_room_inputs, normalize_viewer_name


class CoreRefreshTests(unittest.TestCase):
    def test_slow_handler_cannot_hold_request_past_timeout_or_reply_late(self):
        bus = EventBus()
        release = threading.Event()
        entered = threading.Event()
        finished = threading.Event()
        observed = []
        timeouts = []

        def handler(event):
            observed.append(event)
            entered.set()
            release.wait(2)
            observed.append(event.respond({"late": True}))
            finished.set()

        bus.subscribe("work", handler)
        bus.subscribe("request_timeout", lambda event: timeouts.append(event.data))
        result = []
        requester = threading.Thread(
            target=lambda: result.append(bus.request("work", {}, "test", timeout=0.05))
        )
        requester.start()
        try:
            self.assertTrue(entered.wait(1))
            requester.join(0.8)
            self.assertFalse(requester.is_alive(), "timeout must include handler execution")
            self.assertEqual(result, [None])
            self.assertFalse(observed[0].is_active)
            self.assertEqual(len(timeouts), 1)
        finally:
            release.set()
            requester.join(2)
        self.assertTrue(finished.wait(1))
        self.assertIs(observed[1], False)

    def test_completed_request_rejects_further_response(self):
        bus = EventBus()
        seen = []

        def handler(event):
            seen.append(event)
            event.respond({"answer": 42})

        bus.subscribe("work", handler)
        result = bus.request("work", {}, "test", timeout=1)
        self.assertEqual(result, {"answer": 42})
        self.assertFalse(seen[0].respond({"answer": 0}))
        self.assertEqual(result, {"answer": 42})

    def test_stale_cancel_does_not_interrupt_new_speech(self):
        arbiter = SpeechOutputArbiter()
        cancelled = []
        arbiter.configure(cancel_callback=lambda output, reason: cancelled.append(output.output_id))
        previous = arbiter.request_start(speaker="Lumi", source="old")
        current = arbiter.request_start(speaker="Nox", source="new", policy=POLICY_INTERRUPT)
        cancelled.clear()
        self.assertEqual(arbiter.cancel_if_current(previous.output_id)["status"], "not_current")
        self.assertTrue(arbiter.is_current(current.output_id))
        self.assertEqual(cancelled, [])
        self.assertEqual(arbiter.cancel_if_current(current.output_id)["status"], "cancelled")
        self.assertEqual(cancelled, [current.output_id])
        self.assertFalse(arbiter.is_busy())

    def test_readable_names_cover_default_unicode_and_special_names(self):
        for raw, expected in [
            ("BILI_12345", "B站用户"), ("12345_bili", "B站用户"),
            ("✨测试ＡＢ１２", "测试"), ("Ａｌｉｃｅ_123", "Alice"),
            ("Café_99", "Café"), ("１２３_✨", "123"), ("ねこ✨", "ねこ✨"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_viewer_name(raw), expected)

    def test_enter_room_names_are_unique_and_zero_limit_is_preserved(self):
        items = [{"source": "enter_room", "speaker": name} for name in ["测试_1", "测试_2", "朋友_3"]]
        folded = fold_enter_room_inputs(items, is_recognized=lambda _: True)
        self.assertIn("3 位观众", folded[0]["text"])
        self.assertEqual(folded[0]["text"].count("「测试」"), 1)
        self.assertIn("「朋友」", folded[0]["text"])
        unnamed = fold_enter_room_inputs(items, is_recognized=lambda _: True, max_named=0)
        self.assertNotIn("「", unnamed[0]["text"])


if __name__ == "__main__":
    unittest.main()
