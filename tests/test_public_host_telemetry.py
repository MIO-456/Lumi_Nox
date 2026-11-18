"""Presentation trace limits and timings are independent of result bookkeeping."""
from types import SimpleNamespace
import unittest

from public_host.telemetry import RequestTrace
from public_host.playback import DemoPlayer
from public_host.providers import ProviderError


class RequestTraceTests(unittest.TestCase):
    def test_invalid_demo_duration_is_rejected_before_playback(self):
        for value in ("2", None, True, float("nan"), float("inf"), 0, 100):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                DemoPlayer(value)

    def test_timings_are_monotonic_and_active_records_survive_eviction(self):
        now = [0.0]
        trace = RequestTrace(limit=2, clock=lambda: now[0], wall_clock=lambda: 100 + now[0])

        def queued(request_id):
            trace.queued(SimpleNamespace(request_id=request_id, kind="chat", source="manual",
                                         speaker=None, text=request_id))

        queued("active")
        now[0] = 0.25
        trace.update("active", "generating", speaker="A")
        now[0] = 0.75
        record = trace.snapshot()[0]
        self.assertEqual(record["timings"], {"queued": 250, "generating": 500})
        for request_id in ("one", "two", "three"):
            queued(request_id)
            trace.finish(request_id, "complete")
        self.assertEqual(list(trace.records), ["active", "three"])
        trace.finish("active", "cancelled")
        trace.update("active", "playing", text="late text")
        record = trace.snapshot()[0]
        self.assertEqual((record["phase"], record["text"]), ("cancelled", ""))
        self.assertEqual(record["elapsed_ms"], 750)


if __name__ == "__main__":
    unittest.main()
