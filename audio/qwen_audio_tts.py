"""Adapt tts_v2 Qwen-Audio synthesis to the existing complete-sentence pipeline."""

import base64
import math
import os
from contextlib import suppress
from types import SimpleNamespace

from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer


def stream_synthesis(*, model, voice, text, timeout):
    """Yield one verified sentence of 24 kHz mono int16 PCM.

    The synchronous SDK path avoids its extra asynchronous completion thread.
    ``timeout`` bounds completion waiting; the SDK separately bounds connection
    and task startup (5 and 10 seconds). Closing an executing Python generator
    cannot interrupt that wait; the caller discards obsolete sentences afterward.
    The connection is closed before any response is yielded.
    """
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Qwen-Audio TTS timeout must be finite and positive")
    # This model family needs a compatible service selected by the caller.
    # The SDK's default endpoint is not a verified fallback for these models.
    endpoint = os.environ.get("QWEN_AUDIO_TTS_WEBSOCKET_URL", "").strip()
    if not endpoint:
        raise ValueError(
            "Set QWEN_AUDIO_TTS_WEBSOCKET_URL to a compatible Qwen-Audio TTS service"
        )
    synthesizer = SpeechSynthesizer(
        model=model,
        voice=voice,
        format=AudioFormat.PCM_24000HZ_MONO_16BIT,
        url=endpoint,
    )
    finished = False
    try:
        pcm = synthesizer.call(text, timeout_millis=max(1, math.ceil(timeout * 1000)))
        request_id = synthesizer.get_last_request_id()
        result = synthesizer.get_response() or {}
        header = result.get("header") or {}
        # In sync mode the SDK raises task-failed on its receiver thread, while
        # call() can still return previously received PCM. Require the terminal
        # success event so a truncated sentence can never reach playback.
        if header.get("event") != "task-finished":
            detail = header.get("error_message") or "missing task-finished event"
            code = header.get("error_code") or header.get("event") or "incomplete"
            raise RuntimeError(
                f"Qwen-Audio TTS failed ({code}): {detail}; request_id={request_id}"
            )
        if not isinstance(pcm, (bytes, bytearray)) or not pcm:
            raise RuntimeError(f"Qwen-Audio TTS returned empty PCM; request_id={request_id}")
        if len(pcm) % 2:
            raise RuntimeError(f"Qwen-Audio TTS returned invalid int16 PCM; request_id={request_id}")
        finished = True
    finally:
        # Startup failures have no active task; timeout/failure may already have
        # closed the socket. Cleanup must not replace the original error.
        if not finished:
            with suppress(Exception):
                synthesizer.streaming_cancel()
        with suppress(Exception):
            synthesizer.close()

    yield SimpleNamespace(
        status_code=200,
        request_id=request_id,
        output={"audio": {"data": base64.b64encode(pcm).decode("ascii")}},
    )
