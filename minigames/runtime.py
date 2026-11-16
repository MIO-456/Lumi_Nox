"""Turn-based runtime shared by lightweight livestream games."""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Callable

from minigames.contracts import DecisionRequest
from minigames.roles import resolve_turn_roles


class MinigameRuntimeError(RuntimeError):
    pass


class InvalidActionError(MinigameRuntimeError):
    pass


class StaleDecisionError(MinigameRuntimeError):
    pass


class MinigameRuntime:
    def __init__(
        self,
        adapter,
        *,
        participation_mode: str,
        active_characters: list[str],
        event_callback: Callable[[str, dict], None] | None = None,
        controller: str | None = None,
        id_factory: Callable[[], str] | None = None,
    ):
        self.adapter = adapter
        self.participation_mode = participation_mode
        self.active_characters = list(active_characters)
        self.controller = controller
        self.event_callback = event_callback or (lambda _event_type, _data: None)
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.session_id = ""
        self.round_no = 0
        self.revision = 0
        self.turn_index = 0
        self.running = False
        self.finished = False
        self._pending: DecisionRequest | None = None

    @property
    def game_id(self) -> str:
        return str(self.adapter.game_id)

    def _publish(self, event_type: str, data: dict) -> None:
        payload = {
            "game": self.game_id,
            "session_id": self.session_id,
            "revision": self.revision,
            **deepcopy(data),
        }
        self.event_callback(event_type, payload)

    def start(self, seed=None) -> str:
        reset_match = getattr(self.adapter, "reset_match", None)
        if callable(reset_match):
            reset_match(seed=seed)
        else:
            self.adapter.reset(seed=seed)
        self.session_id = self.id_factory()
        self.round_no = 1
        self.revision = 1
        self.turn_index = 0
        self.running = True
        self.finished = False
        self._pending = None
        self._publish("game_event", {"event_type": "round_start", "round_no": self.round_no})
        self._publish_state()
        return self.session_id

    def start_next_round(self, seed=None) -> None:
        if not self.running:
            raise MinigameRuntimeError("minigame is not running")
        if not self.finished:
            raise MinigameRuntimeError("current round is not finished")
        self.adapter.reset(seed=seed)
        self.round_no += 1
        self.revision += 1
        self.turn_index = 0
        self.finished = False
        self._pending = None
        self._publish("game_event", {
            "event_type": "round_start",
            "round_no": self.round_no,
        })
        self._publish_state()

    def snapshot(self, viewer: str | None = None) -> dict:
        return deepcopy(self.adapter.snapshot(viewer=viewer))

    def _publish_state(self) -> None:
        roles = self._resolve_roles()
        self._publish("game_state", {
            "state": self.snapshot(),
            "running": self.running,
            "round_no": self.round_no,
            "turn_index": self.turn_index,
            "participation_mode": self.participation_mode,
            "actor": roles.actor,
            "spectator": roles.spectator,
        })

    def _resolve_roles(self):
        return resolve_turn_roles(
            mode=self.participation_mode,
            active_characters=self.active_characters,
            controller=self.controller,
            turn_index=self.turn_index,
            player_slot=self.adapter.current_player_slot(),
        )

    def next_decision(self, *, publish: bool = True) -> DecisionRequest | None:
        if not self.running:
            raise MinigameRuntimeError("minigame is not running")
        if self.finished:
            return None
        if self._pending is not None:
            return self._pending

        roles = self._resolve_roles()
        legal_actions = tuple(deepcopy(self.adapter.legal_actions(roles.actor)))
        self._pending = DecisionRequest(
            decision_id=self.id_factory(),
            session_id=self.session_id,
            revision=self.revision,
            actor=roles.actor,
            spectator=roles.spectator,
            state=self.snapshot(viewer=roles.actor),
            legal_actions=legal_actions,
        )
        if publish:
            self._publish("need_decision", {
                "decision_id": self._pending.decision_id,
                "actor": roles.actor,
                "spectator": roles.spectator,
                "state": self._pending.state,
                "legal_actions": list(legal_actions),
            })
        return self._pending

    def _require_pending(self, decision_id: str) -> DecisionRequest:
        if self._pending is None or self._pending.decision_id != decision_id:
            raise StaleDecisionError(f"decision is no longer current: {decision_id}")
        return self._pending

    def submit_decision(self, *, decision_id: str, actor: str, action: dict) -> dict:
        return self._apply_pending(
            decision_id=decision_id,
            actor=actor,
            action=action,
            fallback_reason=None,
        )

    def submit_fallback(self, decision_id: str, *, reason: str) -> dict:
        pending = self._require_pending(decision_id)
        if not pending.legal_actions:
            raise InvalidActionError("no legal action is available for fallback")
        return self._apply_pending(
            decision_id=decision_id,
            actor=pending.actor,
            action=deepcopy(pending.legal_actions[0]),
            fallback_reason=reason,
        )

    def finish_round(self, *, reason: str) -> dict:
        """Finish a live round through an adapter-defined external boundary."""
        if not self.running:
            raise MinigameRuntimeError("minigame is not running")
        if self.finished:
            return self.snapshot()
        finish = getattr(self.adapter, "finish_round", None)
        if not callable(finish):
            raise MinigameRuntimeError(
                f"{self.game_id} does not support external round finish"
            )
        finish(reason=str(reason or "external_finish"))
        if not self.adapter.is_terminal():
            raise MinigameRuntimeError("adapter did not become terminal")
        self._pending = None
        self.revision += 1
        self.finished = True
        self._publish("game_event", {
            "event_type": "game_finished",
            "finish_reason": str(reason or "external_finish"),
            "normalized_result": {
                "session_id": self.session_id,
                "result_id": f"{self.session_id}:round:{self.round_no}",
                "result_version": f"{self.session_id}:revision:{self.revision}",
                "round_no": self.round_no,
                "result": deepcopy(self.adapter.result()),
            },
        })
        self._publish_state()
        return self.snapshot()

    def _apply_pending(
        self,
        *,
        decision_id: str,
        actor: str,
        action: dict,
        fallback_reason: str | None,
    ) -> dict:
        pending = self._require_pending(decision_id)
        if actor != pending.actor:
            raise InvalidActionError(f"wrong actor: {actor}")
        if action not in pending.legal_actions:
            raise InvalidActionError(f"illegal action: {action}")

        game_event = self.adapter.apply_action(actor, deepcopy(action)) or {}
        self._pending = None
        self.turn_index += 1
        self.revision += 1
        self._publish("decision_executed", {
            "decision_id": decision_id,
            "actor": actor,
            "action": action,
            "fallback_reason": fallback_reason,
        })
        if game_event:
            self._publish("game_event", game_event)
        if self.adapter.is_terminal():
            self.finished = True
            self._publish("game_event", {
                "event_type": "game_finished",
                "normalized_result": {
                    "session_id": self.session_id,
                    "result_id": f"{self.session_id}:round:{self.round_no}",
                    "result_version": f"{self.session_id}:revision:{self.revision}",
                    "round_no": self.round_no,
                    "result": deepcopy(self.adapter.result()),
                },
            })
        self._publish_state()
        return self.snapshot()

    def stop(self) -> bool:
        if not self.running:
            return False
        self.running = False
        self._pending = None
        self._publish("game_event", {"event_type": "activity_stopped"})
        self._publish_state()
        return True
