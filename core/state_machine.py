"""Lumi 全局状态机 — 系统当前状态的唯一真相源"""

import enum
import threading
import time
import logging
from typing import Callable, Optional

logger = logging.getLogger("state_machine")


class State(enum.Enum):
    IDLE = "IDLE"
    OPENING = "OPENING"
    CHATTING = "CHATTING"
    PLAYING_WORDLE = "PLAYING_WORDLE"
    PLAYING_HANDLE = "PLAYING_HANDLE"
    PLAYING_BUCKSHOT = "PLAYING_BUCKSHOT"
    PLAYING_TERRARIA = "PLAYING_TERRARIA"
    PLAYING_KR = "PLAYING_KR"
    DRAWING = "DRAWING"
    TRANSITIONING = "TRANSITIONING"
    ENDING = "ENDING"


# 活动状态集合（需要经过 TRANSITIONING 才能互相切换的状态）
_ACTIVITY_STATES = {
    State.CHATTING, State.PLAYING_WORDLE, State.PLAYING_HANDLE,
    State.PLAYING_BUCKSHOT, State.PLAYING_TERRARIA, State.PLAYING_KR,
    State.DRAWING,
}

# 合法的状态转移表
_TRANSITIONS: dict[State, set[State]] = {
    State.IDLE: {State.OPENING},
    State.OPENING: {State.CHATTING, State.TRANSITIONING},
    State.CHATTING: {State.TRANSITIONING, State.ENDING},
    State.PLAYING_WORDLE: {State.TRANSITIONING},
    State.PLAYING_HANDLE: {State.TRANSITIONING},
    State.PLAYING_BUCKSHOT: {State.TRANSITIONING},
    State.PLAYING_TERRARIA: {State.TRANSITIONING},
    State.PLAYING_KR: {State.TRANSITIONING},
    State.DRAWING: {State.TRANSITIONING},
    State.TRANSITIONING: {
        State.CHATTING,  # 回滚 / 活动→聊天
        State.ENDING,    # 活动→下播
        State.PLAYING_WORDLE, State.PLAYING_HANDLE, State.PLAYING_BUCKSHOT,
        State.PLAYING_TERRARIA, State.PLAYING_KR, State.DRAWING,
    },
    State.ENDING: {State.IDLE},
}


class InvalidTransition(Exception):
    pass


class StateMachine:
    """线程安全的全局状态机"""

    def __init__(self, bus=None):
        self._state = State.IDLE
        self._metadata: dict = {}
        self._lock = threading.Lock()
        self._callbacks: list[Callable] = []
        self._bus = bus
        if bus:
            bus.subscribe("transition_request", self._on_transition_request)

    @property
    def state(self) -> State:
        return self._state

    @property
    def metadata(self) -> dict:
        with self._lock:
            return dict(self._metadata)

    def on_change(self, callback: Callable):
        """注册状态变更回调: callback(old_state, new_state, metadata)"""
        self._callbacks.append(callback)

    def transition_to(self, target: State, metadata: Optional[dict] = None):
        with self._lock:
            if target not in _TRANSITIONS.get(self._state, set()):
                raise InvalidTransition(
                    f"不允许从 {self._state.value} 转移到 {target.value}"
                )
            old = self._state
            self._state = target
            if target == State.TRANSITIONING and metadata:
                self._metadata = {**metadata, "started_at": time.time()}
            else:
                self._metadata = {}
            meta_copy = dict(self._metadata)

        logger.info(f"状态变更: {old.value} → {target.value}"
                     + (f" meta={meta_copy}" if meta_copy else ""))

        if self._bus:
            self._bus.publish("state_changed", {
                "old": old.value,
                "new": target.value,
                "metadata": meta_copy,
            }, source="state_machine")

        for cb in self._callbacks:
            try:
                cb(old, target, meta_copy)
            except Exception:
                logger.exception("状态变更回调异常")

    def _on_transition_request(self, event):
        target_str = event.data.get("target")
        metadata = event.data.get("metadata")
        try:
            target = State(target_str)
            self.transition_to(target, metadata=metadata)
        except (ValueError, InvalidTransition) as e:
            logger.warning(f"状态切换请求被拒绝: {e}")

    def is_playing(self) -> bool:
        return self._state.value.startswith("PLAYING_")

    def is_activity(self) -> bool:
        return self._state in _ACTIVITY_STATES
