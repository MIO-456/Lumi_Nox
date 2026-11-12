"""Rule/view boundary extracted from the lightweight game host.

This defines the adapter shape, not a runtime, event transport or game engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DecisionRequest:
    decision_id: str
    session_id: str
    revision: int
    actor: str
    spectator: str | None
    state: dict[str, Any]
    legal_actions: tuple[dict[str, Any], ...]


class MinigameAdapter(Protocol):
    """The small rule/view boundary every lightweight game supplies."""

    game_id: str
    label: str
    game_prompt: str

    def reset(self, seed=None) -> None: ...
    def snapshot(self, viewer: str | None = None) -> dict[str, Any]: ...
    def legal_actions(self, actor: str) -> list[dict[str, Any]]: ...
    def current_player_slot(self) -> int: ...
    def apply_action(self, actor: str, action: dict[str, Any]) -> dict[str, Any]: ...
    def is_terminal(self) -> bool: ...
    def result(self) -> dict[str, Any]: ...
    def describe_decision(self, decision: DecisionRequest) -> dict[str, Any]: ...
