"""Speech output arbitration.

This is the runtime implementation of the "表现调度" layer from the system
architecture: one effective speech output at a time, with explicit queue /
drop / interrupt policy.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


POLICY_QUEUE = "queue_after_current"
POLICY_DROP = "drop_if_busy"
POLICY_INTERRUPT = "interrupt_current"


@dataclass
class SpeechOutput:
    output_id: str
    speaker: str
    source: str
    policy: str
    started_at: float = field(default_factory=time.time)
    task_id: Optional[str] = None
    cancelled: bool = False
    reason: str = ""


class SpeechOutputArbiter:
    def __init__(self, *, event_bus=None, log_fn: Callable[[str], None] | None = None):
        self._lock = threading.RLock()
        self._counter = 0
        self._current: SpeechOutput | None = None
        self._cancelled: set[str] = set()
        self._queue: queue.Queue[dict] = queue.Queue()
        self._cancel_callback: Callable[[SpeechOutput, str], None] | None = None
        self._event_bus = event_bus
        self._log_fn = log_fn or (lambda _msg: None)

    def configure(self, *, event_bus=None, log_fn=None,
                  cancel_callback: Callable[[SpeechOutput, str], None] | None = None):
        with self._lock:
            if event_bus is not None:
                self._event_bus = event_bus
            if log_fn is not None:
                self._log_fn = log_fn
            if cancel_callback is not None:
                self._cancel_callback = cancel_callback

    def request_start(self, *, speaker: str, source: str,
                      policy: str = POLICY_QUEUE,
                      reason: str = "") -> SpeechOutput | None:
        with self._lock:
            if self._current and self.is_busy_locked():
                if policy == POLICY_DROP:
                    self._publish("speech_output_dropped", {
                        "speaker": speaker, "source": source, "policy": policy,
                        "current_output_id": self._current.output_id,
                    })
                    return None
                if policy == POLICY_QUEUE:
                    self._queue.put({
                        "speaker": speaker, "source": source,
                        "policy": policy, "reason": reason,
                    })
                    self._publish("speech_output_queued", {
                        "speaker": speaker, "source": source,
                        "current_output_id": self._current.output_id,
                        "queue_size": self._queue.qsize(),
                    })
                    return None
                if policy == POLICY_INTERRUPT:
                    self.cancel_current_locked(reason or f"interrupted_by_{source}")

            output = self._new_output_locked(speaker=speaker, source=source, policy=policy)
            self._publish("speech_output_started", self._event_data(output))
            return output

    def mark_task_id(self, output_id: str, task_id: str | None):
        if not output_id or not task_id:
            return
        with self._lock:
            if self._current and self._current.output_id == output_id:
                self._current.task_id = task_id

    def mark_done(self, output_id: str | None):
        if not output_id:
            return
        with self._lock:
            if self._current and self._current.output_id == output_id:
                done = self._current
                self._current = None
                self._publish("speech_output_done", self._event_data(done))
            self._cancelled.discard(output_id)

    def fail_current(self, reason: str = "failed", *,
                     task_id: str | None = None,
                     output_id: str | None = None) -> SpeechOutput | None:
        """Release the current output after a transport/generation failure."""
        with self._lock:
            current = self._current
            if not current:
                return None
            if output_id and current.output_id != output_id:
                return None
            if task_id and current.task_id and current.task_id != task_id:
                return None
            failed = current
            failed.reason = reason
            self._current = None
            self._cancelled.discard(failed.output_id)
            self._publish("speech_output_failed", {
                **self._event_data(failed),
                "reason": reason,
            })
            return failed

    def cancel_current(self, reason: str = "cancelled") -> SpeechOutput | None:
        with self._lock:
            return self.cancel_current_locked(reason)

    def cancel_current_locked(self, reason: str) -> SpeechOutput | None:
        if not self._current:
            return None
        old = self._current
        old.cancelled = True
        old.reason = reason
        self._cancelled.add(old.output_id)
        self._current = None
        self._publish("speech_output_cancelled", {
            **self._event_data(old),
            "reason": reason,
        })
        callback = self._cancel_callback
        if callback:
            try:
                callback(old, reason)
            except Exception as exc:
                self._log_fn(f"[speech_arbiter] cancel callback failed: {exc}")
        return old

    def is_current(self, output_id: str | None) -> bool:
        if not output_id:
            return False
        with self._lock:
            return bool(
                self._current
                and self._current.output_id == output_id
                and output_id not in self._cancelled
            )

    def is_current_task(self, task_id: str | None) -> bool:
        if not task_id:
            return True
        with self._lock:
            return bool(
                self._current
                and self._current.task_id == task_id
                and self._current.output_id not in self._cancelled
            )

    def current_task_id(self) -> str | None:
        with self._lock:
            return self._current.task_id if self._current else None

    def is_cancelled(self, output_id: str | None) -> bool:
        if not output_id:
            return False
        with self._lock:
            return output_id in self._cancelled

    def is_busy(self) -> bool:
        with self._lock:
            return self.is_busy_locked()

    def is_busy_locked(self) -> bool:
        return self._current is not None

    def current(self) -> SpeechOutput | None:
        with self._lock:
            return self._current

    def finish_current_if_idle(self, *, max_age_s: float,
                               is_transport_busy_fn: Callable[[], bool],
                               reason: str = "transport_idle") -> SpeechOutput | None:
        with self._lock:
            output = self._current
            if not output:
                return None
            age_s = time.time() - output.started_at
            if age_s < max_age_s:
                return None
        try:
            if is_transport_busy_fn():
                return None
        except Exception as exc:
            self._log_fn(f"[speech_arbiter] transport busy check failed: {exc}")
            return None
        with self._lock:
            if not self._current or self._current.output_id != output.output_id:
                return None
            self._current = None
            self._cancelled.discard(output.output_id)
            self._publish("speech_output_done", {
                **self._event_data(output),
                "reason": reason,
                "age_s": age_s,
            })
            return output

    def queued_count(self) -> int:
        return self._queue.qsize()

    def _new_output_locked(self, *, speaker: str, source: str, policy: str) -> SpeechOutput:
        self._counter += 1
        output = SpeechOutput(
            output_id=f"speech_{int(time.time() * 1000)}_{self._counter}",
            speaker=speaker or "",
            source=source or "",
            policy=policy or POLICY_QUEUE,
        )
        self._current = output
        return output

    def _event_data(self, output: SpeechOutput) -> dict:
        return {
            "output_id": output.output_id,
            "speaker": output.speaker,
            "source": output.source,
            "policy": output.policy,
            "task_id": output.task_id,
            "started_at": output.started_at,
        }

    def _publish(self, event_type: str, data: dict):
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish(event_type, data, source="speech_output_arbiter")
        except Exception as exc:
            self._log_fn(f"[speech_arbiter] publish {event_type} failed: {exc}")


arbiter = SpeechOutputArbiter()
