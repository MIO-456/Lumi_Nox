"""Qwen 独立合成器：旧 Qwen3 HTTP SSE / Qwen-Audio tts_v2，共用播放链路。"""

import base64
import math
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from threading import Event, Lock, RLock
from typing import Callable

import dashscope
import numpy as np
import pyaudiowpatch as pyaudio
from dashscope.audio.qwen_tts import SpeechSynthesizer
from dotenv import load_dotenv
from scipy.signal import resample_poly

import role_volume


REQUEST_TIMEOUT_SECONDS = 20
FINISH_TIMEOUT_SECONDS = 45
MAX_TURN_AUDIO_SECONDS = 180.0
TURN_PLAYBACK_FRAMES_PER_BUFFER = 2048
MAX_ATTEMPTS = 2
PREFETCH_WORKERS = 2
SLOW_FIRST_CHUNK_SECONDS = 1.2
JITTER_WARNING_SECONDS = 0.28
SOURCE_SAMPLE_RATE = 24000
PLAYBACK_BUFFER_SECONDS = 0.5
PLAYBACK_FRAMES_PER_BUFFER = int(SOURCE_SAMPLE_RATE * PLAYBACK_BUFFER_SECONDS)
PLAYBACK_STALL_WARNING_SECONDS = 0.15

_AUDIO_END = object()

_pa_instance = None
_monitor_device_index = None
_ref_buffer = None
_log_fn = lambda msg: print(msg, flush=True)
_announced_monitor_routes = set()


def init(*, pa_instance, ref_buffer=None, monitor_device_index=None, log_fn=None):
    global _pa_instance, _monitor_device_index, _ref_buffer, _log_fn
    _pa_instance = pa_instance
    _ref_buffer = ref_buffer
    _monitor_device_index = monitor_device_index
    if log_fn:
        _log_fn = log_fn
    load_dotenv()
    if not getattr(dashscope, "api_key", None):
        dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY")


def set_monitor_device(index):
    global _monitor_device_index
    _monitor_device_index = index


def _stream_synthesis(*, model, voice, text, timeout):
    if model.startswith("qwen-audio-"):
        from qwen_audio_tts import stream_synthesis
        return stream_synthesis(model=model, voice=voice, text=text, timeout=timeout)
    return SpeechSynthesizer.call(
        model=model,
        text=text,
        voice=voice,
        stream=True,
        timeout=timeout,
    )


class SentenceSynthesisError(RuntimeError):
    def __init__(self, message, *, attempts=MAX_ATTEMPTS, request_id=""):
        super().__init__(message)
        self.attempts = attempts
        self.request_id = request_id


@dataclass(frozen=True)
class SentencePcm:
    pcm: bytes
    sample_rate: int
    attempts: int


@dataclass(frozen=True)
class _MonitorRoute:
    device_index: int
    sample_rate: int
    channels: int
    uses_wasapi: bool
    device_name: str


def _stream_open_args(
    device_index,
    *,
    sample_rate=SOURCE_SAMPLE_RATE,
    channels=1,
):
    args = dict(
        format=pyaudio.paInt16,
        channels=channels,
        rate=sample_rate,
        output=True,
        frames_per_buffer=int(sample_rate * PLAYBACK_BUFFER_SECONDS),
    )
    if device_index is not None:
        args["output_device_index"] = device_index
    return args


def _open_output_stream(
    device_index,
    *,
    label,
    allow_default_fallback,
    sample_rate=SOURCE_SAMPLE_RATE,
    channels=1,
    stream_callback=None,
):
    args = _stream_open_args(
        device_index,
        sample_rate=sample_rate,
        channels=channels,
    )
    callback_args = ({"stream_callback": stream_callback, "start": False,
                      "frames_per_buffer": TURN_PLAYBACK_FRAMES_PER_BUFFER}
                     if stream_callback is not None else {})
    args.update(callback_args)
    try:
        return _pa_instance.open(**args)
    except Exception as exc:
        if allow_default_fallback and device_index is not None:
            _log_fn(
                f"[Qwen3-TTS·{label}] 声卡 index={device_index} "
                f"不可用({exc})，回退默认输出"
            )
            fallback_args = _stream_open_args(
                None, sample_rate=sample_rate, channels=channels,
            )
            fallback_args.update(callback_args)
            return _pa_instance.open(**fallback_args)
        raise


