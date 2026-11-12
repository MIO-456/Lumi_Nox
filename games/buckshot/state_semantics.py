"""Causal and lifecycle guards for Buckshot Roulette bridge messages.

The Godot game exposes rapidly changing internal arrays.  A non-empty old
magazine, the empty clearing frame, and the newly loaded magazine are three
different implementation states, but only the last one is authoritative for
the next combat round.  This module keeps those transition values away from
the streamer and validates inferred shot events against real commands.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass


_TRANSITION_PHASES = frozenset({"item_grabbing", "round_loading"})

_PLAYER_SHOT_TARGETS = {
    "player_shot_dealer": "dealer",
    "player_shot_dealer_blank": "dealer",
    "player_shot_self": "self",
    "player_shot_self_blank": "self",
}

_DEALER_SHOT_EVENTS = frozenset(
    {
        "dealer_shot_player",
        "dealer_shot_player_blank",
        "dealer_shot_self",
        "dealer_shot_self_blank",
    }
)


@dataclass(frozen=True)
class BuckshotOutcomeSignal:
    """Separate commentary truth from the director's visual transition signal."""

    commentary_event: str
    final_status: str = ""
    notify_director: bool = False
    early_victory: bool = False


def classify_buckshot_outcome(
    event: str,
    *,
    batch,
    endless,
    current_status: str,
) -> BuckshotOutcomeSignal:
    """Promote only the third standard batch win to a confirmed final victory.

    Buckshot uses zero-based batch indexes, so ``2`` is the third and final
    batch in the standard game.  Unknown/malformed values fail closed and stay
    ordinary batch commentary.  The later native ``victory`` event remains the
    director signal, but does not enqueue a second victory reaction if the
    outcome was already confirmed at the final ``batch_won``.
    """

    event = str(event or "")
    current_status = str(current_status or "")
    is_final_standard_batch = (
        event == "batch_won"
        and type(batch) is int
        and batch == 2
        and endless is False
    )
    if is_final_standard_batch:
        return BuckshotOutcomeSignal(
            commentary_event="victory",
            final_status="胜利",
            notify_director=False,
            early_victory=True,
        )
    if event == "victory":
        return BuckshotOutcomeSignal(
            commentary_event="" if current_status == "胜利" else "victory",
            final_status="胜利",
            notify_director=True,
        )
    if event == "defeat":
        return BuckshotOutcomeSignal(
            commentary_event="defeat",
            final_status="失败",
            notify_director=True,
        )
    return BuckshotOutcomeSignal(commentary_event=event)


def build_buckshot_new_game_snapshot() -> dict:
    """Return a clean loading snapshot for a newly created full match.

    ``starting_game`` is earlier and more authoritative than the first
    ``round_start``.  Publishing this snapshot immediately prevents a previous
    match's terminal status from surviving through the intro of the next one.
    """

    return {
        "phase": "round_loading",
        "hp": 0,
        "hp_opp": 0,
        "hp_max": 0,
        "batch": 0,
        "endless": False,
        "live": 0,
        "blank": 0,
        "shells_ready": False,
        "raw_shells_remaining": 0,
        "player_items": [],
        "dealer_items": [],
        "game_status": "进行中",
        "last_action_result": "",
        "known_intel": "当前膛内未知",
        "player_cuffed": False,
        "dealer_cuffed": False,
        "player_wire_cut": False,
        "dealer_wire_cut": False,
    }


