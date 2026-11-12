"""Public adapter for user-supplied CosyVoice identifiers.

The live project keeps its cloned-voice registry private. The open-core resolves an
example character's ``voice_name`` through environment variables instead. Missing
values intentionally raise ``KeyError`` so ``lumi_tts`` falls back to a system voice.
"""

import os
import re


DEFAULT_MODEL = "cosyvoice-v3-plus"


def _env_prefix(voice_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", voice_name or "").strip("_").upper()
    if normalized.endswith("_VOICE"):
        normalized = normalized[:-6]
    if not normalized:
        raise KeyError("voice_name is empty")
    return normalized


def get_voice_id(voice_name: str) -> str:
    key = f"{_env_prefix(voice_name)}_VOICE_ID"
    value = os.getenv(key, "").strip()
    if not value:
        raise KeyError(f"set {key} to use a cloned voice")
    return value


def get_voice_model(voice_name: str) -> str:
    key = f"{_env_prefix(voice_name)}_VOICE_MODEL"
    return os.getenv(key, DEFAULT_MODEL).strip() or DEFAULT_MODEL
