"""Exercise the loopback HTTP boundary without external services or sound cards."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from public_host.config import ConfigStore, default_config
from public_host.providers import AudioClip, Cancelled, ProviderError
from public_host.server import HostApp, make_server


class FakeChat:
    def complete(self, messages, cancel):
        if cancel.is_set():
            raise Cancelled()
        return "A short test reply."


class FakeSynth:
    def synthesize(self, text, voice, cancel):
        if cancel.is_set():
            raise Cancelled()
        return AudioClip(b"\0\0" * 80, 8000)


class FakePlayer:
    def play(self, clip, cancel, on_level=None):
        if cancel.is_set():
            raise Cancelled()
        if on_level:
            on_level(0.1)
            on_level(0)
        return "complete"


class BlockedPlayer(FakePlayer):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def play(self, clip, cancel, on_level=None):
        self.entered.set()
        while not self.release.wait(0.01):
            if cancel.is_set():
                raise Cancelled()
        return super().play(clip, cancel, on_level)


class BlockedChat(FakeChat):
    """Simulate a bounded network call that has not observed cancellation yet."""
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, cancel):
        self.entered.set()
        if not self.release.wait(3):
            raise ProviderError("test request timed out")
        return "Late reply must not become completed output."


class PublicHostHTTPTests(unittest.TestCase):
    def test_temporary_room_demo_keeps_saved_config_and_rejects_stale_controls(self):
        saved = self.app.store.load()
        self.app.store.save(saved)
        before = self.app.store.path.read_bytes()
        code, started, _ = self.post("/api/demo", {"run_mode": "manual"})
        self.assertEqual(code, 200)
        self.assertEqual(started["mode"], "demo")
        self.assertEqual(len(started["roles"]), 2)
        self.assertEqual(self.app.store.path.read_bytes(), before)
        ready = self.wait_status(lambda s: s["phase"] == "ready")
        code, _, _ = self.post("/api/chat", {"text": "hello", "run_id": "stale"})
        self.assertEqual(code, 409)
        self.assertEqual(self.status()["requests"], [])
        code, response, _ = self.post("/api/chat", {"text": "hello", "run_id": ready["run_id"]})
        self.assertEqual(code, 200)
        self.wait_status(lambda s: s["current_request"] is not None)
        self.assertEqual(response["request_id"], self.status()["requests"][0]["request_id"])
        self.post("/api/interrupt", {"run_id": ready["run_id"]})
        self.wait_status(lambda s: s["requests"][0]["outcome"] == "cancelled")
        self.assertEqual(self.app.store.path.read_bytes(), before)

    def test_room_demo_reports_missing_gomoku_before_starting(self):
        with patch.object(self.app, "capabilities", return_value={"gomoku": False}):
            code, capabilities, _ = self.request("/api/capabilities")
            self.assertEqual((code, capabilities["gomoku"]), (200, False))
            code, response, _ = self.post("/api/demo", {"run_mode": "program"})
            self.assertEqual(code, 400)
            self.assertIn("aiohttp", response["error"])
            self.assertFalse(self.status()["running"])
            self.assertEqual(self.post("/api/demo", {"run_mode": "manual"})[0], 200)

    def test_program_http_preflight_and_nonblocking_end_controls(self):
        config = default_config()
        config["mode"] = "demo"
        self.assertEqual(self.post("/api/config", config)[0], 200)
        code, rejected, _ = self.post("/api/start", {"run_mode": "program"})
        self.assertEqual(code, 400)
        self.assertIn("两个不同角色", rejected["error"])
        self.assertFalse(self.status()["running"])
        config["roles"][1]["enabled"] = True
        config["program"]["stages"] = [
            {"id": "open", "kind": "opening", "title": "开场", "duration": 30, "prompt": "hello"},
            {"id": "end", "kind": "ending", "title": "结束", "duration": 0.1, "prompt": "bye"}]
        self.assertEqual(self.post("/api/config", config)[0], 200)
        self.assertEqual(self.post("/api/start", {"run_mode": "program"})[0], 200)
        self.wait_status(lambda s: s["program"]["phase"] == "opening")
        self.assertEqual(self.post("/api/config", config)[0], 409)
        self.assertEqual(self.post("/api/start")[0], 409)
        self.assertEqual(self.post("/api/end")[0], 200)
        final = self.wait_status(lambda s: not s["running"] and not s["program"]["running"])
        self.assertEqual(final["program"]["phase"], "finished", final)
        self.assertEqual(self.post("/api/next")[0], 409)
        code, catalog, _ = self.request("/api/activities")
        self.assertEqual(code, 200)
        self.assertEqual(catalog["activities"][0]["roles_required"], 2)
        self.assertEqual(self.post("/api/start", {"run_mode": "manual"})[0], 200)
        self.assertIsNone(self.status()["program"])

    def test_program_rejects_control_port_collision_before_starting(self):
        config = default_config()
        config["mode"] = "demo"
        config["roles"][1]["enabled"] = True
        config["program"]["gomoku_port"] = self.server.server_port
        self.assertEqual(self.post("/api/config", config)[0], 200)
        code, result, _ = self.post("/api/start", {"run_mode": "program"})
        self.assertEqual(code, 400)
        self.assertIn("端口", result["error"])
        self.assertFalse(self.status()["running"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.app = HostApp(ConfigStore(self.directory / "local"))
        self.server = make_server(app=self.app, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.worker.start()
        self.release_events = []
        self.addCleanup(self.cleanup_server)
        self.host = f"127.0.0.1:{self.server.server_port}"

    def cleanup_server(self):
        for event in self.release_events:
            event.set()
        self.app.close(timeout=2)
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def request(self, path, *, method="GET", data=None, headers=None, raw=None):
        outgoing = {"Host": self.host}
        body = None
        if method == "POST":
            outgoing.update({"Content-Type": "application/json", "X-Public-Host": "1",
                             "Origin": f"http://{self.host}"})
            body = raw if raw is not None else json.dumps({} if data is None else data).encode()
        for name, value in (headers or {}).items():
            if value is None:
                outgoing.pop(name, None)
            else:
                outgoing[name] = value
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=outgoing)
            response = connection.getresponse()
            content = response.read().decode("utf-8")
            response_headers = dict(response.getheaders())
            try:
                result = json.loads(content)
            except ValueError:
                result = content
            return response.status, result, response_headers
        finally:
            connection.close()

    def post(self, path, data=None, **kwargs):
        return self.request(path, method="POST", data=data, **kwargs)

    def status(self):
        code, result, _ = self.request("/api/status")
        self.assertEqual(200, code)
        return result

    def wait_status(self, condition, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.status()
            if condition(value):
                return value
            time.sleep(0.01)
        self.fail(f"condition not reached: {self.status()}")

    @staticmethod
    def live_config():
        config = default_config()
        for name in ("llm", "tts"):
            config[name].update(base_url="https://service.invalid/v1", model="test-model",
                                api_key=f"test-only-{name}-secret")
        config["roles"][0]["voice"] = "test-voice"
        return config

    def save_config(self, config):
        code, result, _ = self.post("/api/config", config)
        self.assertEqual(200, code, result)
        return result

    def test_server_listens_only_on_loopback_without_a_remote_bind_option(self):
        self.assertEqual("127.0.0.1", self.server.server_address[0])
        with self.assertRaises(TypeError):
            make_server(app=self.app, host="0.0.0.0", port=0)

    def test_get_config_and_status_do_not_start_services_devices_or_vts(self):
        with patch("public_host.server.make_services") as services, \
                patch("public_host.server.make_player") as player, \
                patch("public_host.server.output_devices") as devices, \
                patch("public_host.avatar.VTSAvatar") as avatar:
            self.assertEqual(200, self.request("/api/config")[0])
            self.assertFalse(self.status()["running"])
            for dependency in (services, player, devices, avatar):
                dependency.assert_not_called()

    def test_config_http_never_returns_saved_keys_and_blank_preserves_them(self):
        config = self.live_config()
        exposed = self.save_config(config)
        for name in ("llm", "tts"):
            self.assertTrue(exposed[name]["has_api_key"])
            self.assertNotIn("api_key", exposed[name])
        code, exposed, headers = self.request("/api/config")
        self.assertEqual(200, code)
        self.assertEqual("no-store", headers["Cache-Control"])
        rendered = json.dumps(exposed)
        self.assertNotIn("test-only-llm-secret", rendered)
        self.assertNotIn("test-only-tts-secret", rendered)
        exposed["llm"]["api_key"] = ""
        self.save_config(exposed)
        self.assertEqual("test-only-llm-secret", self.app.store.load()["llm"]["api_key"])
        exposed["llm"]["clear_api_key"] = True
        response = self.save_config(exposed)
        self.assertFalse(response["llm"]["has_api_key"])
        self.assertEqual("", self.app.store.load()["llm"]["api_key"])

    def test_invalid_host_and_origin_cannot_read_or_modify_configuration(self):
        cases = [{"Host": "attacker.invalid"}, {"Host": "127.0.0.1:1"},
                 {"Origin": "https://attacker.invalid"}, {"Origin": "null"},
                 {"Sec-Fetch-Site": "cross-site"}]
        config = default_config()
        config["mode"] = "demo"
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(400, self.request("/api/config", headers=headers)[0])
                self.assertEqual(400, self.post("/api/config", config, headers=headers)[0])
        self.assertFalse(self.app.store.path.exists())

    def test_localhost_requires_matching_origin(self):
        host = f"localhost:{self.server.server_port}"
        self.assertEqual(200, self.request("/api/status", headers={"Host": host, "Origin": "http://" + host})[0])
        self.assertEqual(400, self.request("/api/status", headers={"Host": host, "Origin": "http://" + self.host})[0])

    def test_post_requires_custom_header_and_json_and_rejects_bad_bodies(self):
        config = default_config()
        for headers in ({"X-Public-Host": None}, {"X-Public-Host": "0"},
                        {"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"}):
            with self.subTest(headers=headers):
                self.assertEqual(400, self.post("/api/config", config, headers=headers)[0])
        for raw in (b"{", b"[]", b"null"):
            with self.subTest(raw=raw):
                self.assertEqual(400, self.post("/api/config", raw=raw)[0])
        self.assertEqual(400, self.post("/api/config", raw=b"{}", headers={"Content-Length": "100001"})[0])
        self.assertFalse(self.app.store.path.exists())

    def test_static_allowlist_blocks_path_traversal_and_file_reading(self):
        web = self.directory / "web"
        web.mkdir()
        (web / "index.html").write_text("<html>test host</html>", encoding="utf-8")
        (self.directory / "secret.txt").write_text("test-only-file-secret", encoding="utf-8")
        with patch("public_host.server.WEB", web):
            code, content, headers = self.request("/")
            self.assertEqual(200, code)
            self.assertIn("test host", content)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/local/config.json",
                         "/public_host/server.py", "/file:///secret.txt"):
                with self.subTest(path=path):
                    code, result, _ = self.request(path)
                    self.assertEqual(404, code)
                    self.assertNotIn("test-only-file-secret", json.dumps(result))

    def test_demo_save_start_submit_interrupt_and_stop_over_http(self):
        config = default_config()
        config["mode"] = "demo"
        self.save_config(config)
        with patch("public_host.playback._sounddevice") as device, \
                patch("public_host.providers.post_json") as external_request:
            self.assertEqual(200, self.post("/api/start")[0])
            code, submitted, _ = self.post("/api/chat", {"text": "Hello"})
            self.assertEqual(200, code)
            request_id = submitted["request_id"]
            result = self.wait_status(lambda s: any(m.get("request_id") == request_id for m in s["messages"]))
            self.assertEqual("simulated", result["last_outcome"])
            self.assertNotEqual("complete", result["last_outcome"])
            self.assertEqual(200, self.post("/api/interrupt")[0])
            self.assertEqual(200, self.post("/api/stop")[0])
            self.wait_status(lambda s: not s["running"])
            device.assert_not_called()
            external_request.assert_not_called()

    def test_default_live_config_reports_missing_settings_without_starting(self):
        with patch("public_host.server.make_services") as services:
            code, result, _ = self.post("/api/start")
            self.assertEqual(400, code)
            self.assertIn("error", result)
            self.assertFalse(self.status()["running"])
            services.assert_not_called()

    def test_running_session_blocks_save_checks_preview_and_second_start(self):
        config = self.live_config()
        self.save_config(config)
        with patch("public_host.server.make_services", return_value=(FakeChat(), FakeSynth())), \
                patch("public_host.server.make_player", return_value=FakePlayer()):
            self.assertEqual(200, self.post("/api/start")[0])
            self.wait_status(lambda s: s["phase"] == "ready")
            actions = [("/api/config", config), ("/api/start", {}), ("/api/probe-chat", {}),
                       ("/api/preview", {"speaker": config["roles"][0]["name"]}),
                       ("/api/avatar", {"speaker": config["roles"][0]["name"]})]
            for path, data in actions:
                with self.subTest(path=path):
                    self.assertEqual(409, self.post(path, data)[0])
            self.post("/api/stop")
            self.wait_status(lambda s: not s["running"])

    def test_stop_keeps_busy_until_late_model_request_really_exits(self):
        config, chat = self.live_config(), BlockedChat()
        self.release_events.append(chat.release)
        self.save_config(config)
        with patch("public_host.server.make_services", return_value=(chat, FakeSynth())), \
                patch("public_host.server.make_player", return_value=FakePlayer()):
            self.post("/api/start")
            self.post("/api/chat", {"text": "Wait"})
            self.assertTrue(chat.entered.wait(1))
            code, result, _ = self.post("/api/stop")
            self.assertEqual(200, code)
            self.assertTrue(result["stopping"])
            self.assertTrue(result["running"])
            self.assertEqual(409, self.post("/api/start")[0])
            self.assertEqual(409, self.post("/api/config", config)[0])
            chat.release.set()
            result = self.wait_status(lambda s: not s["running"])
            self.assertFalse(any(m.get("outcome") == "complete" for m in result["messages"]))
            self.assertEqual(200, self.post("/api/start")[0])
            self.post("/api/stop")
            self.wait_status(lambda s: not s["running"])

    def test_connection_job_blocks_conflicting_operations_until_cancel_finishes(self):
        config, chat = self.live_config(), BlockedChat()
        self.release_events.append(chat.release)
        self.save_config(config)
        with patch("public_host.server.CompatibleChat", return_value=chat):
            self.assertEqual(200, self.post("/api/probe-chat")[0])
            self.assertTrue(chat.entered.wait(1))
            self.assertTrue(self.status()["operation"]["running"])
            for path, data in (("/api/config", config), ("/api/start", {}),
                               ("/api/probe-chat", {}),
                               ("/api/preview", {"speaker": config["roles"][0]["name"]})):
                with self.subTest(path=path):
                    self.assertEqual(409, self.post(path, data)[0])
            self.post("/api/stop")
            self.assertTrue(self.status()["operation"]["running"])
            self.assertEqual(409, self.post("/api/config", config)[0])
            chat.release.set()
            result = self.wait_status(lambda s: not s["operation"]["running"])
            self.assertIn("取消", result["operation"]["message"])
            self.assertIsNone(result["operation"]["result"])
            self.save_config(config)

    def test_probe_background_error_is_visible_without_raw_exception_or_key(self):
        self.save_config(self.live_config())
        service = Mock()
        service.complete.side_effect = RuntimeError("RAW-SECRET-ERROR")
        with patch("public_host.server.CompatibleChat", return_value=service):
            self.assertEqual(200, self.post("/api/probe-chat")[0])
            result = self.wait_status(lambda s: not s["operation"]["running"])
            self.assertIn("未完成", result["operation"]["message"])
            self.assertIsNone(result["operation"]["result"])
            self.assertNotIn("RAW-SECRET-ERROR", json.dumps(result))
            self.assertNotIn("test-only-llm-secret", json.dumps(result))

    def test_preview_complete_exits_and_releases_config_lock(self):
        config = self.live_config()
        self.save_config(config)
        chat = Mock()
        with patch("public_host.server.make_services", return_value=(chat, FakeSynth())), \
                patch("public_host.server.make_player", return_value=FakePlayer()):
            code, _, _ = self.post("/api/preview", {"speaker": config["roles"][0]["name"]})
            self.assertEqual(200, code)
            result = self.wait_status(lambda s: not s["running"])
            self.assertEqual("complete", result["last_outcome"])
            self.assertTrue(result["preview"])
            self.assertEqual("narration", result["messages"][-1]["role"])
            chat.complete.assert_not_called()
            self.save_config(config)

    def test_preview_failure_exits_with_error_without_claiming_completion(self):
        config = self.live_config()
        self.save_config(config)
        player = Mock()
        player.play.side_effect = ProviderError("test audio device unavailable")
        with patch("public_host.server.make_services", return_value=(FakeChat(), FakeSynth())), \
                patch("public_host.server.make_player", return_value=player):
            self.assertEqual(200, self.post("/api/preview", {"speaker": config["roles"][0]["name"]})[0])
            result = self.wait_status(lambda s: not s["running"])
            self.assertEqual("failed", result["last_outcome"])
            self.assertIn("device unavailable", result["last_error"])
            self.assertFalse(any(m.get("outcome") == "complete" for m in result["messages"]))
            self.save_config(config)

    def test_preview_interrupt_exits_and_cannot_remain_ready_forever(self):
        config, player = self.live_config(), BlockedPlayer()
        self.release_events.append(player.release)
        self.save_config(config)
        with patch("public_host.server.make_services", return_value=(FakeChat(), FakeSynth())), \
                patch("public_host.server.make_player", return_value=player):
            self.assertEqual(200, self.post("/api/preview", {"speaker": config["roles"][0]["name"]})[0])
            self.assertTrue(player.entered.wait(1))
            self.assertEqual(409, self.post("/api/config", config)[0])
            self.assertEqual(409, self.post("/api/start")[0])
            self.assertEqual(409, self.post("/api/preview", {"speaker": config["roles"][0]["name"]})[0])
            self.assertEqual(409, self.post("/api/chat", {"text": "must not enter preview"})[0])
            self.assertEqual(200, self.post("/api/interrupt")[0])
            result = self.wait_status(lambda s: not s["running"])
            self.assertEqual("cancelled", result["last_outcome"])
            self.save_config(config)

    def test_avatar_discovery_authorizes_only_on_explicit_boolean_and_closes(self):
        config = self.live_config()
        self.save_config(config)
        for supplied, expected in ((False, False), ("true", False), (True, True)):
            avatar = Mock()
            avatar.models.return_value = [{"modelID": "test-id", "modelName": "Hiyori"}]
            avatar.parameters.return_value = [{"name": "MouthOpen"}]
            with self.subTest(authorize=supplied), patch("public_host.avatar.VTSAvatar", return_value=avatar) as factory:
                code, _, _ = self.post("/api/avatar", {"speaker": config["roles"][0]["name"], "authorize": supplied})
                self.assertEqual(200, code)
                result = self.wait_status(lambda s: not s["operation"]["running"])
                self.assertEqual("test-id", result["operation"]["result"]["models"][0]["modelID"])
                avatar.connect.assert_called_once_with(allow_authorize=expected)
                self.assertEqual("", factory.call_args.args[0]["model_id"])
                avatar.close.assert_called_once()

    def test_failed_avatar_discovery_is_cleaned_up_and_not_reported_connected(self):
        config = self.live_config()
        self.save_config(config)
        avatar = Mock()
        avatar.connect.side_effect = ProviderError("VTS authorization required")
        with patch("public_host.avatar.VTSAvatar", return_value=avatar):
            self.assertEqual(200, self.post("/api/avatar", {"speaker": config["roles"][0]["name"]})[0])
            result = self.wait_status(lambda s: not s["operation"]["running"])
            self.assertIn("authorization required", result["operation"]["message"])
            self.assertIsNone(result["operation"]["result"])
            avatar.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
