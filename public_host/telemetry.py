"""Bounded presentation records; never used to decide scheduling or completion."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import time


class RequestTrace:
    """Owned by Session's lock, including reads from audio callbacks."""

    def __init__(self, limit=240, *, clock=time.monotonic, wall_clock=time.time):
        self.limit = limit
        self.clock = clock
        self.wall_clock = wall_clock
        self.records = OrderedDict()
        self._started = {}

    def queued(self, request):
        now = self.wall_clock()
        self.records[request.request_id] = {
            "request_id": request.request_id, "kind": request.kind,
            "source": request.source, "speaker": request.speaker,
            "input": request.text, "text": "", "phase": "queued", "outcome": "",
            "error": "", "queued_at": now, "updated_at": now, "timings": {},
            "elapsed_ms": 0,
        }
        tick = self.clock()
        self._started[request.request_id] = (tick, tick)
        self._trim()

    def update(self, request_id, phase, *, speaker=None, text=None):
        record = self.records.get(request_id)
        if record is None or record["outcome"]:
            return
        tick = self.clock()
        start, previous = self._started[request_id]
        if phase != record["phase"]:
            timings = record["timings"]
            timings[record["phase"]] = timings.get(record["phase"], 0) + round((tick - previous) * 1000, 2)
            self._started[request_id] = (start, tick)
        record.update(phase=phase, updated_at=self.wall_clock(), elapsed_ms=round((tick - start) * 1000, 2))
        if speaker is not None:
            record["speaker"] = speaker
        if text is not None:
            record["text"] = text

    def finish(self, request_id, outcome, *, speaker=None, error=None):
        self.update(request_id, outcome, speaker=speaker)
        record = self.records.get(request_id)
        if record is not None:
            record.update(outcome=outcome, error=error or "")
            if outcome in ("cancelled", "failed"):
                record["text"] = ""
            self._started.pop(request_id, None)
        self._trim()

    def snapshot(self):
        records = deepcopy(list(self.records.values()))
        tick = self.clock()
        for record in records:
            timing = self._started.get(record["request_id"])
            if timing is not None:
                start, previous = timing
                record["elapsed_ms"] = round((tick - start) * 1000, 2)
                phase = record["phase"]
                record["timings"][phase] = record["timings"].get(phase, 0) + round((tick - previous) * 1000, 2)
        return records

    def _trim(self):
        for request_id in list(self.records):
            if len(self.records) <= self.limit:
                break
            if self.records[request_id]["outcome"]:
                del self.records[request_id]
