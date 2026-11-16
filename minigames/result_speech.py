"""Wait for one directed result line to finish playing before continuing."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class _PendingSpeech:
    text: str
    speaker: str
    instruction_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    dispatched: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    task_id: str | None = None
    succeeded: bool = False


class ResultSpeechCoordinator:
    """Correlate a director instruction with its exact logical TTS task."""

    def __init__(self, bus):
        self.bus = bus
        self._lock = threading.Lock()
        self._pending: list[_PendingSpeech] = []
        self._by_task: dict[str, _PendingSpeech] = {}
        self._recent_outcomes: OrderedDict[str, bool] = OrderedDict()
        # Keep stable bound-method objects because EventBus.unsubscribe compares
        # callbacks by identity.
        self._dispatched_callback = self._on_dispatched
        self._tts_done_callback = self._on_tts_done
        self._terminal_callback = self._on_terminal
        self._simulated_callback = self._on_simulated
        self.bus.subscribe(
            "director_instruction_dispatched",
            self._dispatched_callback,
        )
        self.bus.subscribe("tts_done", self._tts_done_callback)
        self.bus.subscribe("speech_task_terminal", self._terminal_callback)
        self.bus.subscribe("tts_simulated", self._simulated_callback)

    def _on_dispatched(self, event) -> None:
        data = dict(getattr(event, "data", {}) or {})
        text = str(data.get("original_text") or "")
        speaker = str(data.get("speaker") or "")
        task_id = str(data.get("task_id") or "")
        instruction_id = str(data.get("instruction_id") or "")
        if not task_id:
            return
        with self._lock:
            pending = next(
                (
                    item
                    for item in self._pending
                    if item.task_id is None
                    and item.text == text
                    and item.speaker == speaker
                    and (not instruction_id or item.instruction_id == instruction_id)
                ),
                None,
            )
            if pending is None:
                return
            pending.task_id = task_id
            self._by_task[task_id] = pending
            if task_id in self._recent_outcomes:
                pending.succeeded = self._recent_outcomes[task_id]
                pending.finished.set()
            pending.dispatched.set()

    def _on_tts_done(self, event) -> None:
        task_id = str((getattr(event, "data", {}) or {}).get("task_id") or "")
        self._record_terminal(task_id, True)

    def _on_terminal(self, event) -> None:
        data = dict(getattr(event, "data", {}) or {})
        if data.get("status") in {"failed", "cancelled", "error", "interrupted"}:
            self._record_terminal(str(data.get("task_id") or ""), False)

    def _on_simulated(self, event) -> None:
        # The explicit offline host has no audio. Its own event can finish a
        # demo turn, without publishing or claiming a real playback completion.
        task_id = str((getattr(event, "data", {}) or {}).get("task_id") or "")
        self._record_terminal(task_id, True)

    def _record_terminal(self, task_id: str, succeeded: bool) -> None:
        if not task_id:
            return
        with self._lock:
            # One immutable terminal outcome per logical task; a late success
            # cannot turn an already failed/cancelled task into a played line.
            if task_id in self._recent_outcomes:
                return
            self._recent_outcomes[task_id] = succeeded
            if len(self._recent_outcomes) > 100:
                self._recent_outcomes.popitem(last=False)
            pending = self._by_task.get(task_id)
            if pending is not None:
                pending.succeeded = succeeded
                pending.finished.set()

    @staticmethod
    def _wait(
        signal: threading.Event,
        timeout: float,
        stop_event: threading.Event | None,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if signal.is_set():
                return True
            if stop_event is not None and stop_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return signal.is_set()
            signal.wait(timeout=min(0.05, remaining))

    def speak(
        self,
        text: str,
        *,
        speaker: str,
        context: dict | None = None,
        dispatch_timeout: float = 2.0,
        speech_timeout: float = 20.0,
        stop_event: threading.Event | None = None,
    ) -> bool:
        pending = _PendingSpeech(text=str(text), speaker=str(speaker))
        with self._lock:
            self._pending.append(pending)
        try:
            self.bus.publish(
                "director_instruction",
                {
                    "text": pending.text,
                    "speaker": pending.speaker,
                    "context": dict(context or {}),
                    "instruction_id": pending.instruction_id,
                },
                source="minigame",
            )
            if not self._wait(pending.dispatched, dispatch_timeout, stop_event):
                return False
            return (
                self._wait(pending.finished, speech_timeout, stop_event)
                and pending.succeeded
                and not (stop_event is not None and stop_event.is_set())
            )
        finally:
            with self._lock:
                if pending in self._pending:
                    self._pending.remove(pending)
                if pending.task_id:
                    self._by_task.pop(pending.task_id, None)

    def close(self) -> None:
        self.bus.unsubscribe(
            "director_instruction_dispatched",
            self._dispatched_callback,
        )
        self.bus.unsubscribe("tts_done", self._tts_done_callback)
        self.bus.unsubscribe("speech_task_terminal", self._terminal_callback)
        self.bus.unsubscribe("tts_simulated", self._simulated_callback)
