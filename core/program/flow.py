"""Program decisions shared by the production director and the public host.

All times are supplied by the caller. Device/game-specific pauses may supply a
different snapshot, but may not create another next-segment calculation.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import math


@dataclass(frozen=True)
class FlowDecision:
    kind: str
    target: int | None = None


def build_schedule_timeline(schedule, actual_start_time):
    timeline, cursor = [], actual_start_time
    for segment in schedule or []:
        payload = dict(segment) if isinstance(segment, dict) else asdict(segment)
        boundary = cursor + timedelta(minutes=payload.get("duration_min", 0))
        payload.update(start_time=cursor.strftime("%H:%M"),
                       end_time=(boundary - timedelta(minutes=1)).strftime("%H:%M"))
        timeline.append(payload)
        cursor = boundary
    return timeline, cursor


def clock_snapshot(durations_seconds, *, started_at, current_index, state,
                   revision=0, now=None, name="", activity_id="", runtime_fallback=""):
    snapshot = {"clock_revision": revision, "segment_index": -1, "segment_name": "",
                "game_id": "", "remaining_sec": None, "boundary_reached": False,
                "director_state": state}
    if not durations_seconds or started_at is None or not 0 <= current_index < len(durations_seconds):
        return snapshot
    elapsed = max(0.0, ((now or datetime.now()) - started_at).total_seconds())
    remaining = sum(durations_seconds[:current_index + 1]) - elapsed
    snapshot.update(segment_index=current_index, segment_name=name, game_id=activity_id,
                    runtime_fallback=runtime_fallback,
                    remaining_sec=max(0, int(math.ceil(remaining))), boundary_reached=remaining <= 0)
    return snapshot


def target_index(snapshot, segment_count):
    index = int(snapshot.get("segment_index", -1))
    if index < 0:
        return None
    if not snapshot.get("boundary_reached"):
        return index
    following = index + 1
    return following if following < segment_count else None


def tick_decision(snapshot, segment_count, *, state, mode="solo", pending_target=None, in_unit=False):
    if state not in {"RUNNING", "PENDING_TRANSITION"}:
        return FlowDecision("idle")
    if mode != "together" and state == "PENDING_TRANSITION":
        return (FlowDecision("idle") if in_unit or pending_target is None
                else FlowDecision("transition", pending_target))
    target = target_index(snapshot, segment_count)
    if target is None:
        return FlowDecision("finish")
    if target == snapshot.get("segment_index"):
        return FlowDecision("current", target)
    return FlowDecision("update" if mode == "together" else "transition", target)


def next_decision(*, state, current_index, segment_count, force=False,
                  pending_target=None, reserved_target=None, in_transition=False,
                  needs_final_fallback=False, finish_reserved=False):
    if finish_reserved and reserved_target is not None and reserved_target + 1 >= segment_count:
        return FlowDecision("finish")
    if in_transition or state not in ({"RUNNING", "PENDING_TRANSITION"} if force else {"RUNNING"}):
        return FlowDecision("idle")
    target = (reserved_target + 1 if reserved_target is not None else
              pending_target if force and pending_target is not None else current_index + 1)
    if target >= segment_count:
        return FlowDecision("fallback" if needs_final_fallback else "finish")
    return FlowDecision("transition", target)


def transition_disposition(end_policy, in_unit):
    return "wait" if end_policy != "segment_deadline" and in_unit else "transition"
