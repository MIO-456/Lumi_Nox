"""VTS protocol tests use fake sockets and no installed/authorized VTS app."""
import json
import builtins
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from public_host.avatar import VTSAvatar
from public_host.providers import ProviderError


class FakeVTS:
    def __init__(self):
        self.sent = []
        self.replies = queue.Queue()
        self.closed = False
        self.current_model = "hiyori-test-id"
        self.model_loaded = True
        self.parameter_names = ["MouthOpen", "FaceAngleX"]
        self.allow_auth = True
        self.token_denied = False
        self.drop_type = None
        self.inject_error = False
        self.unmatched_reply = False
        self.malformed = False
        self.block_injection = threading.Event()
        self.block_injection.set()
        self.inject_received = threading.Event()

    def __call__(self, url, **kwargs):
        self.url = url
        self.options = kwargs
        return self

    def send(self, packet):
        if self.closed:
            raise OSError("fake secret must not escape")
        request = json.loads(packet)
        self.sent.append(request)
        kind = request["messageType"]
        if kind == self.drop_type:
            return
        payload = {}
        response_type = kind.removesuffix("Request") + "Response"
        if kind == "AuthenticationTokenRequest":
            payload = {"authenticationToken": "fake-test-token-not-a-real-secret"}
            if self.token_denied:
                response_type, payload = "APIError", {"errorID": 50, "message": "SECRET-RAW-BODY"}
        elif kind == "AuthenticationRequest":
            payload = {"authenticated": self.allow_auth}
        elif kind == "CurrentModelRequest":
            payload = {"modelLoaded": self.model_loaded, "modelID": self.current_model}
        elif kind == "AvailableModelsRequest":
            payload = {"availableModels": [{"modelID": self.current_model,
                        "modelName": "Hiyori", "modelLoaded": self.model_loaded}]}
        elif kind == "InputParameterListRequest":
            payload = {"defaultParameters": [{"name": name, "min": 0, "max": 1}
                        for name in self.parameter_names], "customParameters": []}
        elif kind == "InjectParameterDataRequest":
            self.inject_received.set()
            if self.inject_error:
                response_type, payload = "APIError", {"errorID": 450, "message": "SECRET-RAW-BODY"}
        if self.malformed:
            self.replies.put("not JSON")
            return
        if self.unmatched_reply:
            self.replies.put(json.dumps({"requestID": "an-unrelated-request", "messageType": "APIError"}))
        self.replies.put(json.dumps({"requestID": request["requestID"],
                                    "messageType": response_type, "data": payload}))

    def recv(self, timeout):
        if self.sent[-1]["messageType"] == "InjectParameterDataRequest":
            if not self.block_injection.wait(timeout):
                raise TimeoutError
        try:
            return self.replies.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None

    def close(self):
        self.closed = True

    def types(self):
        return [request["messageType"] for request in self.sent]

    def mouth_values(self):
        return [request["data"]["parameterValues"][0]["value"] for request in self.sent
                if request["messageType"] == "InjectParameterDataRequest"]


