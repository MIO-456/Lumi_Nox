"""PCM playback completion is the output-device completion, not synthesis completion."""
from __future__ import annotations

from array import array
import math
import sys
import threading
import time

from public_host.providers import AudioClip, Cancelled, ProviderError, check_cancel


def _sounddevice():
    try:
        import sounddevice
        return sounddevice
    except (ImportError, OSError):
        raise ProviderError("未找到音频播放依赖，请安装 requirements-host.txt；Linux 还需要 PortAudio。") from None


def output_devices():
    sd = _sounddevice()
    try:
        return [{"id": i, "name": d["name"], "channels": d["max_output_channels"]}
                for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]
    except Exception:
        raise ProviderError("无法读取音频设备，请检查系统声卡。") from None


class AudioPlayer:
    def __init__(self, device=None, *, backend=None):
        self.device = device
        self._backend = backend

    def play(self, clip: AudioClip, cancel, on_level=None) -> str:
        clip.validate()
        check_cancel(cancel)
        sd = self._backend or _sounddevice()
        finished = threading.Event()
        offset = 0
        failure = []
        framesize = clip.channels * clip.sample_width
        callback = on_level or (lambda _level: None)

        def process(outdata, frames, _timing, status):
            nonlocal offset
            if cancel.is_set():
                outdata[:] = bytes(len(outdata))
                raise sd.CallbackAbort
            length = frames * framesize
            chunk = clip.pcm[offset:offset + length]
            offset += len(chunk)
            outdata[:] = chunk + bytes(length - len(chunk))
            samples = array("h", chunk)
            if sys.byteorder != "little":
                samples.byteswap()
            level = math.sqrt(sum(x * x for x in samples) / max(1, len(samples))) / 32768
            try:
                callback(min(1.0, level * 4))
            except Exception:
                failure.append(True)
                raise sd.CallbackAbort
            if offset >= len(clip.pcm):
                # PortAudio drains queued audio before finished_callback fires.
                raise sd.CallbackStop

        stream = None
        try:
            stream = sd.RawOutputStream(samplerate=clip.sample_rate, channels=clip.channels,
                                        dtype="int16", device=self.device,
                                        blocksize=max(128, clip.sample_rate // 50),
                                        callback=process, finished_callback=finished.set)
            stream.start()
            deadline = time.monotonic() + len(clip.pcm) / framesize / clip.sample_rate + 15
            while not finished.wait(0.02):
                if cancel.is_set():
                    stream.abort()
                    raise Cancelled()
                if time.monotonic() > deadline or not stream.active:
                    raise ProviderError("音频设备未完成播放，请检查所选输出设备。")
            check_cancel(cancel)
            if failure or offset != len(clip.pcm):
                raise ProviderError("音频播放提前结束，未记录为完整发言。")
            return "complete"
        except (Cancelled, ProviderError):
            raise
        except Exception:
            check_cancel(cancel)
            raise ProviderError("无法播放音频，请检查输出设备和音频格式。") from None
        finally:
            if stream is not None:
                try:
                    stream.abort() if cancel.is_set() or failure else stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                callback(0.0)
            except Exception:
                pass


class DemoPlayer:
    def __init__(self, duration=0.1):
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or not 0.05 <= duration <= 10):
            raise ProviderError("演示播放时长必须是 0.05–10 秒的有限数字。")
        self.duration = duration

    def play(self, clip: AudioClip, cancel, on_level=None) -> str:
        deadline = time.monotonic() + self.duration
        try:
            while True:
                check_cancel(cancel)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "simulated"
                if on_level:
                    on_level(0.35 + 0.2 * math.sin(remaining * 12))
                if cancel.wait(min(0.05, remaining)):
                    raise Cancelled()
        finally:
            if on_level:
                on_level(0.0)


def make_player(config: dict):
    return (DemoPlayer(config.get("demo_playback_seconds", 0.1))
            if config["mode"] == "demo" else AudioPlayer(config["audio"]["device"]))
