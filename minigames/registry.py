"""Track the one lightweight game host currently attached to conversation."""

import threading

class MinigameRegistry:
    def __init__(self, *, platform=None):
        self._lock = threading.Lock()
        self._active = None
        self._registered = {}
        self._web_platform = platform

    @property
    def web_platform(self):
        if self._web_platform is None:
            from minigames.web_bridge import MinigamePlatformServer
            self._web_platform = MinigamePlatformServer()
        return self._web_platform

    def create_browser(self, game_id: str, html_path):
        from minigames.web_bridge import MinigameWebBridge
        return MinigameWebBridge(
            html_path,
            game_id=game_id,
            platform=self.web_platform,
        )

    def start_platform(
        self,
        *,
        port: int = 8790,
        host: str = "127.0.0.1",
        timeout: float = 5.0,
    ) -> None:
        self.web_platform.start(port=port, host=host, timeout=timeout)

    def register(self, game_id: str, host, *, port: int = 8790):
        game_id = str(game_id).strip()
        if not game_id:
            raise ValueError("game_id cannot be empty")
        if host is None:
            raise ValueError("host cannot be None")
        with self._lock:
            existing = self._registered.get(game_id)
            if existing is not None and existing[0] is not host:
                raise RuntimeError(f"minigame already registered: {game_id}")
            self._registered[game_id] = (host, int(port))

    def has(self, game_id: str) -> bool:
        with self._lock:
            return game_id in self._registered

    def start(self, game_id: str, *, seed=None):
        with self._lock:
            registered = self._registered.get(game_id)
            active = self._active
        if registered is None:
            raise KeyError(f"unknown minigame: {game_id}")

        host, port = registered
        if active is not None:
            if active is host:
                raise RuntimeError(f"minigame is already active: {game_id}")
            raise RuntimeError("another minigame host is already active")
        session_id = host.start(port=port, seed=seed)
        self.activate(host)
        return session_id

    def stop(self, game_id: str) -> bool:
        with self._lock:
            registered = self._registered.get(game_id)
            is_active = registered is not None and self._active is registered[0]
        if registered is None:
            raise KeyError(f"unknown minigame: {game_id}")
        if not is_active:
            return False

        host = registered[0]
        try:
            return bool(host.stop())
        finally:
            self.deactivate(host)

    def activate(self, host):
        if host is None:
            raise ValueError("host cannot be None")
        with self._lock:
            if self._active is not None and self._active is not host:
                raise RuntimeError("another minigame host is already active")
            self._active = host

    def active(self):
        with self._lock:
            return self._active

    def deactivate(self, host) -> bool:
        with self._lock:
            if self._active is not host:
                return False
            self._active = None
            return True

    def shutdown(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            active = self._active
        try:
            if active is not None:
                active.stop()
        finally:
            if active is not None:
                self.deactivate(active)
            if self._web_platform is not None:
                self._web_platform.shutdown(timeout=timeout)


registry = MinigameRegistry()
