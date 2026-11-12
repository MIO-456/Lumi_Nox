"""Portable voice contracts; SDKs and audio devices are replaced, never contacted."""
import base64
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def forbidden_io(*args, **kwargs):
    raise AssertionError("This test must not call a real SDK or audio processor")


def module(name, **values):
    result = types.ModuleType(name)
    result.__dict__.update(values)
    return result


@contextmanager
def voice_modules():
    """Restore sys.modules on exit so these fakes cannot leak to other suites."""
    sdk = types.SimpleNamespace(call=forbidden_io)
    stubs = {
        "numpy": module("numpy"),
        "pyaudiowpatch": module("pyaudiowpatch", paInt16=8, paContinue=0, paComplete=1),
        "dashscope": module("dashscope", api_key="offline-test"),
        "dashscope.audio": module("dashscope.audio"),
        "dashscope.audio.qwen_tts": module("dashscope.audio.qwen_tts", SpeechSynthesizer=sdk),
        "dashscope.audio.tts_v2": module(
            "dashscope.audio.tts_v2", SpeechSynthesizer=forbidden_io,
            ResultCallback=object,
            AudioFormat=types.SimpleNamespace(PCM_24000HZ_MONO_16BIT="pcm24"),
        ),
        "scipy": module("scipy"),
        "scipy.signal": module("scipy.signal", resample_poly=forbidden_io),
        "dotenv": module("dotenv", load_dotenv=lambda: None),
        "openai": module("openai", OpenAI=lambda **kwargs: types.SimpleNamespace(**kwargs)),
    }
    names = ["role_volume", "spoken_output", "speech_pauses", "tts_emitter",
             "qwen3_tts", "qwen_audio_tts", "cosyvoice_tts", "fast_brain"]
    with patch.dict(sys.modules, stubs), patch.dict(os.environ, {
        "ARK_API_KEY_FAST": "offline-test", "DASHSCOPE_API_KEY": "offline-test",
        "QWEN_AUDIO_TTS_WEBSOCKET_URL": "wss://tts.example.test/inference",
    }):
        loaded = {}
        for name in names:
            spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
            loaded[name] = importlib.util.module_from_spec(spec)
            sys.modules[name] = loaded[name]
            spec.loader.exec_module(loaded[name])
        with tempfile.TemporaryDirectory() as folder:
            volume = loaded["role_volume"]
            volume._STORE = volume.RoleVolumeStore(Path(folder) / "volumes.json")
            yield types.SimpleNamespace(**loaded)


class FakeStream:
    def __init__(self, fail=False):
        self.writes = []
        self.closed = False
        self.fail = fail

    def write(self, data, **kwargs):
        if self.fail:
            raise OSError(-9999, "offline device failure")
        self.writes.append(data)

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class FakePA:
    def __init__(self, fail_write=False):
        self.streams = []
        self.fail_write = fail_write

    def open(self, **kwargs):
        stream = FakeStream(self.fail_write)
        self.streams.append(stream)
        return stream


class ManualTimer:
    """Run timeout callbacks explicitly, without time-dependent assertions."""
    instances = []

    def __init__(self, interval, callback):
        self.callback = callback
        self.cancelled = False
        self.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class FakeCosyEngine:
    def __init__(self, *, connect_timeout=False, sends_audio=False):
        self.calls = []
        self.callback = None
        self.connect_timeout = connect_timeout
        self.sends_audio = sends_audio
        self.cancelled = False
        self.completed = False

    def streaming_call(self, text):
        self.calls.append(text)
        if self.connect_timeout:
            raise TimeoutError("offline connection timeout")
        if self.sends_audio:
            self.callback.on_data(b"\x01\x00")

    def streaming_complete(self):
        self.completed = True

    def streaming_cancel(self):
        self.cancelled = True


def chunk(pcm, *, status=200):
    return types.SimpleNamespace(
        status_code=status, request_id="offline-request", message="offline failure",
        output={"audio": {"data": base64.b64encode(pcm).decode("ascii")}},
    )


