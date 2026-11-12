"""独立 CosyVoice TTS（DashScope tts_v2）流式合成器。

文本架构下 speak() 借它出声。支持 CosyVoice 声音复刻音色（voice_id 来自百炼声音复刻）。
对外类 IndependentSynth，生命周期：
  open(voice_id, model, cable_index) → feed(sentence)... → finish() / abort()

依赖由 init() 注入（pyaudio / AEC 参考缓冲 / 监听设备 / 日志），避免与 lumi_tts 循环 import。
播放/声卡/AEC/监听机器与老链路一致；合成核心走 CosyVoice 流式（streaming_call /
streaming_complete / streaming_cancel + ResultCallback.on_data 收 PCM 帧）。

首帧超时 + 单次重放：喂第一句后启动看门狗，first_frame_timeout 秒内没收到第一帧音频
就取消当前会话、新建会话把已喂句子重放一遍（最多 max_retries 次）。首帧始终不到时
finish() 直接放弃，不再干等服务端自己的 60 秒超时。逻辑全在本文件内闭环，对上层透明。
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

from audio import role_volume

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


def _make_synthesizer(model, voice, callback):
    """创建一个 CosyVoice 流式合成会话。抽成模块函数是为了重放时重建会话、以及单测可注入假引擎。"""
    return SpeechSynthesizer(
        model=model, voice=voice,
        format=AudioFormat.PCM_24000HZ_MONO_16BIT,
        callback=callback,
    )


class IndependentSynth:
    """CosyVoice 流式合成器。open 建会话 + 启播放线程；feed 按句 streaming_call；
    finish 提交并等播完；abort 取消。音频帧写指定声卡 + 监听 + AEC（24k→16k 重采样）。

    首帧看门狗：喂第一句后启动，first_frame_timeout 秒没等到第一帧音频就取消当前会话、
    新建会话重放已喂句子（最多 max_retries 次）；两次都没首帧则标记放弃，finish 不再等服务端超时。"""

    def __init__(self, first_frame_timeout=4.0, max_retries=1, speaker="Lumi"):
        role_volume.get_volume(speaker)
        self._speaker = speaker
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

        # ---- 首帧超时 + 重放 ----
        self._first_frame_timeout = first_frame_timeout
        self._max_retries = max_retries
        self._voice_id = None
        self._model = None
        self._first_frame = Event()   # 收到第一帧音频
        self._resolved = Event()      # 已决议：要么首帧到、要么彻底放弃
        self._fed_sentences = []      # 已喂句子，供重放
        self._session_lock = Lock()   # 保护会话切换（看门狗重放 vs 上层继续 feed）
        self._retry_count = 0
        self._gave_up = False
        self._watchdog = None         # threading.Timer

    # ---- 生命周期 ----
    def open(self, voice_id, model, cable_index):
        self._cable_index = cable_index
        self._audio_chunk_count = 0
        self._audio_state = {"closed": False}

        # 重置首帧/重放状态
        self._voice_id = voice_id
        self._model = model
        self._first_frame.clear()
        self._resolved.clear()
        self._fed_sentences = []
        self._retry_count = 0
        self._gave_up = False
        self._watchdog = None

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
        self._syn = _make_synthesizer(model, voice_id, self._make_callback())
        self._opened = True

    def feed(self, text):
        if not (self._opened and text):
            return
        with self._session_lock:
            if self._gave_up:
                return
            self._fed_sentences.append(text)
            is_first = len(self._fed_sentences) == 1
            try:
                self._syn.streaming_call(text)
            except TimeoutError as exc:
                self._recover_connect_timeout_locked(exc)
        if is_first and not self._resolved.is_set():
            self._start_watchdog()

    def finish(self):
        if not self._opened:
            return
        if not self._fed_sentences:
            # 没喂过任何句子（看门狗从未启动）：无首帧决议可等，按原逻辑正常关流。
            try:
                self._syn.streaming_complete()
            except Exception as e:
                _log_fn(f"[CosyVoice·complete异常] {e}")
            self._cleanup(interrupted=False)
            return
        # 先等首帧决议：要么第一帧音频到了（会话正常），要么看门狗把重试用尽标记放弃。
        # 兜底超时留足两道看门狗 + 余量，避免 _resolved 因异常永不 set 而卡死。
        budget = (self._max_retries + 1) * self._first_frame_timeout + 1.0
        self._resolved.wait(timeout=budget)
        self._cancel_watchdog()

        with self._session_lock:
            gave_up = self._gave_up or not self._first_frame.is_set()

        if gave_up:
            # 首帧始终没来：取消会话、打无音频日志，绝不调 streaming_complete（否则会干等服务端 60 秒超时）。
            try:
                self._syn.streaming_cancel()
            except Exception:
                pass
            _log_fn("[CosyVoice·无音频] 未收到任何音频帧")
            self._cleanup(interrupted=True)
            return

        try:
            # 首帧已到、会话正常，阻塞到服务端合成完毕（所有 on_data 已回调 + on_complete）。
            self._syn.streaming_complete()
        except Exception as e:
            _log_fn(f"[CosyVoice·complete异常] {e}")
        if self._audio_chunk_count == 0:
            _log_fn("[CosyVoice·无音频] 未收到任何音频帧")
        self._cleanup(interrupted=False)

    def abort(self):
        if not self._opened:
            return
        self._cancel_watchdog()
        self._resolved.set()
        try:
            self._syn.streaming_cancel()
        except Exception:
            pass
        self._cleanup(interrupted=True)

    # ---- 首帧看门狗 + 重放 ----
    def _start_watchdog(self):
        if self._resolved.is_set():
            return
        w = threading.Timer(self._first_frame_timeout, self._on_first_frame_timeout)
        w.daemon = True
        self._watchdog = w
        w.start()

    def _cancel_watchdog(self):
        w = self._watchdog
        if w is not None:
            w.cancel()

    def _recover_connect_timeout_locked(self, error):
        """首次 streaming_call 建连超时时，限次重建会话并重放已喂文本。

        必须在 _session_lock 内调用。重试仍超时就把本轮标记为无音频并返回，
        让上层 speak() 正常收尾释放状态，不能把异常抛到主循环造成永久占锁。
        """
        while True:
            if self._retry_count >= self._max_retries:
                try:
                    self._syn.streaming_cancel()
                except Exception:
                    pass
                self._gave_up = True
                self._resolved.set()
                _log_fn(f"[CosyVoice·建连超时] 重试已用尽，放弃本轮合成: {error}")
                return

            self._retry_count += 1
            _log_fn(
                f"[CosyVoice·建连超时] 重建会话并重放(第{self._retry_count}次): {error}"
            )
            try:
                self._syn.streaming_cancel()
            except Exception:
                pass
            self._syn = _make_synthesizer(
                self._model, self._voice_id, self._make_callback()
            )
            try:
                for sentence in self._fed_sentences:
                    self._syn.streaming_call(sentence)
                return
            except TimeoutError as exc:
                error = exc

    def _on_first_frame_timeout(self):
        with self._session_lock:
            if self._first_frame.is_set():
                return  # 首帧在临界处刚到，放弃重放
            if self._retry_count >= self._max_retries:
                # 重试用尽，彻底放弃：唤醒 finish 走放弃路径。
                self._gave_up = True
                self._resolved.set()
                _log_fn("[CosyVoice·首帧超时] 重试已用尽，放弃本轮合成")
                return
            self._retry_count += 1
            _log_fn(f"[CosyVoice·首帧超时] {self._first_frame_timeout}s 未收到音频，"
                    f"取消并重放(第{self._retry_count}次)")
            try:
                self._syn.streaming_cancel()
            except Exception:
                pass
            # 新建会话，把已喂句子按顺序重放一遍。
            self._syn = _make_synthesizer(self._model, self._voice_id, self._make_callback())
            for sentence in self._fed_sentences:
                try:
                    self._syn.streaming_call(sentence)
                except Exception as e:
                    _log_fn(f"[CosyVoice·重放异常] {e}")
            # 起下一道看门狗（第二次机会）。
            w = threading.Timer(self._first_frame_timeout, self._on_first_frame_timeout)
            w.daemon = True
            self._watchdog = w
            w.start()

    # ---- 内部 ----
    def _make_callback(self):
        synth = self

        class CB(ResultCallback):
            def on_open(self):
                pass

            def on_data(self, data: bytes):
                if not synth._first_frame.is_set():
                    synth._first_frame.set()
                    synth._resolved.set()
                    synth._cancel_watchdog()
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
                item = role_volume.scale_pcm16(item, self._speaker)
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
