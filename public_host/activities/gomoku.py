"""Public lifecycle adapter for the existing local two-player Gomoku game.

Only prepare imports the optional web dependency. The platform belongs to this
adapter, survives individual segments, and is released by close(). Lifecycle
methods are synchronous and must be called by the programme worker, not HTTP.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import threading


class GomokuActivity:
    game_id = "gomoku"
    label = "五子棋"

    def __init__(self, bus, characters, port=8791, *, event_callback=None,
                 move_interval=1.0, round_pause=1.5, round_timeout=120.0,
                 seed=None):
        names = list(characters)
        if (len(names) != 2 or any(not isinstance(name, str) or not name.strip() for name in names)
                or len({name.strip().casefold() for name in names}) != 2):
            raise ValueError("五子棋需要启用两个名字不同的角色。")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("五子棋网页端口必须是有效的整数端口。")
        self.bus = bus
        self.characters = names
        self.port = port
        self.event_callback = event_callback
        self._settings = dict(move_interval=move_interval, round_pause=round_pause,
                              round_timeout=round_timeout, seed=seed)
        self._host = None
        self._registry = None
        self._operations = threading.RLock()
        self._lock = threading.RLock()
        self._phase = "idle"
        self._ready = False
        self._closed = False
        self._state = {}
        self._result = {}
        self._last_error = ""

    @staticmethod
    def _check_cancel(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("五子棋准备或启动已取消。")

    def _on_event(self, event_type, data):
        with self._lock:
            if event_type == "game_state":
                self._state = deepcopy(data.get("state") or {})
            elif event_type == "match_finalized":
                self._result = deepcopy(data)
                self._state["match_result"] = deepcopy(data)
            elif event_type == "game_event" and data.get("event_type") == "round_complete":
                self._result = deepcopy(data.get("result") or {})
            elif event_type == "game_event" and data.get("event_type") == "host_error":
                self._last_error = str(data.get("error") or "五子棋运行失败。")
                self._phase = "failed"
                self._ready = False
        self.bus.publish(event_type, deepcopy(data), source="gomoku")
        if self.event_callback is not None:
            self.event_callback(event_type, deepcopy(data))

    def prepare(self, cancel_event=None):
        with self._operations:
            if self._closed:
                raise RuntimeError("五子棋宿主已关闭。")
            self._check_cancel(cancel_event)
            if self._host is not None and self.snapshot()["running"]:
                raise RuntimeError("五子棋仍在运行或清理，不能重新准备。")
            with self._lock:
                self._phase, self._ready, self._last_error = "preparing", False, ""
                self._result = {}
            try:
                if self._host is None:
                    from minigames.gomoku import create_gomoku_host
                    from minigames.registry import MinigameRegistry
                    self._registry = MinigameRegistry()
                    self._host = create_gomoku_host(
                        bus=self.bus, active_characters=self.characters,
                        html_path=Path(__file__).resolve().parents[2] / "minigames" / "gomoku" / "index.html",
                        registry=self._registry, event_callback=self._on_event,
                        **self._settings,
                    )
                # Binding our own socket is the ownership check. A conflict
                # fails here; never kill, attach to, or repurpose another host.
                self._host.browser.start(port=self.port)
                self._check_cancel(cancel_event)
                self._host.adapter.reset_match()
                state = self._host.adapter.snapshot()
                state.update(status="ready", actor="")
                self._host.browser.publish_state({"game": self.game_id, "state": state})
                with self._lock:
                    self._state = state
                    self._phase, self._ready = "ready", True
                return self.snapshot()
            except ImportError as exc:
                self._fail("五子棋网页需要 aiohttp，请先安装 requirements-host.txt。")
                raise RuntimeError(self._last_error) from exc
            except Exception as exc:
                if self._host is not None:
                    self._host.browser.stop()
                message = f"五子棋网页准备失败（端口 {self.port}）：{exc}"
                self._fail(message)
                raise RuntimeError(message) from exc

    def activate(self, cancel_event=None):
        with self._operations:
            self._check_cancel(cancel_event)
            if self._closed or not self._ready or self._host is None:
                raise RuntimeError("五子棋网页尚未准备就绪。")
            if self.snapshot()["running"]:
                raise RuntimeError("五子棋已经启动。")
            try:
                self._host.start(port=self._host.browser.port, seed=self._settings["seed"])
                self._check_cancel(cancel_event)
                if not self._host.running or not self._host.browser.platform.running:
                    raise RuntimeError("五子棋没有进入运行状态。")
                with self._lock:
                    self._phase = "active"
                return self.snapshot()
            except Exception as exc:
                try:
                    self.stop(reason="activation_failed")
                finally:
                    self._fail(str(exc))
                raise

    def _fail(self, message):
        with self._lock:
            self._phase, self._ready, self._last_error = "failed", False, str(message)

    def stop(self, reason="stopped"):
        with self._operations:
            with self._lock:
                self._phase, self._ready = "stopping", False
            try:
                if self._host is not None:
                    self._host.stop()
                if self.snapshot()["running"]:
                    raise TimeoutError("五子棋仍在清理，尚未停止。")
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                raise
            with self._lock:
                self._phase = "stopped"
            return {**self.snapshot(), "reason": str(reason)}

    def snapshot(self):
        with self._lock:
            host = self._host
            worker_alive = bool(host and host._worker and host._worker.is_alive())
            running = bool(host and (host.running or worker_alive))
            stopped = not running and self._phase in {"idle", "stopped", "closed"}
            session_id = host.runtime.session_id if host is not None and self._phase not in {"idle", "ready", "preparing"} else ""
            return {
                "game_id": self.game_id, "phase": self._phase, "status": self._phase,
                "ready": self._ready, "active": self._phase == "active" and running,
                "running": running, "stopped": stopped,
                "closed": stopped, "gameClosed": stopped,
                "platform_closed": self._closed,
                "platform_running": bool(host and host.browser.platform.running),
                "session_id": session_id, "instance_id": session_id,
                "url": host.browser.url if host is not None else "",
                "state": deepcopy(self._state), "result": deepcopy(self._result),
                "last_error": self._last_error,
            }

    def close(self):
        with self._operations:
            if self._closed:
                return self.snapshot()
            self.stop(reason="host_closed")
            if self._host is not None:
                self._host.close()
            if self._registry is not None:
                self._registry.shutdown()
            with self._lock:
                self._closed, self._phase = True, "closed"
            return self.snapshot()
