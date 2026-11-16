"""Shared host that joins a game adapter to speech and browser output."""

from __future__ import annotations

import random
import threading
import time
from copy import deepcopy
from typing import Callable

from minigames.decision import DecisionCoordinator, LiveDecisionRequest
from minigames.registry import registry as default_registry
from minigames.runtime import MinigameRuntime, MinigameRuntimeError


class MinigameHost:
    def __init__(
        self,
        adapter,
        *,
        bus,
        browser,
        participation_mode: str,
        active_characters: list[str],
        event_callback: Callable[[str, dict], None] | None = None,
        controller: str | None = None,
        id_factory: Callable[[], str] | None = None,
        registry=None,
        auto_run: bool = True,
        decision_timeout: float = 35.0,
        speech_timeout: float = 120.0,
        action_provider=None,
        move_interval: float = 0.0,
        move_interval_max: float | None = None,
        move_interval_random: Callable[[float, float], float] | None = None,
        repeat_rounds: bool = False,
        round_pause: float = 0.0,
        round_timeout: float = 0.0,
        round_finished_callback: Callable[[dict, int], None] | None = None,
    ):
        self.adapter = adapter
        self.bus = bus
        self.browser = browser
        self.event_callback = event_callback or (
            lambda event_type, data: self.bus.publish(
                event_type,
                data,
                source=str(self.adapter.game_id),
            )
        )
        self.registry = registry or default_registry
        self.auto_run = bool(auto_run)
        self.decision_timeout = float(decision_timeout)
        self.speech_timeout = float(speech_timeout)
        self.action_provider = action_provider
        self.move_interval = max(0.0, float(move_interval))
        self.move_interval_max = max(
            self.move_interval,
            float(move_interval_max)
            if move_interval_max is not None
            else self.move_interval,
        )
        self._move_interval_random = move_interval_random or random.uniform
        self.repeat_rounds = bool(repeat_rounds)
        self.round_pause = max(0.0, float(round_pause))
        self.round_timeout = max(0.0, float(round_timeout))
        self.round_finished_callback = round_finished_callback
        self._stop_requested = threading.Event()
        self._match_finalizing = threading.Event()
        self._worker: threading.Thread | None = None
        self._finishing_round = False
        self._state_lock = threading.RLock()
        self._latest_browser_state: dict | None = None
        self._match_clock: dict = {}
        self._match_result: dict = {}
        self._finalizing_identity: tuple[int, int] | None = None
        self._last_runtime_event_at = 0.0
        self.coordinator = DecisionCoordinator(bus)
        self.runtime = MinigameRuntime(
            adapter,
            participation_mode=participation_mode,
            active_characters=active_characters,
            controller=controller,
            event_callback=self._on_runtime_event,
            id_factory=id_factory,
        )
        self._segment_clock_callback = self._on_segment_clock
        self._activity_finalizing_callback = self._on_activity_finalizing
        if hasattr(self.bus, "subscribe"):
            self.bus.subscribe("segment_clock", self._segment_clock_callback)
            self.bus.subscribe(
                "activity_finalizing",
                self._activity_finalizing_callback,
            )

    @property
    def game_label(self) -> str:
        return str(getattr(self.adapter, "label", "") or self.runtime.game_id)

    @property
    def game_prompt(self) -> str:
        return str(getattr(self.adapter, "game_prompt", "") or "")

    @property
    def running(self) -> bool:
        return self.runtime.running

    @property
    def in_round(self) -> bool:
        return bool(
            self.runtime.running
            and (not self.runtime.finished or self._finishing_round)
        )

    @property
    def match_finalizing(self) -> bool:
        return self._match_finalizing.is_set()

    def expected_silence_evidence(self, *, max_age: float | None = None) -> dict | None:
        """Return fresh runtime facts that can justify result-only silence."""
        if not self.runtime.running:
            return None
        age = time.monotonic() - self._last_runtime_event_at
        freshness = (
            max(5.0, self.move_interval_max * 3.0 + 2.0)
            if max_age is None
            else max(0.0, float(max_age))
        )
        if self._last_runtime_event_at <= 0 or age > freshness:
            return None
        return {
            "game_id": self.runtime.game_id,
            "running": True,
            "round_no": self.runtime.round_no,
            "revision": self.runtime.revision,
            "phase": (
                "match_finalizing"
                if self.match_finalizing
                else ("round_settlement" if self._finishing_round else "playing")
            ),
            "state_fresh": True,
        }

    def _browser_state_with_match(self, data: dict) -> dict:
        payload = deepcopy(data)
        state = dict(payload.get("state") or {})
        with self._state_lock:
            if self._match_clock:
                state["match_clock"] = deepcopy(self._match_clock)
            if self._match_result:
                state["match_result"] = deepcopy(self._match_result)
        payload["state"] = state
        return payload

    def _publish_latest_browser_state(self) -> None:
        with self._state_lock:
            latest = deepcopy(self._latest_browser_state)
        if latest is not None:
            self.browser.publish_state(self._browser_state_with_match(latest))

    def _on_runtime_event(self, event_type: str, data: dict) -> None:
        self._last_runtime_event_at = time.monotonic()
        if event_type == "game_state":
            with self._state_lock:
                self._latest_browser_state = deepcopy(data)
            self._publish_latest_browser_state()
        self.event_callback(event_type, data)

    def _on_segment_clock(self, event) -> None:
        data = dict(getattr(event, "data", {}) or {})
        if data.get("game_id") != self.runtime.game_id or not self.runtime.running:
            return
        segment_index = data.get("segment_index")
        revision = data.get("clock_revision")
        if not isinstance(segment_index, int) or not isinstance(revision, int):
            return
        identity = (segment_index, revision)
        with self._state_lock:
            current_identity = (
                self._match_clock.get("segment_index"),
                self._match_clock.get("clock_revision"),
            ) if self._match_clock else None
            if current_identity is not None and identity < current_identity:
                return
            self._match_clock = {
                "segment_index": segment_index,
                "clock_revision": revision,
                "remaining_sec": data.get("remaining_sec"),
                "boundary_reached": bool(data.get("boundary_reached")),
            }
        self._last_runtime_event_at = time.monotonic()
        self._publish_latest_browser_state()

    def _on_activity_finalizing(self, event) -> None:
        data = dict(getattr(event, "data", {}) or {})
        if (
            data.get("game_id") != self.runtime.game_id
            or not self.runtime.running
            or self.runtime.participation_mode != "duel"
        ):
            return
        segment_index = data.get("segment_index")
        revision = data.get("clock_revision")
        if not isinstance(segment_index, int) or not isinstance(revision, int):
            return
        identity = (segment_index, revision)
        with self._state_lock:
            if self._finalizing_identity == identity and self._match_result:
                return
            self._finalizing_identity = identity
            self._match_finalizing.set()
        cancel_callback = getattr(self.round_finished_callback, "cancel", None)
        if callable(cancel_callback):
            cancel_callback()
        self.coordinator.clear("match_finalizing")
        self.coordinator.cancel_speech_waits()

        snapshot = self.runtime.snapshot()
        raw_scores = snapshot.get("scores")
        players = tuple(self.runtime.active_characters)
        if not isinstance(raw_scores, dict) or len(players) != 2:
            self.event_callback("game_event", {
                "game": self.runtime.game_id,
                "session_id": self.runtime.session_id,
                "revision": self.runtime.revision,
                "event_type": "host_error",
                "error": "timed duel requires scores for exactly two players",
            })
            return
        scores = {player: raw_scores.get(player) for player in players}
        if any(
            isinstance(score, bool) or not isinstance(score, int) or score < 0
            for score in scores.values()
        ):
            self.event_callback("game_event", {
                "game": self.runtime.game_id,
                "session_id": self.runtime.session_id,
                "revision": self.runtime.revision,
                "event_type": "host_error",
                "error": "timed duel scores must be non-negative integers",
            })
            return
        first, second = players
        winner = None
        if scores[first] != scores[second]:
            winner = first if scores[first] > scores[second] else second
        result = {
            "game_id": self.runtime.game_id,
            "session_id": self.runtime.session_id,
            "phase": "match_over",
            "round_no": self.runtime.round_no,
            "scores": scores,
            "winner": winner,
            "draw": winner is None,
            "aborted_round_no": (
                None if self.runtime.finished else self.runtime.round_no
            ),
            "segment_index": segment_index,
            "clock_revision": revision,
            "reason": data.get("reason") or "schedule_finalizing",
        }
        with self._state_lock:
            self._match_result = deepcopy(result)
        self._publish_latest_browser_state()
        self.event_callback("match_finalized", result)

    def start(self, *, port: int, seed=None) -> str:
        if self.runtime.running:
            raise MinigameRuntimeError("minigame is already running")
        if self._worker is not None and self._worker.is_alive():
            raise MinigameRuntimeError(
                "previous minigame turn loop is still stopping"
            )
        self._stop_requested.clear()
        self._match_finalizing.clear()
        with self._state_lock:
            self._latest_browser_state = None
            self._match_clock = {}
            self._match_result = {}
            self._finalizing_identity = None
        self._last_runtime_event_at = 0.0
        reset_callback = getattr(self.round_finished_callback, "reset", None)
        if callable(reset_callback):
            reset_callback()
        try:
            self.browser.start(port=port)
            self.registry.activate(self)
            session_id = self.runtime.start(seed=seed)
            if self.auto_run:
                self._worker = threading.Thread(
                    target=self._run_turn_loop,
                    daemon=True,
                    name=f"minigame-{self.runtime.game_id}",
                )
                self._worker.start()
            return session_id
        except Exception:
            self._stop_requested.set()
            self.coordinator.clear("start_failed")
            if self.runtime.running:
                self.runtime.stop()
            self.registry.deactivate(self)
            self.browser.stop()
            raise

    def _run_turn_loop(self) -> None:
        try:
            round_started_at = time.monotonic()
            while (
                not self._stop_requested.is_set()
                and self.runtime.running
                and not self._match_finalizing.is_set()
            ):
                while (
                    not self._stop_requested.is_set()
                    and self.runtime.running
                    and not self.runtime.finished
                    and not self._match_finalizing.is_set()
                ):
                    if (
                        self.round_timeout > 0
                        and time.monotonic() - round_started_at >= self.round_timeout
                    ):
                        self.runtime.finish_round(reason="round_timeout")
                        break
                    if self.action_provider is None:
                        self.stage_turn()
                        self.execute_pending_turn(
                            decision_timeout=self.decision_timeout,
                            speech_timeout=self.speech_timeout,
                        )
                    else:
                        decision = self.runtime.next_decision(publish=False)
                        wait_time = self._next_move_interval()
                        if self.round_timeout > 0:
                            wait_time = min(
                                wait_time,
                                max(
                                    0.0,
                                    self.round_timeout
                                    - (time.monotonic() - round_started_at),
                                ),
                            )
                        if decision is None or self._stop_requested.wait(wait_time):
                            break
                        if (
                            self.round_timeout > 0
                            and time.monotonic() - round_started_at >= self.round_timeout
                        ):
                            self.runtime.finish_round(reason="round_timeout")
                            break
                        choose = getattr(self.action_provider, "choose_action", None)
                        action = (
                            choose(decision)
                            if callable(choose)
                            else self.action_provider(decision)
                        )
                        with self._state_lock:
                            if self._match_finalizing.is_set() or self._stop_requested.is_set():
                                break
                            self.runtime.submit_decision(
                                decision_id=decision.decision_id,
                                actor=decision.actor,
                                action=action,
                            )

                if (
                    self._stop_requested.is_set()
                    or not self.runtime.running
                    or self._match_finalizing.is_set()
                ):
                    break
                if not self.runtime.finished:
                    continue

                self._finishing_round = True
                result = deepcopy(self.adapter.result())
                if self.round_finished_callback is not None:
                    self.round_finished_callback(result, self.runtime.round_no)
                if (
                    self._stop_requested.is_set()
                    or not self.runtime.running
                    or self._match_finalizing.is_set()
                ):
                    break
                self.event_callback("game_event", {
                    "game": self.runtime.game_id,
                    "session_id": self.runtime.session_id,
                    "revision": self.runtime.revision,
                    "event_type": "round_complete",
                    "round_no": self.runtime.round_no,
                    "result": result,
                })
                self._finishing_round = False
                if not self.repeat_rounds:
                    break
                if self._stop_requested.wait(self.round_pause):
                    break
                if self._match_finalizing.is_set():
                    break
                self.runtime.start_next_round()
                round_started_at = time.monotonic()
        except Exception as exc:
            if not self._stop_requested.is_set():
                self.event_callback("game_event", {
                    "game": self.runtime.game_id,
                    "session_id": self.runtime.session_id,
                    "revision": self.runtime.revision,
                    "event_type": "host_error",
                    "error": str(exc),
                })
                self.runtime.stop()
                self.browser.stop()
                self.registry.deactivate(self)
        finally:
            self._finishing_round = False

    def _next_move_interval(self) -> float:
        if self.move_interval_max <= self.move_interval:
            return self.move_interval
        sampled = float(
            self._move_interval_random(
                self.move_interval,
                self.move_interval_max,
            )
        )
        return min(self.move_interval_max, max(self.move_interval, sampled))

    def stage_turn(self) -> LiveDecisionRequest | None:
        if self._match_finalizing.is_set():
            return None
        decision = self.runtime.next_decision()
        if decision is None:
            return None
        description = self.adapter.describe_decision(decision) or {}
        return self.coordinator.stage(
            decision,
            state_text=str(description.get("state_text") or ""),
            intel_text=str(description.get("intel_text") or ""),
            spectator_text=str(description.get("spectator_text") or ""),
            tools=list(description.get("tools") or []),
        )

    def get_pending_decision(self) -> LiveDecisionRequest | None:
        return self.coordinator.get_pending_decision()

    def complete_decision(
        self,
        *,
        decision_id: str,
        actor: str,
        action: dict,
        output_id: str | None,
    ) -> bool:
        if self._match_finalizing.is_set():
            self.coordinator.clear("match_finalizing")
            return False
        return self.coordinator.complete(
            decision_id=decision_id,
            actor=actor,
            action=action,
            output_id=output_id,
        )

    def execute_pending_turn(
        self,
        *,
        decision_timeout: float,
        speech_timeout: float,
    ) -> dict | None:
        if self._match_finalizing.is_set():
            self.coordinator.clear("match_finalizing")
            return None
        request = self.coordinator.get_pending_decision()
        if request is None:
            raise MinigameRuntimeError("there is no pending minigame decision")
        if not request.result_event.wait(timeout=decision_timeout):
            try:
                return self.runtime.submit_fallback(
                    request.decision.decision_id,
                    reason="decision_timeout",
                )
            finally:
                self.coordinator.clear("decision_timeout")
        if request.cancelled:
            return None

        speech_completed = self.coordinator.wait_for_speech(
            request.output_id,
            timeout=speech_timeout,
        )
        if self._stop_requested.is_set():
            self.coordinator.clear("host_stopped")
            return None
        if not speech_completed:
            self.event_callback("game_event", {
                "game": self.runtime.game_id,
                "session_id": self.runtime.session_id,
                "revision": self.runtime.revision,
                "event_type": "speech_wait_timeout",
                "decision_id": request.decision.decision_id,
                "output_id": request.output_id,
            })
        try:
            return self.runtime.submit_decision(
                decision_id=request.decision.decision_id,
                actor=request.decision.actor,
                action=request.result,
            )
        finally:
            self.coordinator.clear("turn_executed")

    def stop(self) -> bool:
        self._stop_requested.set()
        cancel_callback = getattr(self.round_finished_callback, "cancel", None)
        if callable(cancel_callback):
            cancel_callback()
        self.coordinator.clear("host_stopped")
        self.coordinator.cancel_speech_waits()
        worker = self._worker
        if (
            worker is not None
            and worker.is_alive()
            and worker is not threading.current_thread()
        ):
            worker.join(timeout=5.0)
        if worker is not None and worker.is_alive():
            raise TimeoutError("minigame turn loop did not stop in time")
        stopped = self.runtime.stop()
        self.browser.stop()
        self.registry.deactivate(self)
        return stopped

    def close(self) -> None:
        self.stop()
        self.coordinator.close()
        coordinator = getattr(self.round_finished_callback, "coordinator", None)
        if coordinator is not None:
            coordinator.close()
        self.bus.unsubscribe("segment_clock", self._segment_clock_callback)
        self.bus.unsubscribe("activity_finalizing", self._activity_finalizing_callback)
