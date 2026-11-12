"""Existing drawing wire shapes; TypedDict does not perform runtime validation."""
from typing import Literal, NotRequired, TypedDict


class LoadingMessage(TypedDict):
    type: Literal["loading"]
    title: str
    title_short: str
    started_at_ms: int
    snapshot: NotRequired[bool]


class DrawMessage(TypedDict):
    type: Literal["draw"]
    draw_id: str
    title: str
    title_short: str
    canvas_width: int
    canvas_height: int
    draw_rect: list[float]  # [x0, y0, x1, y1], not [x, y, width, height]
    strokes: list[list[list[float]]]  # stroke -> interpolated pixel points -> [x, y]
    line_width: float
    stroke_palette: list[str]
    anim_interval_ms: int
    stroke_pause_ms: int
    display_duration_ms: int
    visual_timeout_ms: int
    started_at_ms: int
    snapshot: NotRequired[bool]


class ClearMessage(TypedDict):
    type: Literal["clear"]
    started_at_ms: int
    snapshot: NotRequired[bool]


class DrawComplete(TypedDict):
    type: Literal["draw_complete"]
    draw_id: str
    # Legacy board acknowledgements omit these. Strict stage mode requires both.
    consumer: NotRequired[str]
    visible: NotRequired[bool]
