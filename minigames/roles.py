"""Resolve who acts and who comments for a lightweight game turn."""

from __future__ import annotations

from dataclasses import dataclass


PARTICIPATION_MODES = frozenset({
    "solo",
    "controller_spectator",
    "alternating_coop",
    "duel",
})


@dataclass(frozen=True)
class TurnRoles:
    actor: str
    spectator: str | None


def resolve_turn_roles(
    *,
    mode: str,
    active_characters: list[str] | tuple[str, ...],
    turn_index: int,
    controller: str | None = None,
    player_slot: int | None = None,
) -> TurnRoles:
    characters = tuple(name for name in active_characters if name)
    if not characters:
        raise ValueError("active_characters cannot be empty")
    if mode not in PARTICIPATION_MODES:
        raise ValueError(f"unknown participation mode: {mode}")

    if len(characters) == 1 or mode == "solo":
        return TurnRoles(actor=characters[0], spectator=None)

    if mode == "controller_spectator":
        actor = controller or characters[0]
        if actor not in characters:
            raise ValueError(f"controller is not active: {actor}")
    elif mode == "alternating_coop":
        actor = characters[turn_index % len(characters)]
    else:
        slot = turn_index if player_slot is None else player_slot
        actor = characters[slot % len(characters)]

    spectator = next((name for name in characters if name != actor), None)
    return TurnRoles(actor=actor, spectator=spectator)
