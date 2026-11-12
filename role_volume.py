"""Thread-safe per-character voice gain shared by every TTS playback path."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from pathlib import Path

import numpy as np


SPEAKERS = ("Lumi", "Nox")
DEFAULT_VOLUME = 1.0
MAX_VOLUME = 4.0
VOLUME_FILE = Path(__file__).with_name("role_volumes.json")


class RoleVolumeStore:
    def __init__(self, path=VOLUME_FILE, *, debounce_seconds=0.2):
        self._path = Path(path)
        self._debounce_seconds = float(debounce_seconds)
        self._lock = threading.RLock()
        self._volumes = {speaker: DEFAULT_VOLUME for speaker in SPEAKERS}
        self._persist_timer = None
        self._load()

    @staticmethod
    def _validate_speaker(speaker):
        if speaker not in SPEAKERS:
            raise ValueError(f"未知角色: {speaker!r}")
        return speaker

    @staticmethod
    def _validate_volume(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("角色音量必须是有限数值")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("角色音量必须是有限数值")
        return max(0.0, min(MAX_VOLUME, value))

    def _load(self):
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        for speaker in SPEAKERS:
            try:
                self._volumes[speaker] = self._validate_volume(payload.get(speaker))
            except (TypeError, ValueError):
                self._volumes[speaker] = DEFAULT_VOLUME

    def get_volume(self, speaker):
        speaker = self._validate_speaker(speaker)
        with self._lock:
            return self._volumes[speaker]

    def get_all_volumes(self):
        with self._lock:
            return dict(self._volumes)

    def set_volume(self, speaker, value, *, persist=True):
        speaker = self._validate_speaker(speaker)
        value = self._validate_volume(value)
        with self._lock:
            self._volumes[speaker] = value
            if persist:
                self._schedule_persist_locked()
        return value

    def _schedule_persist_locked(self):
        if self._persist_timer is not None:
            self._persist_timer.cancel()
        self._persist_timer = threading.Timer(
            self._debounce_seconds, self._persist_from_timer
        )
        self._persist_timer.daemon = True
        self._persist_timer.start()

    def _persist_from_timer(self):
        with self._lock:
            self._persist_timer = None
            self._write_snapshot_locked()

    def _write_snapshot_locked(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(self._volumes, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def flush_pending(self):
        with self._lock:
            if self._persist_timer is not None:
                self._persist_timer.cancel()
                self._persist_timer = None
            self._write_snapshot_locked()

    def scale_pcm16(self, data: bytes, speaker):
        gain = self.get_volume(speaker)
        if gain == 1.0 or not data:
            return data
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        return np.clip(samples * gain, -32768, 32767).astype(np.int16).tobytes()

    def scale_float32(self, data: bytes, speaker):
        gain = self.get_volume(speaker)
        if gain == 1.0 or not data:
            return data
        samples = np.frombuffer(data, dtype=np.float32)
        return np.clip(samples * gain, -1.0, 1.0).astype(np.float32).tobytes()


_STORE = RoleVolumeStore()


def get_volume(speaker):
    return _STORE.get_volume(speaker)


def get_all_volumes():
    return _STORE.get_all_volumes()


def set_volume(speaker, value, *, persist=True):
    return _STORE.set_volume(speaker, value, persist=persist)


def scale_pcm16(data, speaker):
    return _STORE.scale_pcm16(data, speaker)


def scale_float32(data, speaker):
    return _STORE.scale_float32(data, speaker)


def flush_pending():
    _STORE.flush_pending()