def _normalize_device_name(name):
    return " ".join(str(name or "").casefold().split())


def _device_names_match(selected_name, candidate_name):
    selected = _normalize_device_name(selected_name)
    candidate = _normalize_device_name(candidate_name)
    if not selected or not candidate:
        return False
    if selected == candidate:
        return True
    # Windows MME 经常把设备名截断为约 31 个字符，而 WASAPI 保留完整名称。
    shorter, longer = sorted((selected, candidate), key=len)
    return len(shorter) >= 8 and longer.startswith(shorter)


def _resolve_monitor_route(device_index):
    fallback = _MonitorRoute(
        device_index=device_index,
        sample_rate=SOURCE_SAMPLE_RATE,
        channels=1,
        uses_wasapi=False,
        device_name="",
    )
    try:
        selected = _pa_instance.get_device_info_by_index(device_index)
        fallback = _MonitorRoute(
            device_index=device_index,
            sample_rate=SOURCE_SAMPLE_RATE,
            channels=1,
            uses_wasapi=False,
            device_name=str(selected.get("name", "")),
        )
        wasapi_host_index = None
        wasapi_host_info = None
        for host_index in range(_pa_instance.get_host_api_count()):
            host_info = _pa_instance.get_host_api_info_by_index(host_index)
            if "wasapi" in str(host_info.get("name", "")).casefold():
                wasapi_host_index = host_index
                wasapi_host_info = host_info
                break
        if wasapi_host_index is None:
            return fallback

        selected_name = selected.get("name", "")
        candidates = []
        for candidate_index in range(_pa_instance.get_device_count()):
            candidate = _pa_instance.get_device_info_by_index(candidate_index)
            if int(candidate.get("hostApi", -1)) != wasapi_host_index:
                continue
            if int(candidate.get("maxOutputChannels", 0)) <= 0:
                continue
            if bool(candidate.get("isLoopbackDevice", False)):
                continue
            if _device_names_match(selected_name, candidate.get("name", "")):
                exact = (
                    _normalize_device_name(selected_name)
                    == _normalize_device_name(candidate.get("name", ""))
                )
                candidates.append((not exact, candidate_index, candidate))

        # 如果 monitor 是系统默认 MME 输出，允许使用 WASAPI 的默认输出兜底。
        if not candidates:
            try:
                default_selected = _pa_instance.get_default_output_device_info()
                default_wasapi_index = int(
                    (wasapi_host_info or {}).get("defaultOutputDevice", -1)
                )
                if (
                    int(default_selected.get("index", -1)) == int(device_index)
                    and default_wasapi_index >= 0
                ):
                    candidate = _pa_instance.get_device_info_by_index(
                        default_wasapi_index
                    )
                    if (
                        int(candidate.get("maxOutputChannels", 0)) > 0
                        and not bool(candidate.get("isLoopbackDevice", False))
                    ):
                        candidates.append(
                            (False, default_wasapi_index, candidate)
                        )
            except Exception:
                pass

        if not candidates:
            return fallback
        _, matched_index, matched = sorted(candidates, key=lambda item: item[0])[0]
        max_channels = max(1, int(matched.get("maxOutputChannels", 1)))
        return _MonitorRoute(
            device_index=matched_index,
            sample_rate=max(
                1, int(round(float(matched.get("defaultSampleRate", 48000))))
            ),
            channels=2 if max_channels >= 2 else 1,
            uses_wasapi=True,
            device_name=str(matched.get("name", "")),
        )
    except Exception:
        return fallback


def _convert_pcm16_output(
    pcm,
    *,
    source_rate,
    target_rate,
    channels,
):
    if len(pcm) % 2:
        raise ValueError("invalid PCM payload")
    samples = np.frombuffer(pcm, dtype=np.int16)
    if target_rate != source_rate and len(samples):
        divisor = math.gcd(int(source_rate), int(target_rate))
        samples = resample_poly(
            samples.astype(np.float64),
            up=int(target_rate) // divisor,
            down=int(source_rate) // divisor,
        )
        samples = np.rint(np.clip(samples, -32768, 32767)).astype(np.int16)
    if channels > 1 and len(samples):
        samples = np.repeat(samples[:, None], int(channels), axis=1).reshape(-1)
    return samples.astype(np.int16, copy=False).tobytes()


