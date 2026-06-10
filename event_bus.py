"""Lumi 事件总线 — 模块间通信的唯一通道"""

import threading
import uuid
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("event_bus")


@dataclass
class Event:
    """总线上流转的事件"""
    event_type: str
    data: dict
    source: str
    timestamp: float = field(default_factory=time.time)
    correlation_id: Optional[str] = None
    _respond_fn: Optional[Callable] = field(default=None, repr=False)

    def respond(self, data: dict):
        """请求-响应模式：订阅方调用此方法回复请求方"""
        if self._respond_fn:
            self._respond_fn(data)


class EventBus:
    """线程安全的事件总线，支持 publish/subscribe 和 request/response"""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = {}
        self._lock = threading.Lock()
        self._event_log: list[Event] = []
        self._log_lock = threading.Lock()

    def subscribe(self, event_type: str, callback: Callable):
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable):
        with self._lock:
            if event_type in self._subscribers:
                self._subscribers[event_type] = [
                    cb for cb in self._subscribers[event_type] if cb is not callback
                ]

    def publish(self, event_type: str, data: dict, source: str):
        event = Event(event_type=event_type, data=data, source=source)
        self._record(event)
        with self._lock:
            callbacks = list(self._subscribers.get(event_type, []))
        for cb in callbacks:
            try:
                cb(event)
            except Exception:
                logger.exception(f"事件处理异常: {event_type} from {source}")

    def request(self, event_type: str, data: dict, source: str,
                timeout: float = 10.0) -> Optional[dict]:
        """发送请求并等待响应，返回响应数据或 None（超时）"""
        correlation_id = uuid.uuid4().hex[:12]
        response_event = threading.Event()
        response_data = {}

        def on_respond(resp: dict):
            response_data.update(resp)
            response_event.set()

        event = Event(
            event_type=event_type, data=data, source=source,
            correlation_id=correlation_id, _respond_fn=on_respond,
        )
        self._record(event)
        with self._lock:
            callbacks = list(self._subscribers.get(event_type, []))
        for cb in callbacks:
            try:
                cb(event)
            except Exception:
                logger.exception(f"请求处理异常: {event_type} from {source}")

        if response_event.wait(timeout=timeout):
            return response_data
        logger.warning(f"请求超时: {event_type} from {source} (correlation={correlation_id})")
        self.publish("request_timeout", {
            "original_event": event_type,
            "correlation_id": correlation_id,
            "timeout": timeout,
        }, source="event_bus")
        return None

    def _record(self, event: Event):
        with self._log_lock:
            self._event_log.append(event)
            if len(self._event_log) > 1000:
                self._event_log = self._event_log[-500:]
        logger.debug(f"[{event.source}] {event.event_type}: {event.data}")

    def get_recent_events(self, n: int = 50) -> list[Event]:
        with self._log_lock:
            return list(self._event_log[-n:])
