"""独立 CosyVoice TTS（DashScope tts_v2）流式合成器。

文本架构下 speak() 借它出声。支持 CosyVoice 声音复刻音色（voice_id 来自百炼声音复刻）。
对外类 IndependentSynth，生命周期：
  open(voice_id, model, cable_index) → feed(sentence)... → finish() / abort()

依赖由 init() 注入（pyaudio / AEC 参考缓冲 / 监听设备 / 日志），避免与 lumi_tts 循环 import。
播放/声卡/AEC/监听机器与老链路一致；合成核心走 CosyVoice 流式（streaming_call /
streaming_complete / streaming_cancel + ResultCallback.on_data 收 PCM 帧）。
"""

import os
import queue
import threading
from threading import Lock, Event

import numpy as np
import pyaudiowpatch as pyaudio
import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer, ResultCallback, AudioFormat
from scipy.signal import resample_poly
from dotenv import load_dotenv

# ======= 注入依赖（由 init() 设置）=======
_pa_instance = None
_monitor_device_index = None
_ref_buffer = None  # AEC 参考缓冲（= lumi_tts.tts_ref_buffer）
_log_fn = lambda msg: print(msg, flush=True)


def init(*, pa_instance, ref_buffer=None,
         monitor_device_index=None, log_fn=None):
    """由 lumi_tts.init 转调一次，注入合成器需要的外部依赖。

    TTS 模型名不在这里固定——复刻音色挂在 cosyvoice-v3-plus 等模型下，model 随音色变，
    由 open() 逐次传入（音色库按 voice_name 解析出 voice_id + model）。
    """
    global _pa_instance, _monitor_device_index, _ref_buffer, _log_fn
    _pa_instance = pa_instance
    _ref_buffer = ref_buffer
    _monitor_device_index = monitor_device_index
    if log_fn:
        _log_fn = log_fn
    # DashScope 鉴权：与 lumi_asr 同源（DASHSCOPE_API_KEY），不依赖 ASR 模块先初始化。
    load_dotenv()
    if not getattr(dashscope, "api_key", None):
        dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY")


def set_monitor_device(index):
    global _monitor_device_index
    _monitor_device_index = index


class IndependentSynth:
    """CosyVoice 流式合成器。open 建会话 + 启播放线程；feed 按句 streaming_call；
    finish 提交并等播完；abort 取消。音频帧写指定声卡 + 监听 + AEC（24k→16k 重采样）。"""

    def __init__(self):
        self._syn = None
        self._cable_index = None
        self._audio_stream = None
        self._audio_write_lock = Lock()
        self._audio_state = {"closed": False}
        self._monitor_stream = None
        self._monitor_queue: queue.Queue = queue.Queue()
        self._monitor_thread = None
        self._playback_queue: queue.Queue = queue.Queue()
        self._playback_thread = None
        self._audio_chunk_count = 0
        self._opened = False

    # ---- 生命周期 ----
    def open(self, voice_id, model, cable_index):
        self._cable_index = cable_index
        self._audio_chunk_count = 0
        self._audio_state = {"closed": False}

        _audio_fpb = 4800  # 200ms @24kHz，撑过句间空档避免 underrun
        self._audio_stream = None
        if cable_index is not None:
            try:
                self._audio_stream = _pa_instance.open(
                    format=pyaudio.paInt16, channels=1, rate=24000, output=True,
                    output_device_index=cable_index, frames_per_buffer=_audio_fpb,
                )
            except Exception as e:
                _log_fn(f"[CosyVoice] 声卡 index={cable_index} 不可用({e})，回退默认输出")
                self._audio_stream = None
        if self._audio_stream is None:
            self._audio_stream = _pa_instance.open(
                format=pyaudio.paInt16, channels=1, rate=24000, output=True,
                frames_per_buffer=_audio_fpb,
            )

        if _monitor_device_index is not None and cable_index is not None:
            try:
                self._monitor_stream = _pa_instance.open(
                    format=pyaudio.paInt16, channels=1, rate=24000, output=True,
                    output_device_index=_monitor_device_index,
                    frames_per_buffer=_audio_fpb,
                )
                self._monitor_thread = threading.Thread(
                    target=self._monitor_writer, daemon=True)
                self._monitor_thread.start()
            except Exception as e:
                _log_fn(f"[CosyVoice·监听] 无法打开监听设备: {e}")
                self._monitor_stream = None

        self._playback_thread = threading.Thread(
            target=self._playback_worker, daemon=True, name="cosyvoice-playback")
        self._playback_thread.start()

        # CosyVoice 流式合成器：PCM 24kHz，回调收音频帧。连接在首次 streaming_call 惰性建立。
        self._syn = SpeechSynthesizer(
            model=model, voice=voice_id,
            format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            callback=self._make_callback(),
        )
        self._opened = True

    def feed(self, text):
        if self._opened and text:
            self._syn.streaming_call(text)

    def finish(self):
        if not self._opened:
            return
        try:
            # 阻塞到服务端合成完毕（所有 on_data 已回调 + on_complete）。
            self._syn.streaming_complete()
        except Exception as e:
            _log_fn(f"[CosyVoice·complete异常] {e}")
        if self._audio_chunk_count == 0:
            _log_fn("[CosyVoice·无音频] 未收到任何音频帧")
        self._cleanup(interrupted=False)

    def abort(self):
        if not self._opened:
            return
        try:
            self._syn.streaming_cancel()
        except Exception:
            pass
        self._cleanup(interrupted=True)

    # ---- 内部 ----
    def _make_callback(self):
        synth = self

        class CB(ResultCallback):
            def on_open(self):
                pass

            def on_data(self, data: bytes):
                synth._audio_chunk_count += 1
                synth._playback_queue.put_nowait(data)

            def on_complete(self):
                pass

            def on_error(self, message):
                _log_fn(f"[CosyVoice·错误] {message}")

            def on_close(self):
                pass

        return CB()

    def _playback_worker(self):
        while True:
            item = self._playback_queue.get()
            if item is None:
                break
            try:
                with self._audio_write_lock:
                    if self._audio_state["closed"]:
                        break
                    self._audio_stream.write(item)
                if self._monitor_stream:
                    self._monitor_queue.put_nowait(item)
            except Exception as e:
                _log_fn(f"[CosyVoice·播放异常] {e}")
                break
            if _ref_buffer is not None:
                samples = np.frombuffer(item, dtype=np.int16)
                ref16 = resample_poly(samples, up=2, down=3).astype(np.int16)
                _ref_buffer.extend(ref16)

    def _monitor_writer(self):
        while True:
            data = self._monitor_queue.get()
            if data is None:
                break
            try:
                self._monitor_stream.write(data)
            except Exception:
                break

    def _cleanup(self, interrupted: bool):
        if interrupted:
            while not self._playback_queue.empty():
                try:
                    self._playback_queue.get_nowait()
                except queue.Empty:
                    break
            self._playback_queue.put(None)
            self._playback_thread.join(timeout=3)
        else:
            self._playback_queue.put(None)
            self._playback_thread.join(timeout=60)
        with self._audio_write_lock:
            if not self._audio_state["closed"]:
                try:
                    self._audio_stream.stop_stream()
                except Exception:
                    pass
                try:
                    self._audio_stream.close()
                except Exception:
                    pass
                self._audio_state["closed"] = True
        if self._monitor_stream:
            self._monitor_queue.put(None)
            if self._monitor_thread:
                self._monitor_thread.join(timeout=2)
            try:
                self._monitor_stream.stop_stream()
            except Exception:
                pass
            try:
                self._monitor_stream.close()
            except Exception:
                pass
        self._opened = False
