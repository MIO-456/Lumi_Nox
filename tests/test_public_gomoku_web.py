"""WebSocket failures must release the platform's per-client registrations."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HAS_AIOHTTP = importlib.util.find_spec("aiohttp") is not None
if HAS_AIOHTTP:
    from minigames.web_bridge import MinigamePlatformServer


class FakeSocket:
    def __init__(self, *, first_error=False, sender_entered=None):
        self.first_error = first_error
        self.sender_entered = sender_entered
        self.closed = False

    async def prepare(self, request):
        return None

    async def send_json(self, payload):
        if self.first_error:
            raise ConnectionResetError("client disconnected before initial state")

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sender_entered is not None:
            await self.sender_entered.wait()
        self.closed = True
        raise StopAsyncIteration


@unittest.skipUnless(HAS_AIOHTTP, "optional aiohttp not installed")
class GomokuWebCleanupTests(unittest.IsolatedAsyncioTestCase):
    def platform(self):
        platform = MinigamePlatformServer()
        platform._games["gomoku"] = Path("unused-game.html")
        platform._active_game_id = "gomoku"
        platform._latest_states["gomoku"] = {"type": "game_state", "state": {}}
        return platform

    def assert_released(self, platform):
        self.assertEqual(platform._websockets, set())
        self.assertEqual(platform._shell_outboxes, set())
        self.assertEqual(platform._game_outboxes, {})

    async def test_disconnect_during_first_state_releases_shell_and_game_clients(self):
        for role in ("shell", "game"):
            with self.subTest(role=role):
                platform = self.platform()
                socket = FakeSocket(first_error=True)
                request = SimpleNamespace(query={"role": role, "game_id": "gomoku"})
                with patch("minigames.web_bridge.web.WebSocketResponse", return_value=socket):
                    with self.assertRaisesRegex(ConnectionResetError, "initial state"):
                        await platform._websocket_handler(request)
                self.assert_released(platform)

    async def test_failed_sender_cannot_skip_removing_shell_and_game_clients(self):
        for role in ("shell", "game"):
            with self.subTest(role=role):
                platform = self.platform()
                sender_entered = asyncio.Event()
                socket = FakeSocket(sender_entered=sender_entered)
                request = SimpleNamespace(query={"role": role, "game_id": "gomoku"})
                async def fail_sender(*args):
                    sender_entered.set()
                    raise ConnectionResetError("update sender failed")
                with patch("minigames.web_bridge.web.WebSocketResponse", return_value=socket), \
                        patch.object(platform, "_send_updates", fail_sender):
                    with self.assertRaisesRegex(ConnectionResetError, "sender failed"):
                        await platform._websocket_handler(request)
                self.assert_released(platform)


if __name__ == "__main__":
    unittest.main()
