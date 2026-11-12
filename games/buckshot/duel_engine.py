"""Dual-player hotseat duel decision routing and intel isolation.

Each slot owns a HybridDecisionEngine instance so magnifier/phone/inverter
intel never leaks across players. Routing picks the engine of the current
action slot; unknown slots fall back to slot 1.
"""

from __future__ import annotations

from .bot import HybridDecisionEngine


class DuelState:
    """Duel view of the table: exposes the single-player engine fields the
    HybridDecisionEngine reads, always from the current actor's perspective."""

    def __init__(
        self,
        current_slot: int,
        players: dict,
        shells: dict | None = None,
        *,
        barrel_sawed_off: bool = False,
        shotgun_damage: int = 1,
    ):
        self.current_slot = int(current_slot)
        self.players = players  # {slot: {"name", "health", "items"}}
        if self.current_slot not in (1, 3):
            # Keep construction side-effect free; route_decision is the fail-closed gate.
            self_slot, opponent_slot = 1, 3
        else:
            self_slot = self.current_slot
            opponent_slot = 3 if self_slot == 1 else 1
        shells = shells or {}
        live = int(shells.get("live") or 0)
        blank = int(shells.get("blank") or 0)
        self.shells_sequence = ["live"] * live + ["blank"] * blank
        self.shells_remaining = live + blank
        self.live_remaining = live
        self.blank_remaining = blank
        self.live_probability = live / (live + blank) if live + blank else 0.0
        self.current_shell = "unknown"
        self.health_player = int(players.get(self_slot, {}).get("health") or 0)
        self.health_opponent = int(players.get(opponent_slot, {}).get("health") or 0)
        self.max_health = 4
        self.player_items = list(players.get(self_slot, {}).get("items", []))
        self.dealer_items = list(players.get(opponent_slot, {}).get("items", []))
        self.shotgun_damage = int(shotgun_damage or 1)
        self.barrel_sawed_off = bool(barrel_sawed_off)
        self.dealer_cuffed = False
        self.player_cuffed = False
        self.player_jammed = bool(players.get(self_slot, {}).get("jammed", False))
        self.opponent_jammed = bool(players.get(opponent_slot, {}).get("jammed", False))
        self.player_wire_cut = False
        self.dealer_wire_cut = False


def make_engines(engine_factory=HybridDecisionEngine) -> tuple:
    """One engine per slot; intel (known_shell/known_positions) is not shared.

    engine_factory is injectable so tests can pass a lightweight fake that
    does not construct an OpenAI client (which requires ARK_API_KEY).
    """
    return engine_factory(), engine_factory()


def route_decision(state: DuelState, engines: dict) -> dict:
    """Route to the current slot's private engine and reject unknown actors."""
    slot = state.current_slot
    if slot not in engines:
        raise ValueError(f"unknown duel slot: {slot}")
    return engines[slot].decide(state)


def duel_command(decision: dict, current_slot: int) -> dict:
    """Translate an engine decision into a slot-parameterized BridgeMod command.

    Engine decisions use single-player semantics ("shoot dealer/self",
    "use_item X"); duel needs explicit slots: shoot carries target_slot,
    use_item carries the acting slot.
    """
    action = decision.get("action")
    round_index = decision.get("round_index")
    if action == "shoot":
        target = decision.get("target", "dealer")
        if target == "self":
            target_slot = current_slot
        else:
            target_slot = 3 if current_slot == 1 else 1  # opponent of current slot
        command = {"action": "shoot", "slot": current_slot, "target_slot": target_slot}
        if round_index is not None:
            command["round_index"] = int(round_index)
        if decision.get("action_id"):
            command["action_id"] = str(decision["action_id"])
        return command
    if action == "use_item":
        command = {"action": "use_item", "item": decision.get("item", ""), "slot": current_slot}
        steal_item = str(decision.get("steal_item") or "")
        if steal_item:
            command["steal_item"] = steal_item
        if round_index is not None:
            command["round_index"] = int(round_index)
        if decision.get("action_id"):
            command["action_id"] = str(decision["action_id"])
        return command
    return dict(decision)
