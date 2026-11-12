"""Lumi 流式 ASR 模块（fun-asr-realtime 云端，连接轮换架构）。

每轮对话独占一条云端 WebSocket，消灭常驻连接下的跨轮上下文累积（串轮）。
下一轮的连接在当前 Lumi 说话期间就建好并用 WebSocket 心跳保活，
Mio 真正开始说话那一瞬间用的是"热"连接，建连延迟被完全隐藏。

对外接口（三组）：
  生命周期：arm_next / promote_and_start_turn / retire_active
  音频与结果：push_audio / wait_for_final / get_latest_text
  状态查询：is_next_ready / sync_build_fallback 等埋点属性

内部四层：
- SingleConnection：一轮一条、四态（BUILDING/ARMED/ACTIVE/RETIRED/FAILED）
- ResultState + AudioBuffer：每条连接独立，物理隔离避免串轮
- 保活线程：ARMED 状态下做状态巡检，底层 WebSocket 心跳交给 SDK
- StreamingAsr 主类：持有 active + next 两条连接，协调生命周期
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

import numpy as np

import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

logger = logging.getLogger("lumi_asr")


class _SuppressNoValidAudioFilter(logging.Filter):
    """吃掉 DashScope SDK 在收到 NO_VALID_AUDIO_ERROR 服务端响应时直接 error 级别打到
    根 logger 的那条 "Request failed, request_id: ..." 记录。

    本项目的预备热连接架构必然会让空闲连接被服务端判超时，这是预期行为；连接 FAILED 后
    续租循环会换上新连接。SDK 的这条 logger.error 是在 dashscope.audio.asr.recognition.py
    第 491 行无条件触发的，绕过了我们的 _FunAsrCallback.on_error。这里在 SDK 自己的
    logger 上挂 filter 拦截，比 monkey-patch SDK 干净。其他真错误（鉴权失败/网络断/未知）
    仍然正常输出。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return "NO_VALID_AUDIO_ERROR" not in msg


def _install_dashscope_log_filter() -> None:
    """幂等地把 NO_VALID_AUDIO_ERROR 静默 filter 装到 dashscope logger 上。"""
    ds_logger = logging.getLogger("dashscope")
    for f in ds_logger.filters:
        if isinstance(f, _SuppressNoValidAudioFilter):
            return
    ds_logger.addFilter(_SuppressNoValidAudioFilter())

# ============ 常量 ============
FUN_ASR_MODEL = "fun-asr-realtime-2026-02-28"
FUN_ASR_VOCABULARY_ID = "vocab-lumi-937f6509ac9a41fd9cf933fcdca72185"
FUN_ASR_BASE_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
SAMPLE_RATE = 16000
PCM_CHUNK_BYTES = 3200  # 每批推送字节数（100 ms @ 16kHz 16bit mono）
PCM_CHUNK_MS = 100
KEEPALIVE_INTERVAL_SEC = 4.0     # 预备连接状态巡检间隔；WebSocket 心跳由 SDK heartbeat=True 负责
KEEPALIVE_MAX_FAILURES = 3       # 保活连续失败多少次视为连接失效
PROMOTE_SYNC_TIMEOUT_SEC = 1.5   # 升级时同步等预备就绪的最大秒数
ARMED_MAX_IDLE_SEC = 12.0        # fun-asr 长时间无有效音频会断任务；到期前后台续租预备连接


@dataclass
class AsrReadout:
    """get_latest_text() / wait_for_final() 的返回值。"""
    text: str              # 当前最新识别文本
    degraded: bool         # True 表示本次应走本地降级（无活跃连接）
    audio_duration_sec: float  # 本轮累计喂入的音频时长（秒），给降级判断用
    final_hit: bool = False    # wait_for_final 是否在超时前命中 sentence_end（复盘用）


# ============ 音频转换 ============
def _float32_to_int16_bytes(audio: np.ndarray) -> bytes:
    """float32 [-1.0, 1.0] → int16 little-endian PCM bytes。超出范围的样本被 clip。"""
    clipped = np.clip(audio, -1.0, 1.0)
    # 乘 32767 而不是 32768，避免 +1.0 * 32768 = 32768 溢出 int16
    scaled = (clipped * 32767.0).astype(np.int16)
    return scaled.tobytes()