def _announce_monitor_route(source_device_index, route, *, fallback_reason=""):
    route_key = (
        int(source_device_index),
        int(route.device_index),
        int(route.sample_rate),
        int(route.channels),
        bool(route.uses_wasapi),
        str(fallback_reason),
    )
    if route_key in _announced_monitor_routes:
        return
    _announced_monitor_routes.add(route_key)
    if route.uses_wasapi:
        _log_fn(
            f"[Qwen3-TTS·监听] MME[{source_device_index}] 24k/1ch"
            f" → Windows WASAPI[{route.device_index}] "
            f"{route.sample_rate / 1000:g}k/{route.channels}ch"
        )
        return
    reason = f"({fallback_reason})，" if fallback_reason else "，"
    _log_fn(
        f"[Qwen3-TTS·监听] 未使用 WASAPI{reason}"
        f"回退 MME[{route.device_index}] 24k/1ch；"
        "双输出卡顿风险仍在"
    )


def _write_stream(stream, pcm, *, label, sentence):
    try:
        stream.write(pcm, exception_on_underflow=True)
        return True
    except OSError as exc:
        error_code = next(
            (value for value in getattr(exc, "args", ()) if isinstance(value, int)),
            None,
        )
        underflow_code = getattr(pyaudio, "paOutputUnderflowed", -9980)
        event = "播放欠载" if error_code == underflow_code else "播放写入失败"
        _log_fn(
            f"[Qwen3-TTS·{event}] path={label} sentence={sentence} error={exc}"
        )
        return False


