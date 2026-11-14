"""Real local HTTP and device-boundary checks, without cloud keys or audio hardware."""
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
import wave

from public_host.config import ConfigStore, default_config, runtime_config, validate_config
from public_host.providers import AudioClip, Cancelled, CompatibleChat, CompatibleSpeech, ProviderError
from public_host.playback import AudioPlayer


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x01\x00" * 240)
    return output.getvalue()


class ServiceHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, payload, self.headers.get("Authorization")))
        if self.server.status == 302:
            self.send_response(302)
            self.send_header("Location", "/stolen-key")
            self.end_headers()
            return
        body = self.server.body
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ServiceHandler)
        self.server.status, self.server.requests = 200, []
        self.server.body = json.dumps({"choices": [{"message": {"content": "你好"}}]}).encode()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {"base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
                       "api_key": "unit-test-key", "model": "user-selected-model", "timeout": 2}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_chat_uses_user_service_and_protocol(self):
        self.config.update(token_limit_field="max_completion_tokens", max_tokens=32)
        result = CompatibleChat(self.config).complete([{"role": "user", "content": "hi"}], threading.Event())
        self.assertEqual(result, "你好")
        path, payload, authorization = self.server.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(payload["model"], "user-selected-model")
        self.assertEqual(payload["max_completion_tokens"], 32)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(authorization, "Bearer unit-test-key")

    def test_speech_requests_wav_and_checks_real_frames(self):
        self.server.body = wav_bytes()
        clip = CompatibleSpeech(self.config).synthesize("hi", "user-voice", threading.Event())
        self.assertEqual((clip.sample_rate, len(clip.pcm)), (24000, 480))
        path, payload, _ = self.server.requests[0]
        self.assertEqual(path, "/v1/audio/speech")
        self.assertEqual((payload["voice"], payload["response_format"]), ("user-voice", "wav"))

    def test_missing_audio_and_raw_error_are_not_success_or_disclosed(self):
        self.server.body = b"provider-body-containing-unit-test-key"
        with self.assertRaises(ProviderError) as error:
            CompatibleSpeech(self.config).synthesize("hi", "voice", threading.Event())
        self.assertNotIn("unit-test-key", str(error.exception))
        self.server.status = 401
        with self.assertRaises(ProviderError) as error:
            CompatibleChat(self.config).complete([], threading.Event())
        self.assertIn("401", str(error.exception))
        self.assertNotIn("unit-test-key", str(error.exception))

    def test_redirect_does_not_forward_credentials(self):
        self.server.status = 302
        with self.assertRaises(ProviderError):
            CompatibleChat(self.config).complete([], threading.Event())
        self.assertEqual(len(self.server.requests), 1)

    def test_cancelled_request_does_not_contact_server(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            CompatibleChat(self.config).complete([], cancel)
        self.assertFalse(self.server.requests)


class ConfigTests(unittest.TestCase):
    def test_secrets_are_preserved_replaced_and_explicitly_cleared(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(folder)
            data = default_config()
            data["llm"]["api_key"] = "unit-test-key"
            public = store.save(data)
            self.assertNotIn("unit-test-key", json.dumps(public))
            public["llm"]["api_key"] = ""
            store.save(public)
            self.assertEqual(store.load()["llm"]["api_key"], "unit-test-key")
            public["llm"]["api_key"] = "replacement"
            store.save(public)
            self.assertEqual(store.load()["llm"]["api_key"], "replacement")
            public["llm"]["clear_api_key"] = True
            store.save(public)
            self.assertEqual(store.load()["llm"]["api_key"], "")

    def test_live_rejects_unconfigured_service_but_demo_needs_no_keys(self):
        data = default_config()
        with self.assertRaises(ProviderError):
            runtime_config(data)
        data["mode"] = "demo"
        data["roles"][0]["avatar"]["enabled"] = True
        self.assertEqual(len(runtime_config(data)["roles"]), 1)
        self.assertFalse(runtime_config(data)["roles"][0]["avatar"]["enabled"])

    def test_changing_endpoint_does_not_send_old_saved_key_to_new_service(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(folder)
            data = default_config()
            data["llm"].update(base_url="https://first.example.test/v1", api_key="first-service-key")
            public = store.save(data)
            public["llm"]["base_url"] = "https://second.example.test/v1"
            store.save(public)
            self.assertEqual(store.load()["llm"]["api_key"], "")

    def test_no_parent_configuration_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / ".env").write_text("API_KEY=do-not-load", encoding="utf-8")
            store = ConfigStore(Path(folder) / "child")
            self.assertEqual(store.load(), default_config())
            self.assertFalse(store.directory.exists())

    def test_duplicate_vts_endpoint_and_credential_in_url_rejected(self):
        data = default_config()
        for role in data["roles"]:
            role["enabled"] = True
            role["avatar"]["enabled"] = True
            role["avatar"]["url"] = "ws://127.0.0.1:8001"
        with self.assertRaises(ProviderError):
            validate_config(data)
        data = default_config()
        data["llm"]["base_url"] = "https://example.test/v1?key=must-not-leak"
        with self.assertRaises(ProviderError):
            validate_config(data)


class ProviderBoundaryTests(unittest.TestCase):
    def test_eof_after_total_deadline_is_not_a_successful_chat_response(self):
        clock = [0.0]
        chunks = iter([
            (b'{"choices":[{"message":{"content":"valid reply"}}]}', 1.0),
            (b"", 121.0),
        ])

        def read(_size):
            data, clock[0] = next(chunks)
            return data

        response = MagicMock()
        response.__enter__.return_value = response
        response.read1.side_effect = read
        opener = Mock()
        opener.open.return_value = response
        config = {"base_url": "https://service.invalid/v1", "model": "example", "timeout": 120}
        with patch("public_host.providers.build_opener", return_value=opener), \
                patch("public_host.providers.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(ProviderError) as error:
                CompatibleChat(config).complete([], threading.Event())
        self.assertIn("超时", str(error.exception))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 120)

    @staticmethod
    def pcm_wave(data, *, channels=1, extra=b""):
        def little(value, size):
            return value.to_bytes(size, "little")

        frame_size = channels * 2
        fmt = (little(1, 2) + little(channels, 2) + little(24000, 4)
               + little(24000 * frame_size, 4) + little(frame_size, 2) + little(16, 2))
        chunks = (extra + b"fmt " + little(len(fmt), 4) + fmt
                  + b"data" + little(len(data), 4) + data + (b"\0" if len(data) % 2 else b""))
        return b"RIFF" + little(len(chunks) + 4, 4) + b"WAVE" + chunks

    def test_wav_partial_sample_frame_is_rejected_instead_of_truncated(self):
        for channels, data in ((1, b"\x01\x00\x02"), (2, b"\x01\x00" * 3)):
            with self.subTest(channels=channels), \
                    patch("public_host.providers.post_json", return_value=self.pcm_wave(data, channels=channels)):
                with self.assertRaises(ProviderError):
                    CompatibleSpeech({"model": "example"}).synthesize("hi", "voice", threading.Event())

    def test_complete_pcm_wav_allows_padded_unknown_metadata_chunk(self):
        pcm = b"\x01\x00" * 16
        junk = b"JUNK" + (1).to_bytes(4, "little") + b"x\0"
        raw = self.pcm_wave(pcm, extra=junk)
        with patch("public_host.providers.post_json", return_value=raw):
            clip = CompatibleSpeech({"model": "example"}).synthesize("hi", "voice", threading.Event())
        self.assertEqual(clip.pcm, pcm)
        self.assertEqual((clip.sample_rate, clip.channels, clip.sample_width), (24000, 1, 2))


class FakeDevice:
    class CallbackStop(Exception):
        pass

    class CallbackAbort(Exception):
        pass

    def __init__(self):
        self.wrote = threading.Event()
        self.drained = threading.Event()

    def RawOutputStream(self, **kwargs):
        owner = self

        class Stream:
            active = True

            def start(self):
                def work():
                    try:
                        kwargs["callback"](bytearray(32), 16, None, None)
                    except owner.CallbackStop:
                        owner.wrote.set()
                    except owner.CallbackAbort:
                        self.active = False
                        kwargs["finished_callback"]()
                        return
                    owner.drained.wait(2)
                    kwargs["finished_callback"]()
                    self.active = False
                threading.Thread(target=work, daemon=True).start()

            def abort(self):
                owner.drained.set()
                self.active = False

            def stop(self):
                pass

            def close(self):
                owner.drained.set()

        return Stream()


class PlaybackTests(unittest.TestCase):
    def test_returns_only_after_device_drain(self):
        backend, result = FakeDevice(), []
        thread = threading.Thread(target=lambda: result.append(AudioPlayer(backend=backend).play(
            AudioClip(b"\x01\x00" * 16, 8000), threading.Event())), daemon=True)
        thread.start()
        self.assertTrue(backend.wrote.wait(1))
        self.assertEqual(result, [])
        backend.drained.set()
        thread.join(1)
        self.assertEqual(result, ["complete"])

    def test_empty_pcm_is_not_played(self):
        with self.assertRaises(ProviderError):
            AudioPlayer(backend=FakeDevice()).play(AudioClip(b"", 8000), threading.Event())


if __name__ == "__main__":
    unittest.main()
