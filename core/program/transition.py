"""One ordered transition path; adapters implement I/O, not another scheduler.

Native foreground release and complete process exit are deliberately distinct.
Production-specific hooks may halt a transaction while retaining its owner.
"""
from dataclasses import dataclass
from typing import Callable


class TransitionHalted(BaseException):
    """Internal return/continue signal, never an adapter/business failure.

    Inherit outside Exception so existing nested adapter recovery blocks cannot
    swallow an early return that was lifted into a phase hook. Only the shared
    transition and finish boundaries catch this control signal.
    """
    def __init__(self, result=False):
        self.result = result


def stop_confirmed(receipt, requirement="stopped"):
    if requirement == "legacy":
        return True
    if not isinstance(receipt, dict):
        return False
    closed = receipt.get("closed") is True and receipt.get("gameClosed") is True
    if requirement == "released":
        return receipt.get("foregroundReleased") is True or closed
    if requirement == "closed":
        return closed
    return receipt.get("status") == "stopped"


def preparation_accepted(receipt, policy="ready"):
    if not isinstance(receipt, dict) or not receipt:
        return False
    if policy == "legacy":
        return receipt.get("status") != "failed"
    return receipt.get("status") in {"ready", "cold_start_required"}


def activation_accepted(receipt):
    return isinstance(receipt, dict) and receipt.get("status") == "active"


def _noop():
    return None


def _raise(error):
    raise error


@dataclass
class TransitionHooks:
    # These phases match the production path, including early native release,
    # late legacy stop and presentation commit before controlled activation.
    before: Callable = _noop
    enter: Callable = _noop
    announce: Callable = _noop
    stop: Callable = _noop
    prepare: Callable = _noop
    commit: Callable = _noop
    activate: Callable = _noop
    complete: Callable = _noop
    failed: Callable = _raise
    current: Callable = lambda: True


def run_transition(hooks):
    """Execute the same cancellation boundaries and phase ordering in both hosts.

Hooks use the shared receipt predicates before returning from stop/prepare/
activate; they retain device-specific failure recovery and native ownership.
No generic user-provided step list can reorder these phases.
"""
    try:
        if not hooks.current():
            return False
        hooks.before()  # release native control BEFORE scene changes or speech
        if not hooks.current():
            return False
        hooks.enter()
        if not hooks.current():
            return False
        hooks.announce()
        if not hooks.current():
            return False
        hooks.stop()    # only old activities not already released above
        if not hooks.current():
            return False
        hooks.prepare()
        if not hooks.current():
            return False
        hooks.commit()  # target scene/clock must precede native activation
        if not hooks.current():
            return False
        hooks.activate()
        if not hooks.current():
            return False
    except TransitionHalted as halt:
        return halt.result
    except Exception as error:
        return hooks.failed(error)
    # Production completion ran outside its preparation/activation recovery
    # block. Do not replay activity recovery if final notification itself fails.
    return hooks.complete()


def finish_program(close_active, drain, complete):
    """Neither an unconfirmed close nor failed drain may announce completion."""
    try:
        if close_active() is False:
            return False
        if drain() is False:
            return False
        complete()
        return True
    except TransitionHalted as halt:
        return halt.result