class VoiceRefreshTests(unittest.TestCase):
    def setUp(self):
        context = voice_modules()
        self.mods = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.qwen = self.mods.qwen3_tts
        self.qwen.init(pa_instance=FakePA(), log_fn=lambda _: None)

    def request_pcm(self, **kwargs):
        return self.qwen.synthesize_sentence_pcm(
            text="A complete sentence.", voice_id="test-voice", model="qwen-test", **kwargs,
        )

    def test_complete_pcm_waits_for_all_chunks_and_closes_transport(self):
        closed = []

        def responses():
            try:
                yield chunk(b"\x01\x00")
                yield chunk(b"\x02\x00")
            finally:
                closed.append(True)

        with patch.object(self.qwen, "_stream_synthesis", return_value=responses()):
            result = self.request_pcm()
        self.assertEqual(result.pcm, b"\x01\x00\x02\x00")
        self.assertEqual((result.sample_rate, result.attempts), (24000, 1))
        self.assertEqual(closed, [True])

    def test_failed_request_discards_partial_audio_before_retry(self):
        attempts = [iter([chunk(b"\x01\x00"), chunk(b"", status=500)]),
                    iter([chunk(b"\x02\x00")])]
        with patch.object(self.qwen, "_stream_synthesis", side_effect=attempts):
            result = self.request_pcm()
        self.assertEqual(result.pcm, b"\x02\x00")
        self.assertEqual(result.attempts, 2)

    def test_invalid_or_empty_pcm_exhausts_only_the_retry_budget(self):
        for responses in [[], [chunk(b"\x01")]]:
            with self.subTest(responses=responses):
                with patch.object(self.qwen, "_stream_synthesis", side_effect=lambda **_: iter(responses)) as call:
                    with self.assertRaises(self.qwen.SentenceSynthesisError) as raised:
                        self.request_pcm()
                self.assertEqual(call.call_count, self.qwen.MAX_ATTEMPTS)
                self.assertEqual(raised.exception.attempts, self.qwen.MAX_ATTEMPTS)

    def test_cancelled_sentence_closes_transport_without_retry(self):
        closed = []

        def responses():
            try:
                yield chunk(b"\x01\x00")
            finally:
                closed.append(True)

        with patch.object(self.qwen, "_stream_synthesis", return_value=responses()) as call:
            with self.assertRaises(InterruptedError):
                self.request_pcm(is_active=lambda: False)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(closed, [True])

    def test_qwen_audio_family_routes_to_separate_adapter(self):
        with patch.object(self.mods.qwen_audio_tts, "stream_synthesis", return_value="adapter") as call:
            result = self.qwen._stream_synthesis(model="qwen-audio-test", voice="v", text="hello", timeout=2)
        self.assertEqual(result, "adapter")
        call.assert_called_once_with(model="qwen-audio-test", voice="v", text="hello", timeout=2)

    def test_qwen_playback_preserves_sentence_order_and_reports_completion(self):
        pa = FakePA()
        self.qwen.init(pa_instance=pa)
        payloads = {"first": b"\x01\x00", "second": b"\x02\x00"}
        with patch.object(self.qwen, "_stream_synthesis", side_effect=lambda **kw: iter([chunk(payloads[kw["text"]])])):
            synth = self.qwen.IndependentSynth()
            synth.open("test-voice", "qwen-test", None)
            try:
                synth.feed("first")
                synth.feed("second")
                synth.finish()
            finally:
                synth.abort()
        self.assertEqual(pa.streams[0].writes, list(payloads.values()))
        self.assertEqual(synth.outcome, "complete")
        self.assertTrue(pa.streams[0].closed)

    def test_device_failure_is_not_reported_as_complete(self):
        pa = FakePA(fail_write=True)
        self.qwen.init(pa_instance=pa)
        with patch.object(self.qwen, "_stream_synthesis", side_effect=lambda **_: iter([chunk(b"\x01\x00")])):
            synth = self.qwen.IndependentSynth()
            synth.open("test-voice", "qwen-test", None)
            try:
                synth.feed("hello")
                synth.finish()
            finally:
                synth.abort()
        self.assertEqual(synth.outcome, "aborted")
        self.assertEqual(synth.failed_sentences, (1,))
        self.assertEqual(synth._audio_chunk_count, 0)
        self.assertTrue(pa.streams[0].closed)

    def test_sentence_guard_can_reject_before_start_callback_or_device_write(self):
        pa = FakePA()
        self.qwen.init(pa_instance=pa)
        guard, started = Mock(return_value=False), Mock()
        with patch.object(self.qwen, "_stream_synthesis", side_effect=lambda **_: iter([chunk(b"\x01\x00")])):
            synth = self.qwen.IndependentSynth()
            synth.open("test-voice", "qwen-test", None)
            try:
                synth.feed("hello", before_audio=guard, on_audio_start=started)
                synth.finish()
            finally:
                synth.abort()
        guard.assert_called_once()
        started.assert_not_called()
        self.assertEqual(pa.streams[0].writes, [])
        self.assertEqual(synth.outcome, "aborted")

    def test_cancel_predicate_also_applies_to_default_sentence_mode(self):
        pa = FakePA()
        self.qwen.init(pa_instance=pa)
        with patch.object(self.qwen, "_stream_synthesis", side_effect=forbidden_io) as request:
            synth = self.qwen.IndependentSynth(is_cancelled=lambda: True)
            synth.open("test-voice", "qwen-test", None)
            try:
                synth.feed("must not be synthesized")
                synth.finish()
            finally:
                synth.abort()
        request.assert_not_called()
        self.assertEqual(pa.streams[0].writes, [])
        self.assertEqual(synth.outcome, "aborted")

    def test_custom_synthesizer_failure_is_visible_and_later_sentences_continue(self):
        pa = FakePA()
        self.qwen.init(pa_instance=pa)

        def synthesize(**kwargs):
            if kwargs["text"] == "bad":
                raise RuntimeError("offline custom synthesis failure")
            return self.qwen.SentencePcm(pcm=b"\x01\x00", sample_rate=24000, attempts=1)

        synth = self.qwen.IndependentSynth(pcm_synthesizer=synthesize)
        synth.open("test-voice", "qwen-test", None)
        try:
            for sentence in ["first", "bad", "last"]:
                synth.feed(sentence)
            synth.finish()
        finally:
            synth.abort()
        self.assertEqual(pa.streams[0].writes, [b"\x01\x00", b"\x01\x00"])
        self.assertEqual(synth.outcome, "partial")
        self.assertEqual(synth.failed_sentences, (2,))

    def test_callback_output_can_fall_back_to_default_device(self):
        stream = FakeStream()
        fake = types.SimpleNamespace(open=Mock(side_effect=[OSError("missing device"), stream]))
        self.qwen.init(pa_instance=fake)
        callback = lambda *args: None
        result = self.qwen._open_output_stream(999, label="test", allow_default_fallback=True,
                                               stream_callback=callback)
        self.assertIs(result, stream)
        kwargs = fake.open.call_args.kwargs
        self.assertNotIn("output_device_index", kwargs)
        self.assertIs(kwargs["stream_callback"], callback)
        self.assertEqual(kwargs["frames_per_buffer"], self.qwen.TURN_PLAYBACK_FRAMES_PER_BUFFER)

    def test_qwen_audio_requires_explicit_endpoint_before_sdk_construction(self):
        with patch.dict(os.environ, {"QWEN_AUDIO_TTS_WEBSOCKET_URL": ""}):
            with self.assertRaisesRegex(ValueError, "QWEN_AUDIO_TTS_WEBSOCKET_URL"):
                list(self.mods.qwen_audio_tts.stream_synthesis(model="m", voice="v", text="hello", timeout=1))

    def test_qwen_audio_verifies_terminal_event_and_closes_before_returning(self):
        for event, pcm, succeeds in [("task-finished", b"\x01\x00", True),
                                     ("task-failed", b"\x01\x00", False),
                                     ("task-finished", b"\x01", False),
                                     ("task-finished", b"", False)]:
            with self.subTest(event=event, pcm=pcm):
                engine = types.SimpleNamespace(
                    call=Mock(return_value=pcm), get_last_request_id=lambda: "request",
                    get_response=lambda: {"header": {"event": event}},
                    close=Mock(), streaming_cancel=Mock(),
                )
                with patch.object(self.mods.qwen_audio_tts, "SpeechSynthesizer", return_value=engine) as factory:
                    stream = self.mods.qwen_audio_tts.stream_synthesis(model="m", voice="v", text="hello", timeout=1)
                    if succeeds:
                        result = next(stream)
                        self.assertEqual(base64.b64decode(result.output["audio"]["data"]), pcm)
                        engine.streaming_cancel.assert_not_called()
                    else:
                        with self.assertRaises(RuntimeError):
                            next(stream)
                        engine.streaming_cancel.assert_called_once()
                    engine.close.assert_called_once()
                    self.assertEqual(factory.call_args.kwargs["url"], "wss://tts.example.test/inference")

    def test_qwen_audio_cleanup_keeps_original_timeout(self):
        engine = types.SimpleNamespace(call=Mock(side_effect=TimeoutError("offline timeout")),
                                       close=Mock(side_effect=RuntimeError("close failed")),
                                       streaming_cancel=Mock(side_effect=RuntimeError("cancel failed")))
        with patch.object(self.mods.qwen_audio_tts, "SpeechSynthesizer", return_value=engine):
            with self.assertRaisesRegex(TimeoutError, "offline timeout"):
                list(self.mods.qwen_audio_tts.stream_synthesis(model="m", voice="v", text="hello", timeout=1))
        engine.close.assert_called_once()
        engine.streaming_cancel.assert_called_once()

    def test_role_volume_validates_and_persists_independent_speakers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "volumes.json"
            store = self.mods.role_volume.RoleVolumeStore(path)
            self.assertEqual(store.set_volume("Lumi", 2, persist=False), 2)
            self.assertEqual(store.get_volume("Nox"), 1)
            for value in [True, "1", float("nan"), float("inf")]:
                with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                    store.set_volume("Lumi", value, persist=False)
            with self.assertRaises(ValueError):
                store.get_volume("unknown")
            store.flush_pending()
            self.assertEqual(json.loads(path.read_text()), {"Lumi": 2, "Nox": 1})
            self.assertEqual(self.mods.role_volume.RoleVolumeStore(path).get_volume("Lumi"), 2)

    def test_emitter_keeps_legacy_calls_and_forwards_callbacks(self):
        synth = types.SimpleNamespace(open=Mock(), feed=Mock(), finish=Mock(), abort=Mock(),
                                      request_abort=Mock(), outcome="partial", failed_sentences=(2,))
        emitter = self.mods.tts_emitter.IndependentTTSEmitter(synth, "v", "m", 3)
        emitter.feed("first")
        callback = lambda: True
        emitter.feed("second", on_audio_start=callback)
        synth.open.assert_called_once_with("v", "m", 3)
        self.assertEqual(synth.feed.call_args_list[0].args, ("first",))
        self.assertEqual(synth.feed.call_args_list[0].kwargs, {})
        self.assertIs(synth.feed.call_args.kwargs["on_audio_start"], callback)
        emitter.request_abort()
        synth.request_abort.assert_called_once()
        self.assertEqual((emitter.outcome, emitter.failed_sentences), ("partial", (2,)))

    def test_emitter_keyword_open_supports_validated_pause_offsets(self):
        synth = types.SimpleNamespace(open=Mock(), feed=Mock(), supports_pause_tags=True)
        kwargs = dict(voice_id="v", model="qwen-audio-3.1-test", cable_index=None)
        emitter = self.mods.tts_emitter.IndependentTTSEmitter(synth, open_kwargs=kwargs)
        emitter.feed("hello", pause_offsets=[2])
        synth.open.assert_called_once_with(**kwargs)
        self.assertEqual(synth.feed.call_args.kwargs["pause_offsets"], [2])

    @contextmanager
    def cosy_session(self, engines):
        cosy = self.mods.cosyvoice_tts
        pa = FakePA()
        cosy.init(pa_instance=pa, log_fn=lambda _: None)
        pending = iter(engines)

        def factory(model, voice, callback):
            engine = next(pending)
            engine.callback = callback
            return engine

        ManualTimer.instances = []
        with patch.object(cosy, "_make_synthesizer", side_effect=factory), patch.object(cosy.threading, "Timer", ManualTimer):
            synth = cosy.IndependentSynth(first_frame_timeout=1, max_retries=1)
            synth.open("test-voice", "cosy-test", None)
            try:
                yield synth, pa
            finally:
                synth.abort()

    def test_cosy_timeout_replays_in_order_then_finishes_without_sdk_wait(self):
        first, second = FakeCosyEngine(), FakeCosyEngine()
        with self.cosy_session([first, second]) as (synth, pa):
            synth.feed("first")
            synth.feed("second")
            ManualTimer.instances[-1].fire()
            self.assertTrue(first.cancelled)
            self.assertEqual(second.calls, ["first", "second"])
            ManualTimer.instances[-1].fire()
            synth.finish()
            self.assertEqual(synth._retry_count, 1)
            self.assertTrue(synth._gave_up)
            self.assertTrue(second.cancelled)
            self.assertFalse(first.completed or second.completed)
            self.assertTrue(pa.streams[0].closed)

    def test_cosy_connection_timeout_recovers_and_delivers_audio(self):
        first = FakeCosyEngine(connect_timeout=True)
        second = FakeCosyEngine(sends_audio=True)
        with self.cosy_session([first, second]) as (synth, pa):
            synth.feed("hello")
            synth.finish()
            self.assertTrue(first.cancelled)
            self.assertTrue(second.completed)
            self.assertEqual(second.calls, ["hello"])
            self.assertEqual(pa.streams[0].writes, [b"\x01\x00"])
            self.assertTrue(pa.streams[0].closed)

    def test_cosy_abort_cancels_pending_retry(self):
        engine = FakeCosyEngine()
        with self.cosy_session([engine]) as (synth, pa):
            synth.feed("hello")
            timer = ManualTimer.instances[-1]
            synth.abort()
            timer.fire()
            self.assertTrue(timer.cancelled)
            self.assertTrue(engine.cancelled)
            self.assertEqual(synth._retry_count, 0)
            self.assertTrue(pa.streams[0].closed)

    def test_pause_controls_are_buffered_and_plain_text_is_preserved(self):
        pauses = self.mods.speech_pauses
        plain = "First sentence. Second sentence."
        raw = "First sentence." + pauses.TAG + " Second sentence."
        for cut in range(len(raw) + 1):
            buffered = pauses.BufferedPauseOutput("Lumi")
            self.assertEqual(buffered.feed(raw[:cut]), "")
            self.assertEqual(buffered.feed(raw[cut:]), "")
            result = buffered.finish()
            self.assertTrue(result["ok"])
            self.assertEqual(result["plain"], plain)
            self.assertEqual("".join(ET.fromstring(result["ssml"]).itertext()), plain)

    def test_malformed_pause_and_incomplete_response_release_no_text(self):
        pauses = self.mods.speech_pauses
        for tag in ['<break time="40', '<break time="400ms" />', '&lt;break time="400ms"/&gt;', pauses.MARKER]:
            with self.subTest(tag=tag):
                result = pauses.prepare_output("Hello." + tag + "World.", "Lumi")
                self.assertFalse(result["ok"])
                self.assertEqual(result["plain"], "")
        for reason in ["length", "content_filter", "error", None]:
            result = pauses.prepare_output("Hello.", "Lumi", finish_reason=reason)
            self.assertFalse(result["ok"])

    def test_character_model_uses_tool_fallback_without_changing_selection(self):
        brain = self.mods.fast_brain
        brain.set_llm_model("character-260628")
        self.assertFalse(brain.current_supports_tools())
        self.assertEqual(brain.resolve_call_target()[1], "doubao-seed-character-260628")
        _, model_id, params = brain.resolve_call_target(needs_tools=True)
        self.assertEqual(model_id, brain.LLM_MODELS[brain.TOOL_FALLBACK_MODEL_KEY][2])
        self.assertEqual(params["extra_body"], {"reasoning_effort": "minimal"})
        self.assertEqual(brain.resolve_call_target()[1], "doubao-seed-character-260628")


if __name__ == "__main__":
    unittest.main()