class BuckshotStateLifecycle:
    """Hide stale/transient shell arrays until a new magazine is loaded."""

    def __init__(self):
        self._cycle: tuple[int, int] | None = None
        self._saw_empty = False
        self._shells_ready = False

    def reset(self) -> None:
        self._cycle = None
        self._saw_empty = False
        self._shells_ready = False

    def normalize(self, state: dict) -> dict:
        normalized = deepcopy(state)
        phase = str(state.get("phase") or "unknown")
        cycle = (int(state.get("batch") or 0), int(state.get("round") or 0))
        raw_sequence = list(state.get("shells_sequence") or [])
        raw_count = len(raw_sequence)
        normalized["raw_shells_remaining"] = raw_count

        if self._cycle is None:
            self._cycle = cycle
            self._saw_empty = raw_count == 0
            self._shells_ready = bool(
                raw_count > 0 and phase not in _TRANSITION_PHASES
            )
        elif cycle != self._cycle:
            # New round/batch IDs appear before the old array is cleared.
            self._cycle = cycle
            self._saw_empty = raw_count == 0
            self._shells_ready = False

        if phase in _TRANSITION_PHASES:
            self._shells_ready = False
        if raw_count == 0:
            self._saw_empty = True
            self._shells_ready = False
        elif (
            not self._shells_ready
            and self._saw_empty
            and phase not in _TRANSITION_PHASES
        ):
            self._shells_ready = True

        normalized["shells_ready"] = self._shells_ready
        if not self._shells_ready:
            normalized["shells_sequence"] = []
            normalized["shells_remaining"] = 0
            normalized["shells_live_total"] = 0
            normalized["shells_blank_total"] = 0
            normalized["current_shell"] = "none"
        else:
            normalized["shells_sequence"] = raw_sequence
            normalized["shells_remaining"] = raw_count
            normalized["current_shell"] = raw_sequence[0]
        return normalized


class BuckshotEventCausality:
    """Reject shot labels that have no matching action/turn provenance."""

    def __init__(self):
        self._pending_player_shot_target = ""
        self._pending_duel_shot: tuple[int, int] | None = None

    def reset(self) -> None:
        self._pending_player_shot_target = ""
        self._pending_duel_shot = None

    def record_player_command(self, command: dict) -> None:
        action = str(command.get("action") or "")
        if action == "shoot":
            try:
                actor_slot = int(command.get("slot"))
                target_slot = int(command.get("target_slot"))
            except (TypeError, ValueError):
                actor_slot = target_slot = -1
            if actor_slot in (1, 3) and target_slot in (1, 3):
                self._pending_duel_shot = (actor_slot, target_slot)
                self._pending_player_shot_target = ""
                return
            target = str(command.get("target") or "")
            self._pending_player_shot_target = (
                target if target in ("dealer", "self") else ""
            )
            self._pending_duel_shot = None
        elif action == "use_item":
            # A tool animation may also change shells/HP, but it is not a shot.
            self._pending_player_shot_target = ""
            self._pending_duel_shot = None

    def validate_event(self, message: dict, *, phase: str) -> tuple[bool, str]:
        event = str(message.get("event") or "")
        if event == "shot_result":
            if self._pending_duel_shot is None:
                return False, "duel_shot_without_matching_command"
            try:
                actual = (int(message.get("from_slot")), int(message.get("to_slot")))
                damage = int(message.get("damage", 0))
            except (TypeError, ValueError):
                return False, "duel_shot_invalid_fields"
            if actual != self._pending_duel_shot:
                return False, "duel_shot_actor_or_target_mismatch"
            shell = str(message.get("shell") or "")
            if shell not in ("live", "blank") or damage < 0:
                return False, "duel_shot_invalid_result"
            if shell == "blank" and damage != 0:
                return False, "duel_blank_shot_with_damage"
            self._pending_duel_shot = None
            return True, "matched_duel_shoot_command"
        if event == "player_took_damage" and self._pending_player_shot_target == "self":
            self._pending_player_shot_target = ""
            return True, "matched_player_self_shoot_command"
        expected_target = _PLAYER_SHOT_TARGETS.get(event)
        if expected_target:
            if not self._pending_player_shot_target:
                return False, "player_shot_without_matching_shoot_command"
            if self._pending_player_shot_target != expected_target:
                return False, "player_shot_target_mismatch"
            self._pending_player_shot_target = ""
            return True, "matched_player_shoot_command"

        if event in _DEALER_SHOT_EVENTS:
            if str(phase or "") != "dealer_turn":
                return False, "dealer_shot_outside_dealer_turn"
            return True, "dealer_turn"

        return True, "not_a_shot_event"
