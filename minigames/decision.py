"""Coordinate one game decision with character speech completion."""

from __future__ import annotations

import threading
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field

from minigames.contracts import DecisionRequest


@dataclass
class LiveDecisionRequest:
    decision: DecisionRequest
    state_text: str = ""
    intel_text: str = ""
    tools: list = field(default_factory=list)
    spectator_text: str = ""
    result_event: threading.Event = field(default_factory=threading.Event)
    result: dict = field(default_factory=dict)
    cancelled: bool = False
    output_id: str | None = None

    @property
    def role_name(self) -> str:
        return self.decision.actor


class DecisionCoordinator:
    def __init__(self, bus):
        self.bus = bus
        self._lock = threading.Lock()
        self._pending: LiveDecisionRequest | None = None
        self._speech_lock = threading.Lock()
        self._completed_outputs: deque[str] = deque(maxlen=128)
        self._speech_waiters: dict[str, set[threading.Event]] = {}
        self._any_speech_waiters: set[threading.Event] = set()
        self._tts_done_callback = self._on_tts_done
        self.bus.subscribe("tts_done", self._tts_done_callback)

    def _on_tts_done(self, event) -> None:
        output_id = str((getattr(event, "data", None) or {}).get("output_id") or "")
        with self._speech_lock:
            if output_id:
                self._completed_outputs.append(output_id)
            waiters = list(self._any_speech_waiters)
            if output_id:
                waiters.extend(self._speech_waiters.get(output_id, ()))
        for waiter in waiters:
            waiter.set()

    def stage(
        self,
        decision: DecisionRequest,
        *,
        state_text: str = "",
        intel_text: str = "",
        tools: list | None = None,
        spectator_text: str = "",
    ) -> LiveDecisionRequest:
        request = LiveDecisionRequest(
            decision=decision,
            state_text=state_text,
            intel_text=intel_text,
            tools=deepcopy(tools or []),
            spectator_text=spectator_text,
        )
        with self._lock:
            previous = self._pending
            self._pending = request
        if previous is not None and not previous.result_event.is_set():
            previous.cancelled = True
            previous.result_event.set()
        return request

    def get_pending_decision(self) -> LiveDecisionRequest | None:
        with self._lock:
            return self._pending

    def complete(
        self,
        *,
        decision_id: str,
        actor: str,
        action: dict,
        output_id: str | None,
    ) -> bool:
        with self._lock:
            request = self._pending
            if (
                request is None
                or request.cancelled
                or request.decision.decision_id != decision_id
                or request.decision.actor != actor
                or action not in request.decision.legal_actions
                or request.result_event.is_set()
            ):
                return False
            request.result = deepcopy(action)
            request.output_id = output_id
            request.result_event.set()
            return True

    def clear(self, reason: str = "") -> None:
        del reason
        with self._lock:
            request = self._pending
            self._pending = None
        if request is not None and not request.result_event.is_set():
            request.cancelled = True
            request.result_event.set()

    def wait_for_speech(self, output_id: str | None, *, timeout: float) -> bool:
        done = threading.Event()
        with self._speech_lock:
            if output_id is not None and output_id in self._completed_outputs:
                return True
            if output_id is None:
                self._any_speech_waiters.add(done)
            else:
                self._speech_waiters.setdefault(output_id, set()).add(done)
        try:
            return done.wait(timeout=timeout)
        finally:
            with self._speech_lock:
                if output_id is None:
                    self._any_speech_waiters.discard(done)
                else:
                    waiters = self._speech_waiters.get(output_id)
                    if waiters is not None:
                        waiters.discard(done)
                        if not waiters:
                            self._speech_waiters.pop(output_id, None)

    def cancel_speech_waits(self) -> None:
        with self._speech_lock:
            waiters = list(self._any_speech_waiters)
            for output_waiters in self._speech_waiters.values():
                waiters.extend(output_waiters)
        for waiter in waiters:
            waiter.set()

    def close(self) -> None:
        self.bus.unsubscribe("tts_done", self._tts_done_callback)