def eventually(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


class VTSAvatarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"enabled": True, "url": "ws://127.0.0.1:8001",
                       "model_id": "hiyori-test-id", "mouth_parameter": "MouthOpen", "mouth_gain": 1}
        self.fake = FakeVTS()

    def avatar(self, **config):
        avatar = VTSAvatar(self.config | config, self.temp.name, connector=self.fake,
                           request_timeout=0.1, frame_interval=0.01, model_check_interval=0.02,
                           authorization_timeout=0.1)
        self.addCleanup(avatar.close)
        return avatar

    def test_disabled_never_connects_or_imports_dependency(self):
        avatar = self.avatar(enabled=False)
        avatar.connect(allow_authorize=True)
        avatar.set_mouth_level(1, generation=1)
        avatar.close()
        self.assertFalse(avatar.connected)
        self.assertEqual([], self.fake.sent)
        self.assertFalse(list(Path(self.temp.name).iterdir()))

    def test_missing_token_does_not_request_authorization(self):
        avatar = self.avatar()
        with self.assertRaisesRegex(ProviderError, "Authorize"):
            avatar.connect()
        self.assertEqual([], self.fake.sent)

    def test_explicit_authorization_saves_then_reuses_only_local_token(self):
        avatar = self.avatar(model_id="")
        avatar.connect(allow_authorize=True)
        self.assertEqual(["AuthenticationTokenRequest", "AuthenticationRequest"], self.fake.types())
        self.assertEqual(1, len(list(Path(self.temp.name).glob("*.json"))))
        avatar.close()
        another = FakeVTS()
        resumed = VTSAvatar(self.config | {"model_id": ""}, self.temp.name, connector=another)
        self.addCleanup(resumed.close)
        resumed.connect()
        self.assertEqual(["AuthenticationRequest"], another.types())
        self.assertEqual(VTSAvatar.PLUGIN_NAME, another.sent[0]["data"]["pluginName"])

    def test_tokens_are_distinct_for_different_ports(self):
        first, second = self.avatar(model_id=""), self.avatar(model_id="", url="ws://127.0.0.1:8002")
        first.connect(allow_authorize=True)
        with self.assertRaisesRegex(ProviderError, "Authorize"):
            second.connect()
        self.assertNotEqual(first._token_path, second._token_path)

    def test_denied_authorization_never_persists_token_or_exposes_body(self):
        self.fake.token_denied = True
        with self.assertRaisesRegex(ProviderError, "error 50") as error:
            self.avatar().connect(allow_authorize=True)
        self.assertNotIn("SECRET-RAW-BODY", str(error.exception))
        self.assertEqual([], list(Path(self.temp.name).iterdir()))
        self.assertTrue(self.fake.closed)

    def test_rejected_existing_token_does_not_reauthorize_implicitly(self):
        avatar = self.avatar(model_id="")
        avatar.connect(allow_authorize=True)
        avatar.close()
        self.fake = FakeVTS()
        self.fake.allow_auth = False
        retry = self.avatar(model_id="")
        with self.assertRaisesRegex(ProviderError, "rejected"):
            retry.connect()
        self.assertEqual(["AuthenticationRequest"], self.fake.types())

    def test_discovery_lists_without_injecting_or_loading_models(self):
        avatar = self.avatar(model_id="")
        avatar.connect(allow_authorize=True)
        self.assertEqual("hiyori-test-id", avatar.models()[0]["modelID"])
        self.assertEqual("MouthOpen", avatar.parameters()[0]["name"])
        with self.assertRaisesRegex(ProviderError, "Select"):
            avatar.set_mouth_level(0.5)
        avatar.close()
        self.assertNotIn("InjectParameterDataRequest", self.fake.types())
        self.assertNotIn("ModelLoadRequest", self.fake.types())

    def test_initial_model_mismatch_never_injects(self):
        self.fake.current_model = "a-private-model"
        with self.assertRaisesRegex(ProviderError, "does not match"):
            self.avatar().connect(allow_authorize=True)
        self.assertNotIn("InjectParameterDataRequest", self.fake.types())

    def test_missing_mouth_input_never_injects(self):
        self.fake.parameter_names = ["FaceAngleX"]
        with self.assertRaisesRegex(ProviderError, "mouth input"):
            self.avatar().connect(allow_authorize=True)
        self.assertNotIn("InjectParameterDataRequest", self.fake.types())

    def test_injection_uses_tracking_name_and_clamps_gain(self):
        avatar = self.avatar(mouth_gain=2)
        avatar.connect(allow_authorize=True)
        avatar.set_mouth_level(0.8, generation=1)
        eventually(lambda: 1.0 in self.fake.mouth_values())
        packet = next(item for item in self.fake.sent if item["messageType"] == "InjectParameterDataRequest")
        self.assertEqual("MouthOpen", packet["data"]["parameterValues"][0]["id"])
        self.assertEqual("set", packet["data"]["mode"])
        self.assertLessEqual(max(self.fake.mouth_values()), 1.0)

    def test_stale_generation_is_ignored(self):
        avatar = self.avatar()
        avatar.connect(allow_authorize=True)
        avatar.set_mouth_level(0.8, generation=10)
        eventually(lambda: 0.8 in self.fake.mouth_values())
        avatar.set_mouth_level(0.2, generation=11)
        eventually(lambda: 0.2 in self.fake.mouth_values())
        avatar.set_mouth_level(0.9, generation=10)
        time.sleep(0.03)
        self.assertNotIn(0.9, self.fake.mouth_values())

    def test_set_level_does_not_wait_for_a_blocked_websocket(self):
        avatar = self.avatar()
        avatar.connect(allow_authorize=True)
        eventually(lambda: bool(self.fake.mouth_values()))
        self.fake.inject_received.clear()
        self.fake.block_injection.clear()
        self.assertTrue(self.fake.inject_received.wait(0.2))
        before = time.monotonic()
        avatar.set_mouth_level(0.5, generation=1)
        self.assertLess(time.monotonic() - before, 0.03)
        self.fake.block_injection.set()

    def test_model_change_stops_output_and_surfaces_error(self):
        avatar = self.avatar()
        avatar.connect(allow_authorize=True)
        eventually(lambda: bool(self.fake.mouth_values()))
        self.fake.current_model = "another-model"
        eventually(lambda: bool(avatar.last_error))
        with self.assertRaisesRegex(ProviderError, "does not match"):
            avatar.check_error()
        before = len(self.fake.mouth_values())
        avatar.close()
        self.assertEqual(before, len(self.fake.mouth_values()))
        self.assertFalse(avatar.connected)

    def test_background_protocol_error_is_visible_and_sanitized(self):
        avatar = self.avatar()
        avatar.connect(allow_authorize=True)
        self.fake.inject_error = True
        eventually(lambda: bool(avatar.last_error))
        with self.assertRaisesRegex(ProviderError, "error 450") as error:
            avatar.check_error()
        self.assertNotIn("SECRET", str(error.exception))
        with self.assertRaises(ProviderError):
            avatar.set_mouth_level(0.5)

    def test_close_zeros_and_blocks_late_frames_without_unloading_model(self):
        avatar = self.avatar()
        avatar.connect(allow_authorize=True)
        avatar.set_mouth_level(0.6, generation=2)
        eventually(lambda: 0.6 in self.fake.mouth_values())
        avatar.close()
        self.assertEqual(0.0, self.fake.mouth_values()[-1])
        before = len(self.fake.sent)
        avatar.set_mouth_level(0.9, generation=2)
        time.sleep(0.02)
        self.assertEqual(before, len(self.fake.sent))
        self.assertTrue(self.fake.closed)
        self.assertNotIn("ModelLoadRequest", self.fake.types())
        avatar.check_error()

    def test_close_does_not_zero_new_model(self):
        avatar = self.avatar()
        avatar.connect(allow_authorize=True)
        self.fake.current_model = "new-model"
        # Stop with a changed model before the periodic checker can see it.
        with avatar._state_lock:
            avatar._stop.set()
        avatar._thread.join(timeout=0.2)
        before = len(self.fake.mouth_values())
        avatar.close()
        self.assertEqual(before, len(self.fake.mouth_values()))

    def test_request_id_matching_ignores_unrelated_replies(self):
        self.fake.unmatched_reply = True
        avatar = self.avatar(model_id="")
        avatar.connect(allow_authorize=True)
        self.assertEqual(1, len(avatar.models()))
        self.assertTrue(all(0 < len(item["requestID"]) <= 64 for item in self.fake.sent))

    def test_timeout_and_malformed_response_are_clear_errors(self):
        self.fake.drop_type = "AuthenticationRequest"
        with self.assertRaisesRegex(ProviderError, "in time"):
            self.avatar(model_id="").connect(allow_authorize=True)
        self.fake = FakeVTS()
        self.fake.malformed = True
        with self.assertRaisesRegex(ProviderError, "invalid response"):
            self.avatar(model_id="").connect(allow_authorize=True)

    def test_token_save_is_atomic_on_replace_failure(self):
        avatar = self.avatar(model_id="")
        old = "original-valid-test-token"
        avatar._save_token(old)
        with patch("public_host.avatar.os.replace", side_effect=OSError("not allowed")):
            with self.assertRaisesRegex(ProviderError, "Cannot save"):
                avatar._save_token("replacement-test-token")
        self.assertEqual(old, avatar._load_token())
        self.assertEqual([], list(Path(self.temp.name).glob("*.tmp")))

    def test_invalid_values_and_credential_urls_are_rejected(self):
        for url in ("file:///token", "ws://user:secret@localhost:8001", "ws://localhost:8001?token=secret"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                self.avatar(url=url)
        avatar = self.avatar()
        for value in (float("nan"), float("inf"), "bad"):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                avatar.set_mouth_level(value)

    def test_missing_optional_dependency_has_actionable_message(self):
        original_import = builtins.__import__

        def missing_websockets(name, *args, **kwargs):
            if name == "websockets.sync.client":
                raise ImportError("not installed")
            return original_import(name, *args, **kwargs)

        avatar = VTSAvatar(self.config, self.temp.name)
        self.addCleanup(avatar.close)
        with patch("builtins.__import__", side_effect=missing_websockets):
            with self.assertRaisesRegex(ProviderError, "websockets >= 12"):
                avatar.connect(allow_authorize=True)

    def test_explicit_reauthorization_can_replace_a_malformed_local_token(self):
        avatar = self.avatar(model_id="")
        avatar._token_path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ProviderError, "Cannot read"):
            avatar.connect()
        self.assertEqual([], self.fake.sent)
        avatar.connect(allow_authorize=True)
        self.assertTrue(avatar.connected)
        self.assertIsInstance(avatar._load_token(), str)


if __name__ == "__main__":
    unittest.main()