class AudioBuffer:
    """PCM bytes 累积缓冲，累到 PCM_CHUNK_BYTES 才吐出一块。由单条连接的 send 路径调用，单线程访问。"""

    def __init__(self) -> None:
        self._buf = bytearray()

    def append(self, data: bytes) -> None:
        self._buf.extend(data)

    def pop_ready_chunk(self) -> Optional[bytes]:
        if len(self._buf) < PCM_CHUNK_BYTES:
            return None
        chunk = bytes(self._buf[:PCM_CHUNK_BYTES])
        del self._buf[:PCM_CHUNK_BYTES]
        return chunk

    def drain_all(self) -> bytes:
        remaining = bytes(self._buf)
        self._buf.clear()
        return remaining


# ============ 结果维护层 ============
class ResultState:
    """ASR 结果的线程安全状态。fun-asr 回调（SDK 线程）更新，主循环读取。

    每条 SingleConnection 独立持有一份，避免跨连接状态污染。
    sentence_end 用 threading.Event 暴露给 wait_for_final 的等待方。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frozen = ""         # 已 sentence_end 冻结的文本
        self._intermediate = ""   # 最新中间结果（每次 on_event 覆盖）
        self._last_sentence_end_ts: float = 0.0
        self._sentence_end_event = threading.Event()

    def update_intermediate(self, text: str) -> None:
        with self._lock:
            self._intermediate = text

    def finalize_sentence(self, final_text: str) -> None:
        with self._lock:
            self._frozen += final_text
            self._intermediate = ""
            self._last_sentence_end_ts = time.time()
        # set event 放在锁外，避免持锁唤醒
        self._sentence_end_event.set()

    def get_full_text(self) -> str:
        with self._lock:
            return self._frozen + self._intermediate

    def get_last_sentence_end_ts(self) -> float:
        with self._lock:
            return self._last_sentence_end_ts

    def wait_sentence_end(self, timeout_sec: float) -> bool:
        return self._sentence_end_event.wait(timeout=timeout_sec)

    def is_sentence_end_set(self) -> bool:
        """非阻塞查询：本轮是否已有 sentence_end 到达。"""
        return self._sentence_end_event.is_set()

    def reset(self) -> None:
        with self._lock:
            self._frozen = ""
            self._intermediate = ""
            self._last_sentence_end_ts = 0.0
        self._sentence_end_event.clear()


# ============ fun-asr 回调桥接 ============
class _FunAsrCallback(RecognitionCallback):
    """把 DashScope SDK 的回调事件翻译成 ResultState 更新 + on_sentence_end 钩子。

    SDK 回调在独立线程执行。ResultState 已内置锁，线程安全。
    is_current_hook 用于忽略 RETIRED/FAILED 状态的迟到回调，避免脏写已退役连接的状态。
    """

    def __init__(
        self,
        result_state: "ResultState",
        on_sentence_end: Optional[Callable[[str], None]] = None,
        on_error_hook: Optional[Callable[[], None]] = None,
        is_current_hook: Optional[Callable[[], bool]] = None,
        on_open_hook: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self._result_state = result_state
        self._on_sentence_end = on_sentence_end
        self._on_error_hook = on_error_hook
        self._is_current_hook = is_current_hook
        self._on_open_hook = on_open_hook
        self.intermediate_count = 0

    def on_open(self) -> None:
        # 预备连接续租会频繁 open/close，主日志不需要看到这些事件，降级到 debug
        logger.debug("[lumi_asr] fun-asr 连接已建立")
        if self._on_open_hook:
            try:
                self._on_open_hook()
            except Exception:
                logger.exception("[lumi_asr] on_open_hook 抛异常")

    def on_close(self) -> None:
        logger.debug("[lumi_asr] fun-asr 连接已关闭")

    def on_error(self, message) -> None:
        if self._is_current_hook and not self._is_current_hook():
            return
        msg_text = str(getattr(message, "message", message))
        # 空闲预备连接被服务端判超时是热连接架构的预期行为，主日志静默；
        # 后续 _on_error_hook 仍会把连接标 FAILED，由后台续租循环换上新连接。
        if "NO_VALID_AUDIO_ERROR" in msg_text or "no valid audio" in msg_text.lower():
            logger.debug(f"[lumi_asr] fun-asr 空闲超时（预期，由续租换连接）: {msg_text}")
        else:
            logger.warning(f"[lumi_asr] fun-asr 错误: {msg_text}")
        if self._on_error_hook:
            try:
                self._on_error_hook()
            except Exception:
                logger.exception("[lumi_asr] on_error_hook 抛异常")

    def on_event(self, result: RecognitionResult) -> None:
        if self._is_current_hook and not self._is_current_hook():
            return
        sentence = result.get_sentence()
        if not sentence or "text" not in sentence or not sentence["text"]:
            return
        text = sentence["text"]
        if RecognitionResult.is_sentence_end(sentence):
            self._result_state.finalize_sentence(text)
            if self._on_sentence_end:
                try:
                    self._on_sentence_end(text)
                except Exception:
                    logger.exception("[lumi_asr] on_sentence_end 抛异常")
        else:
            self.intermediate_count += 1
            self._result_state.update_intermediate(text)


# ============ 单轮连接 ============
class ConnectionState(Enum):
    BUILDING = "building"   # 正在建连
    ARMED = "armed"         # 建好了，等待 Mio 真正开口；静音保活中
    ACTIVE = "active"       # 正在接收 Mio 真实麦克风语音
    RETIRED = "retired"     # 已关闭
    FAILED = "failed"       # 建连或保活失败


class SingleConnection:
    """一轮对话独占的 fun-asr 连接。生命周期：BUILDING → ARMED → ACTIVE → RETIRED。

    每条连接有独立的 ResultState 和 AudioBuffer，物理上隔离跨轮状态——
    这是消灭串轮的根本保障。
    """

    def __init__(self, on_sentence_end: Optional[Callable[[str], None]] = None) -> None:
        self._lock = threading.Lock()
        self._state = ConnectionState.BUILDING
        self._recognition: Optional[Recognition] = None
        self._callback: Optional[_FunAsrCallback] = None
        self._result_state = ResultState()
        self._audio_buffer = AudioBuffer()
        self._on_sentence_end = on_sentence_end

        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop = threading.Event()
        self._keepalive_ping_count = 0

        self._arm_start_ts: float = 0.0
        # on_open 回调真正触发前为 -1（尚未测到连接就绪）；
        # 回调到达时填入 (on_open 时刻 − arm_start) 的毫秒数
        self._arm_to_ready_ms: int = -1
        self._opened_event = threading.Event()
        self._audio_duration_sec: float = 0.0

    # ---- 状态查询 ----

    @property
    def state(self) -> ConnectionState:
        with self._lock:
            return self._state

    @property
    def result_state(self) -> ResultState:
        return self._result_state

    @property
    def keepalive_ping_count(self) -> int:
        return self._keepalive_ping_count

    @property
    def arm_to_ready_ms(self) -> int:
        return self._arm_to_ready_ms

    @property
    def audio_duration_sec(self) -> float:
        return self._audio_duration_sec

    @property
    def armed_age_sec(self) -> float:
        if self._arm_start_ts <= 0:
            return 0.0
        return time.time() - self._arm_start_ts

    @property
    def intermediate_count(self) -> int:
        cb = self._callback
        return cb.intermediate_count if cb else 0

    def is_usable(self) -> bool:
        """ARMED 或 ACTIVE 视为可用。"""
        return self.state in (ConnectionState.ARMED, ConnectionState.ACTIVE)

    def is_active(self) -> bool:
        return self.state == ConnectionState.ACTIVE

    def is_stale_armed(self) -> bool:
        return self.state == ConnectionState.ARMED and self.armed_age_sec >= ARMED_MAX_IDLE_SEC

    # ---- 生命周期 ----

    def build(self) -> bool:
        """触发建连。rec.start() 本身非阻塞——握手完成会异步回调 on_open。
        成功发起 → ARMED 并返回 True；失败 → FAILED 并返回 False。
        arm_to_ready_ms 的真实值由 _mark_opened 在 on_open 回调时填入。
        """
        self._arm_start_ts = time.time()

        def is_current() -> bool:
            s = self.state
            return s in (ConnectionState.BUILDING, ConnectionState.ARMED, ConnectionState.ACTIVE)

        cb = _FunAsrCallback(
            result_state=self._result_state,
            on_sentence_end=self._on_sentence_end,
            on_error_hook=self._mark_failed,
            is_current_hook=is_current,
            on_open_hook=self._mark_opened,
        )
        try:
            rec = Recognition(
                model=FUN_ASR_MODEL,
                format="pcm",
                sample_rate=SAMPLE_RATE,
                callback=cb,
                vocabulary_id=FUN_ASR_VOCABULARY_ID,
                heartbeat=True,
            )
            rec.start()
        except Exception:
            logger.exception("[lumi_asr] 建连失败")
            with self._lock:
                self._state = ConnectionState.FAILED
            return False

        with self._lock:
            self._recognition = rec
            self._callback = cb
            self._state = ConnectionState.ARMED
        return True

    def _mark_opened(self) -> None:
        """fun-asr 回调 on_open 触发时由 _FunAsrCallback 调用。
        这里是 SDK WebSocket 握手完成、连接真正可用的那一刻，
        arm_to_ready_ms 在此填入，比 build() 返回时测得的值更能反映云端真实建连耗时。"""
        if self._arm_start_ts <= 0:
            return
        elapsed_ms = int((time.time() - self._arm_start_ts) * 1000)
        self._arm_to_ready_ms = elapsed_ms
        self._opened_event.set()

    def _mark_failed(self) -> None:
        """SDK 回调报告错误时，把连接从可用池摘掉。"""
        with self._lock:
            if self._state != ConnectionState.RETIRED:
                self._state = ConnectionState.FAILED
        self._keepalive_stop.set()

    def wait_opened(self, timeout_sec: float) -> bool:
        """阻塞等 on_open 回调，返回是否在超时内真正打开。仅供观测/测试用。"""
        return self._opened_event.wait(timeout=timeout_sec)

    def start_keepalive(self) -> None:
        """启动后台巡检线程。

        fun-asr 对纯静音音频会返回 NO_VALID_AUDIO_ERROR，所以这里不再向识别流发送
        全零 PCM；连接层保活交给 Recognition(..., heartbeat=True)。
        """
        if self.state != ConnectionState.ARMED:
            return
        if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
            return
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            daemon=True,
            name=f"lumi_asr_keepalive_{id(self) & 0xFFFF:04x}",
        )
        self._keepalive_thread.start()

    def _keepalive_loop(self) -> None:
        while True:
            if self._keepalive_stop.wait(timeout=KEEPALIVE_INTERVAL_SEC):
                return
            if self.state != ConnectionState.ARMED:
                return
            if self.is_stale_armed():
                return
            self._keepalive_ping_count += 1

    def activate(self) -> None:
        """ARMED → ACTIVE。停止保活，清空预备期可能残留的状态。"""
        with self._lock:
            if self._state == ConnectionState.ACTIVE:
                return
            self._state = ConnectionState.ACTIVE
        self._keepalive_stop.set()
        # 预备期静音原则上不会产生识别文本，但保险清一下
        self._result_state.reset()
        self._audio_buffer.drain_all()
        self._audio_duration_sec = 0.0

    def send_audio_bytes(self, pcm_bytes: bytes) -> None:
        """ACTIVE 状态下聚合到 100ms 一批发送。非 ACTIVE 状态静默丢弃。"""
        audio_sec = (len(pcm_bytes) / 2) / SAMPLE_RATE
        self._audio_duration_sec += audio_sec
        if self.state != ConnectionState.ACTIVE:
            return
        rec = self._recognition
        if rec is None:
            return
        self._audio_buffer.append(pcm_bytes)
        while True:
            chunk = self._audio_buffer.pop_ready_chunk()
            if chunk is None:
                break
            try:
                rec.send_audio_frame(chunk)
            except Exception:
                logger.exception("[lumi_asr] 发送音频失败")
                with self._lock:
                    self._state = ConnectionState.FAILED
                return

    def wait_for_final(self, timeout_ms: int) -> tuple[str, bool]:
        """返回 (文本, 是否命中 sentence_end)。"""
        final_hit = self._result_state.wait_sentence_end(timeout_sec=timeout_ms / 1000.0)
        text = self._result_state.get_full_text()
        return text, final_hit

    def get_latest_text(self) -> str:
        return self._result_state.get_full_text()

    def retire(self) -> None:
        """关闭连接，释放 SDK 资源和保活线程。"""
        rec = None
        with self._lock:
            if self._state == ConnectionState.RETIRED:
                return
            self._state = ConnectionState.RETIRED
            rec = self._recognition
            self._recognition = None
        self._keepalive_stop.set()
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                pass


# ============ 流式 ASR 主类 ============
class StreamingAsr:
    """连接轮换架构的主类。

    用法：
        asr = StreamingAsr()
        asr.start()  # 进程启动时调用一次，首轮建连
        # 每轮 Mio 开始说话前（如 listen_for_speech 开头）：
        asr.promote_and_start_turn()
        # 说话中（每 32ms）：
        asr.push_audio(chunk_float32)
        # 本地 VAD 判完：
        readout = asr.wait_for_final(timeout_ms=500)
        if readout.degraded: ...
        # 本轮处理完：
        asr.retire_active()
        asr.arm_next()  # 异步建下一轮
    """

    def __init__(self, on_sentence_end: Optional[Callable[[str], None]] = None) -> None:
        self._on_sentence_end = on_sentence_end
        self._lock = threading.Lock()
        self._active: Optional[SingleConnection] = None
        self._next: Optional[SingleConnection] = None
        self._arm_thread: Optional[threading.Thread] = None
        self._renew_thread: Optional[threading.Thread] = None
        self._renew_stop = threading.Event()
        self._renewing_next = False
        self._started = False

        self._hour_start_ts: float = 0.0
        self._hour_audio_sec: float = 0.0

        # 最近一轮 promote_and_start_turn 的埋点
        self._last_sync_build_fallback = False
        self._last_arm_to_ready_ms = 0
        self._last_keepalive_pings = 0
        self._last_conn_ready_on_start = False

        # 进程累计重连次数（保活失败 → 放弃后下一次 arm 才重试，视为一次"重连"）
        self._reconnect_count = 0

    # ---- 生命周期 ----

    def start(self) -> None:
        """进程启动时调用。

        立刻建首条预备连接 + 启动后台续租循环，使 Mio 真正开口时升级走的是热连接、
        不付建连延迟。预备连接被服务端判空闲超时（NO_VALID_AUDIO_ERROR）是预期行为，
        续租循环会先建新再替换旧；该错误在主日志中已被静默到 debug 级别。
        """
        if self._started:
            return
        dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY")
        dashscope.base_websocket_api_url = FUN_ASR_BASE_URL
        _install_dashscope_log_filter()
        self._started = True
        self.arm_next()
        self._renew_stop.clear()
        self._renew_thread = threading.Thread(
            target=self._renew_loop,
            daemon=True,
            name="lumi_asr_renew_loop",
        )
        self._renew_thread.start()

    def is_ready(self) -> bool:
        """流式 ASR 模块是否已初始化。实际云端任务在用户开口后按需创建。"""
        if self._started:
            return True
        with self._lock:
            active = self._active
            nxt = self._next
        if active is not None and active.is_usable():
            return True
        if nxt is not None and nxt.is_usable():
            return True
        return False

    def stop(self) -> None:
        if not self._started:
            return
        with self._lock:
            active = self._active
            next_conn = self._next
            self._active = None
            self._next = None
        self._renew_stop.set()
        if active is not None:
            active.retire()
        if next_conn is not None:
            next_conn.retire()
        self._started = False

    def arm_next(self) -> None:
        """异步建下一条预备连接。若预备已存在则忽略。"""
        stale_next = None
        arm_thread = None
        with self._lock:
            if self._next is not None:
                if self._next.is_usable() and not self._next.is_stale_armed():
                    return
                stale_next = self._next
                self._next = None
            if self._arm_thread is not None and self._arm_thread.is_alive():
                arm_thread = None
            else:
                self._arm_thread = threading.Thread(
                    target=self._arm_worker,
                    daemon=True,
                    name="lumi_asr_arm_next",
                )
                arm_thread = self._arm_thread
        if stale_next is not None:
            stale_next.retire()
        if arm_thread is not None:
            arm_thread.start()

    def _renew_loop(self) -> None:
        """后台续租预备连接，避免 ARMED 空挂到服务端任务超时。

        续租采用先建新连接、再替换旧连接的顺序，尽量保持开口时总有热连接可用。
        """
        while not self._renew_stop.wait(timeout=1.0):
            self._renew_next_if_needed()

    def _renew_next_if_needed(self) -> None:
        with self._lock:
            nxt = self._next
            if nxt is None:
                need_renew = True
            elif not nxt.is_usable():
                need_renew = True
            else:
                need_renew = nxt.is_stale_armed()
            if not need_renew or self._renewing_next:
                return
            self._renewing_next = True
        threading.Thread(
            target=self._renew_worker,
            daemon=True,
            name="lumi_asr_renew_next",
        ).start()

    def _renew_worker(self) -> None:
        old_next = None
        try:
            conn = SingleConnection(on_sentence_end=self._on_sentence_end)
            ok = conn.build()
            if not ok:
                logger.warning("[lumi_asr] 预备连接续租失败，保留旧连接等待下次重试")
                self._reconnect_count += 1
                return
            conn.start_keepalive()
            with self._lock:
                old_next = self._next
                self._next = conn
            if old_next is not None:
                old_next.retire()
            self._reconnect_count += 1
        finally:
            with self._lock:
                self._renewing_next = False

    def _arm_worker(self) -> None:
        conn = SingleConnection(on_sentence_end=self._on_sentence_end)
        ok = conn.build()
        if ok:
            conn.start_keepalive()
            with self._lock:
                self._next = conn
        else:
            logger.warning("[lumi_asr] 预备连接建连失败，下轮 promote 会走同步降级")

    def promote_and_start_turn(self) -> None:
        """升级预备为活跃。未就绪时同步等最多 PROMOTE_SYNC_TIMEOUT_SEC；仍未就绪则同步建连。

        填充以下埋点：
          _last_conn_ready_on_start / _last_sync_build_fallback
          _last_arm_to_ready_ms / _last_keepalive_pings
        """
        self._last_sync_build_fallback = False
        self._last_conn_ready_on_start = False
        self._last_arm_to_ready_ms = 0
        self._last_keepalive_pings = 0

        # 先关掉上一轮的活跃（如果还没 retire）
        with self._lock:
            prev_active = self._active
            self._active = None
        if prev_active is not None and prev_active.state != ConnectionState.RETIRED:
            prev_active.retire()

        # 等预备就绪
        conn: Optional[SingleConnection] = None
        stale_candidates: list[SingleConnection] = []
        deadline = time.time() + PROMOTE_SYNC_TIMEOUT_SEC
        checked_once = False
        while True:
            with self._lock:
                candidate = self._next
                if candidate is not None:
                    if candidate.is_usable() and not candidate.is_stale_armed():
                        conn = candidate
                        self._next = None
                        break
                    stale_candidates.append(candidate)
                    self._next = None
            if not checked_once:
                checked_once = True
                # 第一次检查就命中的话，记为"一进来就好"
                # 这里不命中，说明要等——进入 polling
            if time.time() >= deadline:
                break
            time.sleep(0.05)
        for stale in stale_candidates:
            stale.retire()

        if conn is not None:
            self._last_conn_ready_on_start = True
            self._last_arm_to_ready_ms = conn.arm_to_ready_ms
            self._last_keepalive_pings = conn.keepalive_ping_count
            conn.activate()
            with self._lock:
                self._active = conn
            return

        # 预备没来——同步建一条
        logger.warning("[lumi_asr] 预备未就绪，走同步建连降级")
        self._last_sync_build_fallback = True
        sync_conn = SingleConnection(on_sentence_end=self._on_sentence_end)
        if not sync_conn.build():
            logger.error("[lumi_asr] 同步建连也失败，本轮只能走本地降级")
            self._reconnect_count += 1
            return
        sync_conn.activate()
        self._last_arm_to_ready_ms = sync_conn.arm_to_ready_ms
        with self._lock:
            self._active = sync_conn

    def retire_active(self) -> None:
        """关闭当前活跃连接。"""
        with self._lock:
            active = self._active
            self._active = None
        if active is not None:
            active.retire()

    # ---- 音频和结果 ----

    def push_audio(self, audio: np.ndarray) -> None:
        """推一块 float32 音频到当前活跃连接。无活跃连接时仅累计音频时长用于降级判断。"""
        pcm_bytes = _float32_to_int16_bytes(audio)
        audio_sec = (len(pcm_bytes) / 2) / SAMPLE_RATE
        now = time.time()
        if self._hour_start_ts == 0.0:
            self._hour_start_ts = now
        if now - self._hour_start_ts >= 3600.0:
            self._hour_start_ts = now
            self._hour_audio_sec = 0.0
        self._hour_audio_sec += audio_sec
        active = self._active
        if active is None:
            return
        active.send_audio_bytes(pcm_bytes)

    def get_latest_text(self) -> AsrReadout:
        """即时读当前活跃连接的识别文本。无活跃连接 → degraded=True。"""
        active = self._active
        if active is None or not active.is_active():
            return AsrReadout(text="", degraded=True, audio_duration_sec=0.0, final_hit=False)
        return AsrReadout(
            text=active.get_latest_text(),
            degraded=False,
            audio_duration_sec=active.audio_duration_sec,
            final_hit=False,
        )

    def wait_for_final(self, timeout_ms: int) -> AsrReadout:
        """阻塞等 sentence_end 到来或超时。"""
        active = self._active
        if active is None or not active.is_active():
            return AsrReadout(text="", degraded=True, audio_duration_sec=0.0, final_hit=False)
        text, final_hit = active.wait_for_final(timeout_ms)
        return AsrReadout(
            text=text,
            degraded=False,
            audio_duration_sec=active.audio_duration_sec,
            final_hit=final_hit,
        )

    # ---- 埋点属性 ----

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def intermediate_count(self) -> int:
        active = self._active
        return active.intermediate_count if active is not None else 0

    @property
    def hour_audio_sec(self) -> float:
        return self._hour_audio_sec

    @property
    def last_conn_ready_on_start(self) -> bool:
        """最近一轮 promote 时预备是否已就绪（未走同步降级）。"""
        return self._last_conn_ready_on_start

    @property
    def last_sync_build_fallback(self) -> bool:
        return self._last_sync_build_fallback

    @property
    def last_arm_to_ready_ms(self) -> int:
        return self._last_arm_to_ready_ms

    @property
    def last_keepalive_pings(self) -> int:
        return self._last_keepalive_pings

    def is_next_ready(self) -> bool:
        """下一轮预备连接是否已就绪（ARMED）。"""
        with self._lock:
            nxt = self._next
        return nxt is not None and nxt.is_usable()

    def is_sentence_end_fired(self) -> bool:
        """非阻塞查询：当前活跃连接本轮是否已有 sentence_end 到达。
        用于主循环早停——一旦云端推完最终句，立即停止等 VAD 超时。"""
        active = self._active
        if active is None or not active.is_active():
            return False
        return active.result_state.is_sentence_end_set()
