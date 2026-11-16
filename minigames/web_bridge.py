"""Process-lifetime browser platform for lightweight game overlays."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import threading
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from aiohttp import WSCloseCode, web


_PLATFORM_SHELL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>AI Streamer Minigames</title>
  <style>
    html,body,#minigame-frame{width:100%;height:100%;margin:0;border:0;overflow:hidden;background:transparent}
    #minigame-frame[hidden]{display:none}
  </style>
</head>
<body>
  <iframe id="minigame-frame" allow="autoplay" hidden></iframe>
  <script>
    const frame = document.getElementById('minigame-frame');
    let retry = 500;
    function showPlatformState(message) {
      if (!message.active || !message.game_id || !message.game_url) {
        frame.hidden = true;
        frame.dataset.gameId = '';
        frame.dataset.gameUrl = '';
        frame.src = 'about:blank';
        return;
      }
      if (frame.dataset.gameUrl !== message.game_url) {
        frame.dataset.gameId = message.game_id;
        frame.dataset.gameUrl = message.game_url;
        frame.src = message.game_url;
      }
      frame.hidden = false;
    }
    function connect() {
      const socket = new WebSocket(`ws://${location.host}/ws?role=shell`);
      socket.onopen = () => { retry = 500; };
      socket.onmessage = event => {
        const message = JSON.parse(event.data);
        if (message.type === 'platform_state') showPlatformState(message);
      };
      socket.onclose = () => {
        setTimeout(connect, retry);
        retry = Math.min(Math.round(retry * 1.6), 5000);
      };
    }
    connect();
  </script>
</body>
</html>
"""