def _field(value, name):
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def synthesize_sentence_pcm(
    *,
    text: str,
    voice_id: str,
    model: str,
    is_active: Callable[[], bool] | None = None,
) -> SentencePcm:
    """请求一整句 24 kHz PCM；不打开声卡、不写监听/AEC、不触发字幕。"""
    last_error = None
    last_request_id = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        chunks = []
        responses = None
        request_id = ""
        request_started_at = time.monotonic()
        previous_audio_at = None
        try:
            responses = _stream_synthesis(
                model=model,
                voice=voice_id,
                text=text,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            for response in responses:
                if is_active is not None and not is_active():
                    raise InterruptedError("sentence is no longer active")
                request_id = getattr(response, "request_id", "") or request_id
                status = getattr(response, "status_code", HTTPStatus.OK)
                if status != HTTPStatus.OK:
                    raise RuntimeError(
                        f"HTTP {status}: {getattr(response, 'message', '')}"
                    )
                output = _field(response, "output")
                audio = _field(output, "audio")
                encoded = _field(audio, "data")
                if not encoded:
                    continue
                pcm = base64.b64decode(encoded, validate=True)
                if not chunks and pcm.startswith(b"RIFF"):
                    data_pos = pcm.find(b"data", 12)
                    pcm = pcm[data_pos + 8:] if data_pos >= 0 else pcm[44:]
                if not pcm:
                    continue
                if len(pcm) % 2:
                    raise ValueError("invalid PCM payload")
                audio_at = time.monotonic()
                if not chunks:
                    first_chunk_seconds = audio_at - request_started_at
                    if first_chunk_seconds >= SLOW_FIRST_CHUNK_SECONDS:
                        _log_fn(
                            f"[Qwen3-TTS·慢首包] delay={first_chunk_seconds:.2f}s"
                        )
                elif previous_audio_at is not None:
                    gap_seconds = audio_at - previous_audio_at
                    if gap_seconds >= JITTER_WARNING_SECONDS:
                        _log_fn(f"[Qwen3-TTS·流抖动] gap={gap_seconds:.2f}s")
                previous_audio_at = audio_at
                chunks.append(pcm)
            if not chunks:
                raise RuntimeError("empty audio stream")
            return SentencePcm(
                pcm=b"".join(chunks),
                sample_rate=SOURCE_SAMPLE_RATE,
                attempts=attempt,
            )
        except InterruptedError:
            raise
        except Exception as exc:
            last_error = exc
            last_request_id = request_id
            _log_fn(
                f"[Qwen3-TTS·请求失败] attempt={attempt}/{MAX_ATTEMPTS} "
                f"request_id={request_id or '-'} stage=stream error={exc}"
            )
            if is_active is not None and not is_active():
                raise InterruptedError("sentence is no longer active") from exc
        finally:
            close = getattr(responses, "close", None)
            if callable(close):
                close()
    raise SentenceSynthesisError(
        str(last_error or "unknown synthesis error"),
        attempts=MAX_ATTEMPTS,
        request_id=last_request_id,
    ) from last_error


@dataclass
class _SentenceJob:
    epoch: int
    sequence: int
    text: str
    synthesis_text: str = ""
    on_audio_start: object = None
    before_audio: object = None
    audio_queue: queue.Queue = field(default_factory=queue.Queue)


class _BufferedOutput:
    """PortAudio pulls from complete PCM; no producer waits or network work in callbacks."""

    def __init__(self, is_active, channels=1):
        self.is_active = is_active
        self.channels = channels
        self.pcm = b""
        self.position = 0
        self.underflows = 0
        self.first_frame = Event()

    def prepare(self, pcm):
        self.pcm = pcm
        self.position = 0
        self.underflows = 0
        self.first_frame.clear()

    def callback(self, _input, frame_count, _time_info, status):
        if not self.is_active():
            return (None, pyaudio.paAbort)
        if status & pyaudio.paOutputUnderflow:
            self.underflows += 1
        end = min(len(self.pcm), self.position + frame_count * self.channels * 2)
        block = self.pcm[self.position:end]
        self.position = end
        if block:
            self.first_frame.set()
        return (block, pyaudio.paComplete if end == len(self.pcm) else pyaudio.paContinue)


class IndependentSynth:
    """现有 Qwen 合成／播放链路；逐句有序预取，整段使用可取消的完整 PCM 缓冲。"""

    supports_pause_tags = True

    def __init__(self, speaker="Lumi", *, generation_mode="sentence", is_cancelled=None,
                 pcm_synthesizer=None, pcm_output=None, output_id=None):
        if generation_mode not in ("sentence", "turn"):
            raise ValueError(f"Unknown TTS generation mode: {generation_mode}")
        self._generation_mode = generation_mode
        self._is_cancelled = is_cancelled
        self._pcm_synthesizer = pcm_synthesizer
        self._pcm_output = pcm_output
        self._output_id = output_id or ""
        self._main_buffer = None
        self._monitor_buffer = None
        self.audio_duration_seconds = 0.0
        self._total_audio_seconds = 0.0
        role_volume.get_volume(speaker)
        self._speaker = speaker
        self._state_lock = RLock()
        self._audio_write_lock = Lock()
        self._close_lock = Lock()
        self._state = "CLOSED"
        self._epoch = 0
        self._sequence = 0
        self._voice_id = ""
        self._model = ""
        self._audio_stream = None
        self._monitor_stream = None
        self._monitor_route = None
        self._monitor_queue = queue.Queue()
        self._sentence_queue = queue.Queue()
        self._playback_queue = queue.Queue()
        self._worker_threads = []
        self._playback_thread = None
        self._monitor_thread = None
        self._jobs = []
        self._interrupted = Event()
        self._audio_chunk_count = 0
        self._failed_sentences = []
        self._outcome = "no_audio"

    def open(self, voice_id, model, cable_index):
        with self._state_lock:
            self._epoch += 1
            epoch = self._epoch
            self._state = "OPEN"
        self._voice_id = voice_id
        self._model = model
        self._sequence = 0
        self._audio_chunk_count = 0
        self._failed_sentences = []
        self._outcome = "no_audio"
        self._interrupted.clear()
        self._total_audio_seconds = 0.0
        self._sentence_queue = queue.Queue()
        self._playback_queue = queue.Queue()
        self._monitor_queue = queue.Queue()
        self._jobs = []

        if self._generation_mode == "turn" and self._pcm_output is None:
            self._main_buffer = _BufferedOutput(lambda: self._is_epoch_active(epoch))
            self._monitor_buffer = _BufferedOutput(lambda: self._is_epoch_active(epoch))
        self._audio_stream = None if self._pcm_output is not None else _open_output_stream(
            cable_index, label="主输出", allow_default_fallback=True,
            stream_callback=self._main_buffer.callback if self._main_buffer else None,
        )

        self._monitor_stream = None
        self._monitor_route = None
        if self._pcm_output is None and _monitor_device_index is not None and cable_index is not None:
            try:
                route = _resolve_monitor_route(_monitor_device_index)
                if self._monitor_buffer:
                    self._monitor_buffer.channels = route.channels
                self._monitor_stream = _open_output_stream(
                    route.device_index,
                    label="监听",
                    allow_default_fallback=False,
                    sample_rate=route.sample_rate,
                    channels=route.channels,
                    stream_callback=self._monitor_buffer.callback if self._monitor_buffer else None,
                )
                self._monitor_route = route
                _announce_monitor_route(_monitor_device_index, route)
                if self._generation_mode != "turn":
                    self._monitor_thread = threading.Thread(
                        target=self._monitor_worker, daemon=True, name="qwen3-monitor"
                    )
                    self._monitor_thread.start()
            except Exception as exc:
                self._monitor_stream = None
                self._monitor_route = None
                try:
                    if self._monitor_buffer:
                        self._monitor_buffer.channels = 1
                    self._monitor_stream = _open_output_stream(
                        _monitor_device_index,
                        label="监听",
                        allow_default_fallback=False,
                        stream_callback=self._monitor_buffer.callback if self._monitor_buffer else None,
                    )
                    self._monitor_route = _MonitorRoute(
                        device_index=_monitor_device_index,
                        sample_rate=SOURCE_SAMPLE_RATE,
                        channels=1,
                        uses_wasapi=False,
                        device_name="",
                    )
                    _announce_monitor_route(
                        _monitor_device_index,
                        self._monitor_route,
                        fallback_reason=f"WASAPI 打开失败: {exc}",
                    )
                    if self._generation_mode != "turn":
                        self._monitor_thread = threading.Thread(
                            target=self._monitor_worker,
                            daemon=True,
                            name="qwen3-monitor",
                        )
                        self._monitor_thread.start()
                except Exception as fallback_exc:
                    _log_fn(
                        f"[Qwen3-TTS·监听] 无法打开监听设备: {fallback_exc}"
                    )
                    self._monitor_stream = None
                    self._monitor_route = None

        self._playback_thread = threading.Thread(
            target=self._playback_worker, daemon=True, name="qwen3-playback"
        )
        self._playback_thread.start()
        self._worker_threads = [
            threading.Thread(
                target=self._synthesis_worker, args=(epoch,), daemon=True,
                name=f"qwen3-synthesis-{index + 1}",
            )
            # Conversion decisions must follow text order: a failed sentence
            # switches that sentence and later sentences back to the clone.
            for index in range(1 if self._pcm_synthesizer else PREFETCH_WORKERS)
        ]
        for worker in self._worker_threads:
            worker.start()

    def feed(self, text, on_audio_start=None, *, before_audio=None, pause_offsets=()):
        if not text:
            return
        from speech_pauses import render_ssml, supports_pause_tags
        if pause_offsets and not supports_pause_tags(self._model):
            raise ValueError("This Qwen model has not been enabled for pause controls")
        synthesis_text = render_ssml(text, pause_offsets)
        with self._state_lock:
            if self._state != "OPEN":
                return
            self._sequence += 1
            job = _SentenceJob(
                epoch=self._epoch,
                sequence=self._sequence,
                text=text,
                synthesis_text=synthesis_text,
                on_audio_start=on_audio_start,
                before_audio=before_audio,
            )
            self._jobs.append(job)
        # 播放顺序在 feed 时就锁定；合成可以并发完成，但绝不会按完成顺序串句。
        self._playback_queue.put(job)
        self._sentence_queue.put(job)

    def finish(self):
        with self._state_lock:
            if self._state == "CLOSED":
                return
            aborting = self._state == "ABORTING"
            if not aborting:
                self._state = "DRAINING"
        if aborting:
            # The first-PCM callback may have rejected audio before finish.
            # request_abort invalidates work, but still needs the usual cleanup.
            self.abort()
            return
        for _ in self._worker_threads:
            self._sentence_queue.put(None)
        deadline = time.monotonic() + FINISH_TIMEOUT_SECONDS
        for worker in self._worker_threads:
            if self._generation_mode == "turn" or self._pcm_synthesizer:
                while worker.is_alive() and time.monotonic() < deadline:
                    if not self._is_epoch_active(self._epoch):
                        self.abort()
                        return
                    worker.join(timeout=0.05)
            else:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in self._worker_threads):
            _log_fn(f"[Qwen3-TTS·超时] {FINISH_TIMEOUT_SECONDS}s 未完成，强制中止")
            self.abort()
            return

        if not self._is_epoch_active(self._epoch):
            self.abort()
            return
        self._playback_queue.put(None)
        playback_timeout = (MAX_TURN_AUDIO_SECONDS + 15.0
                            if self._generation_mode == "turn" else FINISH_TIMEOUT_SECONDS)
        if self._pcm_output is not None:
            # Each remote sentence has its own bounded first-audio/tail waits.
            # Keep a whole reply alive through those waits, including long chat.
            playback_timeout = max(FINISH_TIMEOUT_SECONDS,
                                   self._total_audio_seconds + 30.0 * len(self._jobs))
        self._playback_thread.join(timeout=playback_timeout)
        if self._playback_thread.is_alive():
            _log_fn("[Qwen3-TTS·播放超时] 强制中止")
            self.abort()
            return

        if self._interrupted.is_set():
            self._outcome = "aborted"
        elif self._failed_sentences:
            self._outcome = "partial" if self._audio_chunk_count else "no_audio"
        else:
            self._outcome = "complete" if self._audio_chunk_count else "no_audio"
        self._close_audio()
        with self._state_lock:
            self._state = "CLOSED"

    def request_abort(self):
        with self._state_lock:
            if self._state in ("CLOSED", "ABORTING"):
                return
            self._state = "ABORTING"
            self._epoch += 1
        self._interrupted.set()
        self._outcome = "aborted"
        for job in tuple(self._jobs):
            job.audio_queue.put(_AUDIO_END)
        self._drain_queue(self._sentence_queue)
        self._drain_queue(self._playback_queue)
        for _ in self._worker_threads:
            self._sentence_queue.put(None)
        self._playback_queue.put(None)
        if self._pcm_output is not None:
            # Cancelling an output must never block the event bus on network I/O.
            threading.Thread(target=self._interrupt_remote, daemon=True,
                             name="qwen-remote-audio-cancel").start()

    def _interrupt_remote(self):
        try:
            self._pcm_output.interrupt(output_id=self._output_id)
        except Exception as exc:
            _log_fn(f"[Qwen3-TTS·远端音频·取消音频失败] {type(exc).__name__}")

    def abort(self):
        self.request_abort()
        for worker in self._worker_threads:
            if worker is not threading.current_thread():
                worker.join(timeout=0.1 if self._generation_mode == "turn" else 1)
        if self._playback_thread and self._playback_thread is not threading.current_thread():
            self._playback_thread.join(timeout=3)
        self._close_audio()
        with self._state_lock:
            self._state = "CLOSED"

    @property
    def outcome(self):
        return self._outcome

    @property
    def failed_sentences(self):
        return tuple(sorted(self._failed_sentences))

    def _is_epoch_active(self, epoch):
        if self._is_cancelled is not None and self._is_cancelled():
            return False
        with self._state_lock:
            return epoch == self._epoch and self._state in ("OPEN", "DRAINING")

    def _synthesis_worker(self, worker_epoch):
        while True:
            job = self._sentence_queue.get()
            if job is None:
                return
            try:
                if job.epoch != worker_epoch or not self._is_epoch_active(job.epoch):
                    continue
                self._synthesize_sentence(job)
            finally:
                job.audio_queue.put(_AUDIO_END)

    def _synthesize_sentence(self, job):
        try:
            result = (self._pcm_synthesizer or synthesize_sentence_pcm)(
                text=job.synthesis_text or job.text,
                voice_id=self._voice_id,
                model=self._model,
                is_active=lambda: self._is_epoch_active(job.epoch),
            )
            if self._is_epoch_active(job.epoch):
                if self._generation_mode == "turn" and len(result.pcm) / (SOURCE_SAMPLE_RATE * 2) > MAX_TURN_AUDIO_SECONDS:
                    raise SentenceSynthesisError("整段音频超过 180 秒播放上限", attempts=result.attempts)
                # 云端已经完整返回这一句。把整句作为一个播放缓冲交给 PortAudio，
                # 避免 Python 每 200ms 醒来一次、在两次 write 之间被 GC/线程调度卡住。
                with self._state_lock:
                    self._total_audio_seconds += len(result.pcm) / (SOURCE_SAMPLE_RATE * 2)
                job.audio_queue.put(result.pcm)
        except InterruptedError:
            return
        except SentenceSynthesisError as exc:
            self._failed_sentences.append(job.sequence)
            _log_fn(
                f"[Qwen3-TTS·跳过句子] sentence={job.sequence} "
                f"{exc.attempts} 次请求均失败"
            )
        except Exception as exc:
            # Custom synthesizers may use their own exception types. A failed
            # sentence must be visible in the outcome and must not kill a worker.
            self._failed_sentences.append(job.sequence)
            _log_fn(
                f"[Qwen3-TTS·跳过句子] sentence={job.sequence} "
                f"error={type(exc).__name__}: {exc}"
            )

    def _playback_worker(self):
        while True:
            job = self._playback_queue.get()
            if job is None:
                return
            audio_started = False
            while True:
                pcm = job.audio_queue.get()
                if pcm is _AUDIO_END:
                    break
                if not self._is_epoch_active(job.epoch):
                    continue
                if self._generation_mode == "turn":
                    self.audio_duration_seconds = len(pcm) / (SOURCE_SAMPLE_RATE * 2)
                if self._pcm_output is not None:
                    # The sink owns audible-start and playback completion. Upload
                    # completion cannot trigger subtitles or advance the next turn.
                    try:
                        if job.before_audio and job.before_audio() is False:
                            self.request_abort()
                            return
                        self.audio_duration_seconds = len(pcm) / (SOURCE_SAMPLE_RATE * 2)
                        remote_started = Event()

                        def remote_audio_start():
                            if not self._is_epoch_active(job.epoch):
                                return False
                            if job.on_audio_start and job.on_audio_start() is False:
                                return False
                            remote_started.set()
                            return True

                        self._pcm_output.play_sentence(
                            role_volume.scale_pcm16(pcm, self._speaker),
                            text=job.text, output_id=self._output_id,
                            on_audio_start=remote_audio_start,
                            cancelled=lambda: not self._is_epoch_active(job.epoch),
                        )
                        if self._is_epoch_active(job.epoch):
                            if not remote_started.is_set():
                                raise RuntimeError("Remote sink returned before actual audio started")
                            self._audio_chunk_count += 1
                    except InterruptedError:
                        self.request_abort()
                        return
                    except Exception as exc:
                        self._failed_sentences.append(job.sequence)
                        _log_fn(f"[Qwen3-TTS·远端音频·播放失败] sentence={job.sequence} error={type(exc).__name__}")
                        self.request_abort()
                        return
                    continue
                if self._generation_mode != "turn" and not audio_started:
                    try:
                        may_start = job.before_audio() if job.before_audio else True
                        if may_start is not False and job.on_audio_start:
                            may_start = job.on_audio_start()
                    except Exception as exc:
                        self._failed_sentences.append(job.sequence)
                        self.request_abort()
                        try:
                            _log_fn(
                                f"[Qwen3-TTS·播前检查失败] sentence={job.sequence} "
                                f"error={type(exc).__name__}，已取消未播放音频"
                            )
                        except Exception:
                            pass
                        break
                    if may_start is False:
                        self.request_abort()
                        break
                audio_started = True
                source_pcm = role_volume.scale_pcm16(pcm, self._speaker)
                if self._generation_mode == "turn":
                    self._play_buffered_turn(job, source_pcm)
                    continue
                if self._monitor_stream and self._monitor_route:
                    try:
                        monitor_pcm = _convert_pcm16_output(
                            source_pcm,
                            source_rate=SOURCE_SAMPLE_RATE,
                            target_rate=self._monitor_route.sample_rate,
                            channels=self._monitor_route.channels,
                        )
                        # 先交给独立监听线程，让物理声卡/OBS 与嘴型 Cable 同时开始。
                        self._monitor_queue.put(
                            (job.epoch, job.sequence, monitor_pcm)
                        )
                    except Exception as exc:
                        _log_fn(
                            f"[Qwen3-TTS·监听] 格式转换失败，"
                            f"sentence={job.sequence} error={exc}"
                        )
                if _ref_buffer is not None:
                    samples = np.frombuffer(source_pcm, dtype=np.int16)
                    ref16 = resample_poly(samples, up=2, down=3).astype(np.int16)
                    _ref_buffer.extend(ref16)
                with self._audio_write_lock:
                    if not self._is_epoch_active(job.epoch) or self._audio_stream is None:
                        continue
                    write_started_at = time.monotonic()
                    wrote_audio = _write_stream(
                        self._audio_stream,
                        source_pcm,
                        label="主输出",
                        sentence=job.sequence,
                    )
                    write_seconds = time.monotonic() - write_started_at
                if not wrote_audio:
                    self._failed_sentences.append(job.sequence)
                    self.request_abort()
                    return
                self._audio_chunk_count += 1
                audio_seconds = len(source_pcm) / (SOURCE_SAMPLE_RATE * 2)
                overrun_seconds = write_seconds - audio_seconds
                if overrun_seconds >= PLAYBACK_STALL_WARNING_SECONDS:
                    _log_fn(
                        f"[Qwen3-TTS·播放阻塞] sentence={job.sequence} "
                        f"audio={audio_seconds:.2f}s write={write_seconds:.2f}s "
                        f"overrun={overrun_seconds:.2f}s"
                    )

    def _play_buffered_turn(self, job, source_pcm):
        """Use the existing output routes with preloaded, cancellable PortAudio buffers."""
        started = []
        try:
            self._main_buffer.prepare(source_pcm)
            streams = [self._audio_stream]
            if self._monitor_stream and self._monitor_route:
                self._monitor_buffer.prepare(_convert_pcm16_output(
                    source_pcm, source_rate=SOURCE_SAMPLE_RATE,
                    target_rate=self._monitor_route.sample_rate,
                    channels=self._monitor_route.channels,
                ))
                streams.insert(0, self._monitor_stream)
            if not self._is_epoch_active(job.epoch):
                self.request_abort()
                return
            if job.before_audio and job.before_audio() is False:
                self.request_abort()
                return
            if _ref_buffer is not None:
                samples = np.frombuffer(source_pcm, dtype=np.int16)
                _ref_buffer.extend(resample_poly(samples, up=2, down=3).astype(np.int16))
            for stream in streams:
                stream.start_stream()
                started.append(stream)
            notified = False

            def notify_first_pcm():
                nonlocal notified
                if not notified and self._main_buffer.first_frame.is_set():
                    notified = True
                    if job.on_audio_start and job.on_audio_start() is False:
                        self.request_abort()
                        return False
                return True

            deadline = time.monotonic() + self.audio_duration_seconds + 10.0
            while any(stream.is_active() for stream in started):
                if not self._is_epoch_active(job.epoch):
                    self.request_abort()
                    return
                if not notify_first_pcm():
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("整段播放未在音频时长＋10秒内结束")
                self._interrupted.wait(0.01)
            if not self._is_epoch_active(job.epoch):
                self.request_abort()
                return
            if not notify_first_pcm():
                return
            if self._main_buffer.position != len(source_pcm):
                raise RuntimeError("整段主输出提前停止")
            if self._monitor_stream and self._monitor_buffer.position != len(self._monitor_buffer.pcm):
                raise RuntimeError("整段监听提前停止")
            self._audio_chunk_count += 1
        except Exception as exc:
            self._failed_sentences.append(job.sequence)
            _log_fn(f"[Qwen3-TTS·整段播放失败] sentence={job.sequence} error={exc}")
            self.request_abort()
        finally:
            for stream in started:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
            underflows = self._main_buffer.underflows + (
                self._monitor_buffer.underflows if self._monitor_stream else 0)
            if underflows:
                _log_fn(f"[Qwen3-TTS·整段播放欠载] count={underflows}")

    def _monitor_worker(self):
        while True:
            item = self._monitor_queue.get()
            if item is None:
                return
            epoch, sentence, pcm = item
            if not self._is_epoch_active(epoch):
                continue
            try:
                _write_stream(
                    self._monitor_stream,
                    pcm,
                    label=(
                        "监听-WASAPI"
                        if self._monitor_route and self._monitor_route.uses_wasapi
                        else "监听-MME"
                    ),
                    sentence=sentence,
                )
            except Exception as exc:
                _log_fn(f"[Qwen3-TTS·监听] 播放线程退出: {exc}")
                return

    def _close_audio(self):
        with self._close_lock:
            self._close_audio_locked()

    def _close_audio_locked(self):
        with self._audio_write_lock:
            stream, self._audio_stream = self._audio_stream, None
            if stream:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
        if self._monitor_stream:
            self._monitor_queue.put(None)
            if self._monitor_thread:
                self._monitor_thread.join(timeout=2)
            stream, self._monitor_stream = self._monitor_stream, None
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _drain_queue(target):
        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                return

    @staticmethod
    def _field(value, name):
        if value is None:
            return None
        if isinstance(value, dict):
            return value.get(name)
        return getattr(value, name, None)
