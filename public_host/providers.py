"""Small service boundary: text in, a reply or decoded PCM out.

No vendor endpoint, credential, model selection or parent .env is implicit.
New protocols implement the same methods instead of changing the session worker.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import json
import socket
import time
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import wave


class ProviderError(RuntimeError):
    """A deliberately safe, user-facing error; never include a response body/key."""


class Cancelled(Exception):
    """The caller invalidated this work; it must produce no later output."""


def check_cancel(cancel: Event):
    if cancel.is_set():
        raise Cancelled()


@dataclass(frozen=True)
class AudioClip:
    pcm: bytes
    sample_rate: int
    channels: int = 1
    sample_width: int = 2

    def validate(self):
        if self.sample_width != 2 or self.channels not in (1, 2):
            raise ProviderError("需要单声道或双声道的 16 位 PCM 音频。")
        if not 8000 <= self.sample_rate <= 96000:
            raise ProviderError("音频采样率必须在 8000–96000 Hz 之间。")
        frame = self.channels * self.sample_width
        if not self.pcm or len(self.pcm) % frame:
            raise ProviderError("语音服务没有返回完整音频。")
        if len(self.pcm) / frame / self.sample_rate > 120:
            raise ProviderError("单次语音超过 120 秒，请缩短文本。")
        return self


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirected bearer token must never leave the configured endpoint.
        return None


def post_json(service: dict, route: str, payload: dict, cancel: Event,
              *, limit: int = 2_000_000) -> bytes:
    check_cancel(cancel)
    timeout = float(service.get("timeout", 30))
    deadline = time.monotonic() + timeout
    headers = {"Content-Type": "application/json", "Accept": "application/json, audio/wav"}
    key = service.get("api_key", "")
    if key:
        headers["Authorization"] = "Bearer " + key
    request = Request(service["base_url"].rstrip("/") + route,
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers=headers, method="POST")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            chunks, size = [], 0
            while True:
                check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise ProviderError("服务请求超时，请检查连接或缩短输入。")
                chunk = response.read1(65536)
                check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise ProviderError("服务请求超时，请检查连接或缩短输入。")
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ProviderError("服务返回内容过大，已停止读取。")
                chunks.append(chunk)
        check_cancel(cancel)
        return b"".join(chunks)
    except HTTPError as exc:
        exc.close()
        check_cancel(cancel)
        raise ProviderError(f"服务返回 HTTP {exc.code}；请核对地址、模型、权限与额度。") from None
    except (URLError, TimeoutError, socket.timeout, OSError, ValueError):
        check_cancel(cancel)
        raise ProviderError("无法完成服务请求，请检查地址、网络和请求超时设置。") from None


class CompatibleChat:
    def __init__(self, config: dict):
        self.config = dict(config)

    def complete(self, messages: list[dict], cancel: Event) -> str:
        payload = {"model": self.config["model"], "messages": messages, "stream": False}
        field = self.config.get("token_limit_field", "max_tokens")
        if field:
            payload[field] = self.config.get("max_tokens", 256)
        raw = post_json(self.config, "/chat/completions", payload, cancel)
        try:
            text = json.loads(raw)["choices"][0]["message"]["content"]
        except (ValueError, TypeError, KeyError, IndexError):
            raise ProviderError("聊天服务返回格式不符合兼容接口。") from None
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("聊天服务没有返回可朗读的文本。")
        if len(text) > 12000:
            raise ProviderError("回复过长，请降低回复长度限制。")
        return text.strip()


def _wav_data_size(raw: bytes) -> int:
    """Validate one complete RIFF/WAVE container without private wave fields.

    Unknown metadata chunks are allowed and their RIFF padding is respected.
    This adapter consumes one PCM data chunk, not concatenated WAV streams.
    """
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("Not a RIFF/WAVE container")
    end = 8 + int.from_bytes(raw[4:8], "little")
    if end != len(raw) or end < 12:
        raise ValueError("Incomplete RIFF container")
    offset, data_size = 12, None
    while offset < end:
        if offset + 8 > end:
            raise ValueError("Incomplete RIFF chunk header")
        kind = raw[offset:offset + 4]
        size = int.from_bytes(raw[offset + 4:offset + 8], "little")
        next_offset = offset + 8 + size + (size % 2)
        if next_offset > end:
            raise ValueError("Incomplete RIFF chunk")
        if kind == b"data":
            if data_size is not None:
                raise ValueError("Multiple PCM data chunks are unsupported")
            data_size = size
        offset = next_offset
    if data_size is None:
        raise ValueError("Missing PCM data chunk")
    return data_size


class CompatibleSpeech:
    def __init__(self, config: dict):
        self.config = dict(config)

    def synthesize(self, text: str, voice: str, cancel: Event) -> AudioClip:
        payload = {"model": self.config["model"], "input": text,
                   "voice": voice, "response_format": "wav"}
        raw = post_json(self.config, "/audio/speech", payload, cancel, limit=24_000_000)
        try:
            data_size = _wav_data_size(raw)
            with wave.open(io.BytesIO(raw), "rb") as audio:
                frames = audio.getnframes()
                clip = AudioClip(audio.readframes(frames), audio.getframerate(),
                                 audio.getnchannels(), audio.getsampwidth())
                expected_size = frames * clip.channels * clip.sample_width
                if (audio.getcomptype() != "NONE" or data_size != expected_size
                        or len(clip.pcm) != expected_size):
                    raise ProviderError("语音服务返回了压缩或不完整的 WAV。")
        except (wave.Error, EOFError, ValueError):
            raise ProviderError("此适配器需要 WAV 音频；该服务可能使用另一种语音协议。") from None
        check_cancel(cancel)
        return clip.validate()


class DemoChat:
    def complete(self, messages: list[dict], cancel: Event) -> str:
        check_cancel(cancel)
        return "[离线演示] 已收到：" + messages[-1]["content"][:180]


class DemoSpeech:
    def synthesize(self, text: str, voice: str, cancel: Event) -> AudioClip:
        check_cancel(cancel)
        return AudioClip(bytes(1600), 8000)


def make_services(config: dict):
    if config["mode"] == "demo":
        return DemoChat(), DemoSpeech()
    return CompatibleChat(config["llm"]), CompatibleSpeech(config["tts"])