class MinigamePlatformServer:
    """Own one stable OBS page and route it to the active lightweight game."""

    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 0
        self._games: dict[str, Path] = {}
        self._active_game_id: str | None = None
        self._active_page_token: str | None = None
        self._active_session_id: str | None = None
        self._latest_states: dict[str, dict] = {}
        self._state_lock = threading.RLock()
        self._shell_outboxes: set[asyncio.Queue] = set()
        self._game_outboxes: dict[str, set[asyncio.Queue]] = {}
        self._outboxes_lock = threading.Lock()
        self._websockets: set[web.WebSocketResponse] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._stop_requested = threading.Event()
        self._startup_error: BaseException | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def shell_websocket_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws?role=shell"

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and self._started.is_set())

    @property
    def active_game_id(self) -> str | None:
        with self._state_lock:
            return self._active_game_id

    def register_game(self, game_id: str, html_path) -> None:
        game_id = str(game_id).strip()
        html_path = Path(html_path).resolve()
        if not game_id:
            raise ValueError("game_id cannot be empty")
        if not html_path.is_file():
            raise FileNotFoundError(html_path)
        with self._state_lock:
            existing = self._games.get(game_id)
            if existing is not None and existing != html_path:
                raise RuntimeError(f"minigame HTML already registered: {game_id}")
            self._games[game_id] = html_path

    def game_websocket_url(self, game_id: str) -> str:
        return (
            f"ws://{self.host}:{self.port}/ws?game_id="
            f"{quote(str(game_id), safe='')}"
        )

    def _platform_state(self) -> dict:
        with self._state_lock:
            game_id = self._active_game_id
            page_token = self._active_page_token
        return {
            "type": "platform_state",
            "active": game_id is not None,
            "game_id": game_id,
            "game_url": (
                f"/games/{quote(game_id, safe='')}/?v={page_token}"
                if game_id is not None and page_token is not None
                else None
            ),
        }

    @staticmethod
    def _offer_latest(outbox: asyncio.Queue, payload: dict) -> None:
        if outbox.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                outbox.get_nowait()
        outbox.put_nowait(deepcopy(payload))

    def _broadcast(self, outboxes: tuple[asyncio.Queue, ...], payload: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for outbox in outboxes:
            loop.call_soon_threadsafe(self._offer_latest, outbox, payload)

    def activate(self, game_id: str) -> None:
        game_id = str(game_id).strip()
        with self._state_lock:
            if game_id not in self._games:
                raise KeyError(f"unknown minigame page: {game_id}")
            active = self._active_game_id
            if active is not None and active != game_id:
                raise RuntimeError(f"another minigame page is active: {active}")
            self._active_game_id = game_id
            self._active_page_token = secrets.token_hex(8)
            self._active_session_id = None
            self._latest_states.pop(game_id, None)
        with self._outboxes_lock:
            shell_outboxes = tuple(self._shell_outboxes)
        self._broadcast(shell_outboxes, self._platform_state())

    def deactivate(self, game_id: str) -> bool:
        game_id = str(game_id).strip()
        with self._state_lock:
            if self._active_game_id != game_id:
                return False
            self._active_game_id = None
            self._active_page_token = None
            self._active_session_id = None
            self._latest_states.pop(game_id, None)
        with self._outboxes_lock:
            shell_outboxes = tuple(self._shell_outboxes)
        self._broadcast(shell_outboxes, self._platform_state())
        return True

    def publish_state(self, game_id: str, state: dict) -> bool:
        game_id = str(game_id).strip()
        payload = {
            "type": "game_state",
            "game_id": game_id,
            "state": deepcopy(state),
        }
        with self._state_lock:
            if self._active_game_id != game_id:
                return False
            session_id = str(state.get("session_id") or "").strip()
            if session_id:
                if self._active_session_id is None:
                    self._active_session_id = session_id
                elif self._active_session_id != session_id:
                    return False
            self._latest_states[game_id] = payload
        with self._outboxes_lock:
            outboxes = tuple(self._game_outboxes.get(game_id, ()))
        self._broadcast(outboxes, payload)
        return True

    async def _serve_shell(self, _request):
        return web.Response(
            text=_PLATFORM_SHELL_HTML,
            content_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    async def _serve_game_file(self, request):
        game_id = request.match_info["game_id"]
        tail = request.match_info.get("tail") or ""
        with self._state_lock:
            html_path = self._games.get(game_id)
        if html_path is None:
            raise web.HTTPNotFound()
        root = html_path.parent
        target = html_path if not tail else (root / tail).resolve()
        if target != root and root not in target.parents:
            raise web.HTTPNotFound()
        if not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(
            target,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    async def _send_updates(
        self,
        websocket: web.WebSocketResponse,
        outbox: asyncio.Queue,
    ) -> None:
        while not websocket.closed:
            payload = await outbox.get()
            await websocket.send_json(payload)

    async def _websocket_handler(self, request):
        role = request.query.get("role")
        game_id = str(request.query.get("game_id") or "").strip()
        if role != "shell":
            with self._state_lock:
                known_game = game_id in self._games
            if not known_game:
                raise web.HTTPBadRequest(text="missing or unknown game_id")

        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        outbox: asyncio.Queue = asyncio.Queue(maxsize=1)
        sender = None
        try:
            self._websockets.add(websocket)
            if role == "shell":
                with self._outboxes_lock:
                    self._shell_outboxes.add(outbox)
                await websocket.send_json(self._platform_state())
            else:
                with self._outboxes_lock:
                    self._game_outboxes.setdefault(game_id, set()).add(outbox)
                with self._state_lock:
                    latest = (
                        deepcopy(self._latest_states.get(game_id))
                        if self._active_game_id == game_id
                        else None
                    )
                if latest is not None:
                    await websocket.send_json(latest)

            sender = asyncio.create_task(self._send_updates(websocket, outbox))
            async for _message in websocket:
                pass
        finally:
            try:
                if sender is not None:
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender
            finally:
                # A disconnect during the initial send or a failed sender must
                # not leave a client/outbox registered in the persistent host.
                self._websockets.discard(websocket)
                with self._outboxes_lock:
                    if role == "shell":
                        self._shell_outboxes.discard(outbox)
                    else:
                        game_outboxes = self._game_outboxes.get(game_id)
                        if game_outboxes is not None:
                            game_outboxes.discard(outbox)
                            if not game_outboxes:
                                self._game_outboxes.pop(game_id, None)
        return websocket

    def _create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._serve_shell)
        app.router.add_get("/ws", self._websocket_handler)
        app.router.add_get("/games/{game_id}/", self._serve_game_file)
        app.router.add_get("/games/{game_id}/{tail:.*}", self._serve_game_file)
        return app

    def start(
        self,
        *,
        port: int,
        host: str = "127.0.0.1",
        timeout: float = 5.0,
    ) -> None:
        requested_port = int(port)
        thread = self._thread
        if thread is not None and thread.is_alive():
            if host != self.host or requested_port not in {0, self.port}:
                raise RuntimeError(
                    f"minigame platform already runs at {self.host}:{self.port}"
                )
            return
        self.host = host
        self.port = requested_port
        self._started.clear()
        self._stop_requested.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._server_thread,
            daemon=True,
            name="minigame-platform-server",
        )
        self._thread.start()
        if not self._started.wait(timeout=timeout):
            raise TimeoutError("minigame browser platform did not start in time")
        if self._startup_error is not None:
            raise RuntimeError("minigame browser platform failed to start") from self._startup_error

    async def _close_websockets(self) -> None:
        clients = tuple(self._websockets)
        if not clients:
            return
        close_tasks = [
            websocket.close(
                code=WSCloseCode.GOING_AWAY,
                message=b"minigame platform shutting down",
            )
            for websocket in clients
        ]
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*close_tasks, return_exceptions=True),
                timeout=1.0,
            )

    def _server_thread(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def run_server():
            runner = web.AppRunner(
                self._create_app(),
                access_log=None,
                shutdown_timeout=0.5,
            )
            await runner.setup()
            try:
                site = web.TCPSite(runner, self.host, self.port)
                await site.start()
                sockets = tuple(site._server.sockets or ())
                if sockets:
                    self.port = int(sockets[0].getsockname()[1])
                self._started.set()
                while not self._stop_requested.is_set():
                    await asyncio.sleep(0.05)
            finally:
                await self._close_websockets()
                await runner.cleanup()

        try:
            loop.run_until_complete(run_server())
        except BaseException as exc:
            self._startup_error = exc
            self._started.set()
        finally:
            self._loop = None
            loop.close()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._thread = None
            return
        self._stop_requested.set()
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise TimeoutError("minigame browser platform did not stop in time")
        self._thread = None
        self._started.clear()
        with self._state_lock:
            self._active_game_id = None
            self._active_page_token = None
            self._active_session_id = None
            self._latest_states.clear()


class MinigameWebBridge:
    """Per-game channel attached to the shared process-lifetime platform."""

    def __init__(
        self,
        html_path,
        *,
        game_id: str | None = None,
        platform: MinigamePlatformServer | None = None,
    ):
        self.html_path = Path(html_path).resolve()
        self.game_id = str(game_id or self.html_path.parent.name).strip()
        self.platform = platform or MinigamePlatformServer()
        self.platform.register_game(self.game_id, self.html_path)

    @property
    def host(self) -> str:
        return self.platform.host

    @property
    def port(self) -> int:
        return self.platform.port

    @property
    def url(self) -> str:
        return self.platform.url

    @property
    def websocket_url(self) -> str:
        return self.platform.game_websocket_url(self.game_id)

    def publish_state(self, state: dict) -> bool:
        return self.platform.publish_state(self.game_id, state)

    def start(
        self,
        *,
        port: int,
        host: str = "127.0.0.1",
        timeout: float = 5.0,
    ) -> None:
        self.platform.start(port=port, host=host, timeout=timeout)
        self.platform.activate(self.game_id)

    def stop(self, *, timeout: float = 5.0) -> None:
        del timeout
        self.platform.deactivate(self.game_id)

    def shutdown(self, *, timeout: float = 5.0) -> None:
        self.platform.shutdown(timeout=timeout)
