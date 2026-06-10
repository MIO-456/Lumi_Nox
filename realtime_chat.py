"""端到端实时语音大模型（豆包 SC2.0）协议适配 + 会话管理。

职责：
- 维护一条端到端 WebSocket 长连接（StartConnection + StartSession）
- 麦克风 PCM 持续推送（仅 CHATTING 状态）
- SayHello / ChatTextQuery 接口（带 task_id 返回）
- 服务端事件 → 现有事件总线（user_speech_done / reply / tts_segment_done / tts_done / realtime_error）
- 音频回流 → 虚拟声卡 + 监听设备 + AEC 参考缓冲 + 字幕推送

外部依赖通过 init() 注入，无反向 import。
"""
import os
import re
import time
import uuid
import asyncio
import threading
import queue
import gzip
import json
from typing import Optional, Callable, Any
from contextlib import contextmanager
from collections import deque
from threading import Event, Lock, RLock

import websockets
from websockets.exceptions import InvalidStatus

import realtime_chat_protocol as proto

# ===== 终端颜色（自包含，避免反向依赖 lumi.py）=====
C_RT = "\033[38;5;81m"     # 浅蓝 — realtime_chat
C_ERR = "\033[31m"
C_RESET = "\033[0m"

# ===== 端到端服务配置 =====
WS_BASE_URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"
RESOURCE_ID = "volc.speech.dialog"
APP_KEY = "PlgvMymc7f3tQnJ6"  # 公开 App Key（端到端 SC2.0）

# ===== 模块级单例 =====
_log_fn: Callable = lambda msg: print(msg, flush=True)
_pa_instance = None         # pyaudio.PyAudio
_event_bus = None           # event_bus.EventBus

# 归因诊断日志开关。正常路径（enqueue / pair / finalize / clear / 553 stash）密度高、
# 默认不打。要排查归因体系问题时改为 True 重启即可。异常路径（fifo 兜底 finalize、
# 找不到待结 task）始终打，出现即异常。
_DEBUG_ATTRIBUTION = False
# 字幕策略放业务层（lumi.py 订阅 tts_segment_start → lumi_tts 字幕光标 worker），transport
# 层不再直接广播字幕，所以这里也没有 subtitle_broadcast_fn 注入了。
DEFAULT_END_SMOOTH_WINDOW_MS = int(os.getenv("REALTIME_END_SMOOTH_WINDOW_MS", "2500"))
_WEBSEARCH_TYPES = {"web", "web_summary", "web_agent"}
_websearch_missing_key_warned = False
_websearch_invalid_type_warned = False
_websearch_missing_bot_id_warned = False
_websearch_log_lock = Lock()

# ===== 鉴权 =====
APP_ID = ""
ACCESS_KEY = ""


def init(*, log_fn: Callable, pa_instance, event_bus,
         app_id: str, access_key: str):
    """直播启动时由 lumi.py 调用一次。注入运行时依赖。"""
    global _log_fn, _pa_instance, _event_bus
    global APP_ID, ACCESS_KEY
    global _sessions, _speaker_output_devices, _default_speaker, _active_speaker
    global _user_audio_active, _cancelled_task_ids, _cancelled_reply_ids
    global _cancelled_question_ids, _cancelled_unpaired_response_slots
    global _drop_incoming_audio, _pending_asr_question_id
    _log_fn = log_fn
    _pa_instance = pa_instance
    _event_bus = event_bus
    APP_ID = app_id
    ACCESS_KEY = access_key
    _sessions = {}
    _speaker_output_devices = {}
    _default_speaker = "Lumi"
    _active_speaker = "Lumi"
    _user_audio_active = False
    _cancelled_task_ids = set()
    _cancelled_reply_ids = set()
    _cancelled_question_ids = set()
    _cancelled_unpaired_response_slots = 0
    _drop_incoming_audio = False
    _pending_asr_question_id = None
    _log_fn(f"{C_RT}[realtime_chat] 模块初始化完成 app_id={app_id[:8]}...{C_RESET}")


from dataclasses import dataclass, field


@dataclass
class SessionConfig:
    character_manifest: str = ""
    voice_id: str = ""
    end_smooth_window_ms: int = DEFAULT_END_SMOOTH_WINDOW_MS
    sample_rate_in: int = 16000
    sample_rate_out: int = 24000


@dataclass
class SessionRuntime:
    """One realtime SC2.0 session bound to one character."""
    speaker: str
    config: SessionConfig
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ws: Any = None
    ws_thread: Optional[threading.Thread] = None
    ws_loop: Optional[asyncio.AbstractEventLoop] = None
    ws_alive: bool = False
    audio_in_queue: queue.Queue = field(default_factory=queue.Queue)
    monitor_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=200))
    aec_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=200))
    output_stream: Any = None
    monitor_stream: Any = None
    player_thread: Optional[threading.Thread] = None
    monitor_thread: Optional[threading.Thread] = None
    aec_thread: Optional[threading.Thread] = None
    player_running: bool = False
    unconfirmed_tasks: deque = field(default_factory=deque)
    task_id_to_reply_id: dict = field(default_factory=dict)
    reply_id_to_task: dict = field(default_factory=dict)
    question_id_to_task: dict = field(default_factory=dict)
    cancelled_task_ids: set = field(default_factory=set)
    cancelled_reply_ids: set = field(default_factory=set)
    cancelled_question_ids: set = field(default_factory=set)
    cancelled_unpaired_response_slots: int = 0
    drop_incoming_audio: bool = False
    pending_asr_question_id: str | None = None
    last_speech_done_at: float = 0.0
    response_audio_bytes: int = 0
    response_started_at: float = 0.0
    response_tts_types: set = field(default_factory=set)
    chat_published_chars: int = 0
    last_response_full_text: str = ""
    chat_buffer: str = ""
    asr_final_buffer: str = ""
    last_user_query: str = ""
    # 静默卡顿探测用：永久 per-session 字段，不参与 runtime_context 的 global swap。
    # last_inject_at：最后一次发出 inject/say（期待服务端回应）的时刻。
    # last_server_event_at：最后一次从服务端收到任何数据（事件/音频帧）的时刻。
    # 卡顿判据 = inject 发出后超过阈值、且这期间 last_server_event_at 没更新过。
    last_inject_at: float = 0.0
    last_server_event_at: float = 0.0


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _build_websearch_extra_fields() -> dict:
    """Build Volcengine built-in web search config for every realtime session."""
    global _websearch_missing_key_warned, _websearch_invalid_type_warned, _websearch_missing_bot_id_warned

    if not _env_bool("REALTIME_ENABLE_WEBSEARCH", True):
        return {}

    api_key = os.getenv("VOLC_WEBSEARCH_API_KEY") or os.getenv("REALTIME_WEBSEARCH_API_KEY")
    if not api_key:
        if not _websearch_missing_key_warned:
            _log_fn(
                f"{C_RT}[realtime_chat] 内置联网未启用：缺少 VOLC_WEBSEARCH_API_KEY "
                f"或 REALTIME_WEBSEARCH_API_KEY{C_RESET}"
            )
            _websearch_missing_key_warned = True
        return {}

    search_type = (
        os.getenv("VOLC_WEBSEARCH_TYPE")
        or os.getenv("REALTIME_WEBSEARCH_TYPE")
        or "web_summary"
    ).strip()
    if search_type not in _WEBSEARCH_TYPES:
        if not _websearch_invalid_type_warned:
            _log_fn(
                f"{C_RT}[realtime_chat] 未知联网搜索类型 {search_type!r}，已回退 web_summary{C_RESET}"
            )
            _websearch_invalid_type_warned = True
        search_type = "web_summary"

    fields = {
        "enable_volc_websearch": True,
        "volc_websearch_type": search_type,
        "volc_websearch_api_key": api_key,
        "volc_websearch_result_count": _env_int(
            "VOLC_WEBSEARCH_RESULT_COUNT",
            _env_int("REALTIME_WEBSEARCH_RESULT_COUNT", 5, minimum=1, maximum=10),
            minimum=1,
            maximum=10,
        ),
        "volc_websearch_no_result_message": os.getenv(
            "VOLC_WEBSEARCH_NO_RESULT_MESSAGE",
            os.getenv("REALTIME_WEBSEARCH_NO_RESULT_MESSAGE", "我没搜到靠谱结果，别硬编。"),
        ),
    }

    if search_type == "web_agent":
        bot_id = os.getenv("VOLC_WEBSEARCH_BOT_ID") or os.getenv("REALTIME_WEBSEARCH_BOT_ID")
        if bot_id:
            fields["volc_websearch_bot_id"] = bot_id
        elif not _websearch_missing_bot_id_warned:
            _log_fn(
                f"{C_RT}[realtime_chat] web_agent 模式缺少 VOLC_WEBSEARCH_BOT_ID，"
                f"服务端可能拒绝 StartSession{C_RESET}"
            )
            _websearch_missing_bot_id_warned = True

    return fields


def _write_websearch_trace(entry: dict) -> None:
    path = os.getenv("REALTIME_WEBSEARCH_LOG_PATH", os.path.join("logs", "realtime_websearch.jsonl"))
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        **entry,
    }
    try:
        log_dir = os.path.dirname(path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with _websearch_log_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        _log_fn(f"{C_ERR}[realtime_chat] 联网搜索日志写入失败: {e}{C_RESET}")


_session_config: Optional[SessionConfig] = None
_ws = None
_ws_thread: Optional[threading.Thread] = None
_ws_loop: Optional[asyncio.AbstractEventLoop] = None
_ws_alive: bool = False  # ws 当前是否真正可用（连上 + 服务端没主动断）
_session_id: str = ""
_sessions: dict[str, SessionRuntime] = {}
_default_speaker: str = "Lumi"
_active_speaker: str = "Lumi"
_speaker_output_devices: dict[str, int] = {}
_runtime_lock = RLock()


def get_session_config() -> SessionConfig:
    return _session_config


def _resolve_speaker(speaker: str = None) -> str:
    if speaker:
        return speaker
    if _active_speaker in _sessions:
        return _active_speaker
    if _default_speaker in _sessions:
        return _default_speaker
    if _sessions:
        return next(iter(_sessions))
    return _default_speaker


def get_pool() -> dict[str, SessionRuntime]:
    return _sessions


def get_active_speaker() -> str:
    return _resolve_speaker()


def is_connected(speaker: str = None) -> bool:
    """Return whether the speaker's realtime websocket can send frames."""
    sp = _resolve_speaker(speaker)
    rt = _sessions.get(sp)
    if rt is not None:
        return bool(rt.ws_alive and rt.ws is not None and rt.ws_loop is not None)
    return bool(_ws_alive and _ws is not None and _ws_loop is not None)


def wait_until_connected(speaker: str = None, timeout_s: float = 3.0) -> bool:
    """Wait briefly for StartSession handshake before sending injected text."""
    deadline = time.time() + max(0.0, timeout_s)
    while time.time() < deadline:
        if is_connected(speaker):
            return True
        time.sleep(0.05)
    return is_connected(speaker)


def _store_runtime_globals(speaker: str):
    """事件处理结束时把"per-response 临时状态"从全局存回 rt。

    **重要**：永远不写 per-session 永久字段（config / session_id / ws / ws_thread /
    ws_loop / ws_alive / audio_in_queue / monitor_queue / aec_queue / 归因状态）——
    这些字段在 start_session 时已经被显式赋值（rt.ws=ws 等），如果在这里再用全局
    值反向覆盖，会触发严重 race condition：

    bug 重现路径（dtd_test 没有这套 swap，所以 OK；主代码有，所以坏）：
      1. start_session(Lumi) → rt[Lumi].ws = Lumi_ws ✓ 全局 _ws = Lumi_ws
      2. start_session(Nox) → rt[Nox].ws = Nox_ws ✓ 全局 _ws = Nox_ws（覆盖）
      3. Lumi 收事件 → _runtime_context(Lumi) enter: load → 全局 _ws = rt[Lumi].ws
         事件处理（有跨线程动作）
         exit: store → rt[Lumi].ws = 全局 _ws —— 如果这一刻 Nox 的 ws 初始化
         coroutine 在另一线程把全局 _ws 改成 Nox_ws，rt[Lumi].ws 被错误覆盖成 Nox_ws
      4. mic_pump 推帧给 Lumi (active=Lumi) → _send_frame_threadsafe → rt[Lumi].ws
         → 实际拿到 Nox_ws → 帧被推到 Nox session
      5. 用户语音被 Nox session 处理 → reply 来自 Nox（payload speaker=Nox）
         但 active 是 Lumi → 调度器混乱 / 两条 session 都在响应

    历史教训 + 累积修复（2026-05-09 → 05-10）：
      097e391: 摘除 last_speech_done_at（process 级状态）
      本次：摘除所有 per-session 永久字段 —— 让 rt 永远是真值
    """
    rt = _sessions.get(speaker)
    if rt is None:
        return
    # per-session 永久字段（config/session_id/ws/queue 等）已经在 start_session
    # 时显式设上，永远不在这里覆盖。
    # 归因状态（unconfirmed_tasks / task_id_to_reply_id 等）也是 per-session 的，
    # 同样不在这里覆盖（它们需要全局变量是因为模块级事件处理代码直接读写，
    # 但持久化由 _load 一次性同步即可，不需要再 store 回去）。

    # 仅 store"per-response 临时状态"——这些是当前一轮响应进行中的累积值，
    # 退出 _runtime_context 时必须 sync 回 rt 让下次 enter 能 load 正确状态。
    rt.response_audio_bytes = _response_audio_bytes
    rt.response_started_at = _response_started_at
    rt.response_tts_types = _response_tts_types
    rt.chat_published_chars = _chat_published_chars
    rt.last_response_full_text = _last_response_full_text
    rt.chat_buffer = _chat_buffer
    rt.asr_final_buffer = _asr_final_buffer
    rt.last_user_query = _last_user_query
    # 归因状态对象（dict / deque）是引用，全局和 rt 指向同一个对象 ——
    # 任何一边改都自动反映到另一边，不需要显式 store。但首次 _load 后
    # 全局变量已经指向 rt 字段所引用的同一对象，所以全程一致。
    rt.unconfirmed_tasks = _unconfirmed_tasks
    rt.task_id_to_reply_id = _task_id_to_reply_id
    rt.reply_id_to_task = _reply_id_to_task
    rt.question_id_to_task = _question_id_to_task
    rt.cancelled_task_ids = _cancelled_task_ids
    rt.cancelled_reply_ids = _cancelled_reply_ids
    rt.cancelled_question_ids = _cancelled_question_ids
    rt.cancelled_unpaired_response_slots = _cancelled_unpaired_response_slots
    rt.drop_incoming_audio = _drop_incoming_audio
    rt.pending_asr_question_id = _pending_asr_question_id


def _load_runtime_globals(speaker: str):
    global _session_config, _session_id, _ws, _ws_thread, _ws_loop, _ws_alive
    global _audio_in_queue, _monitor_queue, _aec_queue
    global _unconfirmed_tasks, _task_id_to_reply_id, _reply_id_to_task, _question_id_to_task
    global _cancelled_task_ids, _cancelled_reply_ids, _cancelled_question_ids
    global _cancelled_unpaired_response_slots, _drop_incoming_audio, _pending_asr_question_id
    global _response_audio_bytes, _response_started_at, _response_tts_types
    global _chat_published_chars, _last_response_full_text, _chat_buffer, _asr_final_buffer
    global _last_user_query
    rt = _sessions.get(speaker)
    if rt is None:
        return
    _session_config = rt.config
    _session_id = rt.session_id
    _ws = rt.ws
    _ws_thread = rt.ws_thread
    _ws_loop = rt.ws_loop
    _ws_alive = rt.ws_alive
    _audio_in_queue = rt.audio_in_queue
    _monitor_queue = rt.monitor_queue
    _aec_queue = rt.aec_queue
    _unconfirmed_tasks = rt.unconfirmed_tasks
    _task_id_to_reply_id = rt.task_id_to_reply_id
    _reply_id_to_task = rt.reply_id_to_task
    _question_id_to_task = rt.question_id_to_task
    _cancelled_task_ids = rt.cancelled_task_ids
    _cancelled_reply_ids = rt.cancelled_reply_ids
    _cancelled_question_ids = rt.cancelled_question_ids
    _cancelled_unpaired_response_slots = rt.cancelled_unpaired_response_slots
    _drop_incoming_audio = rt.drop_incoming_audio
    _pending_asr_question_id = rt.pending_asr_question_id
    # _last_speech_done_at 不在这里 load —— 见 _store_runtime_globals 注释
    _response_audio_bytes = rt.response_audio_bytes
    _response_started_at = rt.response_started_at
    _response_tts_types = rt.response_tts_types
    _chat_published_chars = rt.chat_published_chars
    _last_response_full_text = rt.last_response_full_text
    _chat_buffer = rt.chat_buffer
    _asr_final_buffer = rt.asr_final_buffer
    _last_user_query = rt.last_user_query


@contextmanager
def _runtime_context(speaker: str = None):
    sp = _resolve_speaker(speaker)
    if sp not in _sessions:
        yield sp
        return
    with _runtime_lock:
        _load_runtime_globals(sp)
        try:
            yield sp
        finally:
            _store_runtime_globals(sp)


def _build_start_session_payload(cfg) -> dict:
    """组装 StartSession (event 100) 的 JSON payload。
    抽出来独立函数便于单测验证关键字段（如 input_mod=keep_alive）。"""
    dialog_extra = {
        "strict_audit": False,
        # keep_alive 模式：麦克风可静音（pause 期间不推 PCM 帧），服务端不会因长时间
        # 不收音频触发 idle timeout。替代手动推零 PCM 帧的偏方做法，端到端官方姿势。
        "input_mod": "keep_alive",
        "model": "2.2.0.0",  # SC2.0
    }
    dialog_extra.update(_build_websearch_extra_fields())

    return {
        "asr": {"extra": {"end_smooth_window_ms": cfg.end_smooth_window_ms}},
        "tts": {
            "speaker": cfg.voice_id,
            "audio_config": {
                "channel": 1,
                "format": "pcm",
                "sample_rate": cfg.sample_rate_out,
            },
        },
        "dialog": {
            "character_manifest": cfg.character_manifest,
            "extra": dialog_extra,
        },
    }


# ===== 静默卡顿探测 =====
# 现象：服务端对某些 inject 完全静默——不报错、不返回音频，干等到客户端 60s 保守
# 上限到期才放行。正常情况下 inject 后 1s 内服务端就持续回事件，所以"正期待说话
# 但服务端连续 N 秒无任何数据"就是静默卡顿。本探测器只打日志、不改任何状态。
SILENT_STALL_WARN_SECONDS = float(os.getenv("REALTIME_SILENT_STALL_WARN_SECONDS", "8"))
_stall_watcher_started = False
_stall_warn_anchor = 0.0  # 去重：同一段静默只告警一次（锚定该段静默起点）


def _silent_stall_watcher():
    global _stall_warn_anchor
    while True:
        try:
            time.sleep(2.0)
            now = time.time()
            sp = _active_speaker
            rt = _sessions.get(sp)
            if rt is None or not rt.ws_alive:
                continue
            # 卡顿判据：发过 inject、且 inject 之后服务端一直没回数据。
            # 用 inject 时刻而非"距上次数据"——后者会把 inject 之前的正常待机静默
            # 误算进来（5/30 实测 43 次误报全是这个原因）。
            if rt.last_inject_at <= 0:
                continue
            if rt.last_server_event_at >= rt.last_inject_at:
                continue  # inject 之后已经收到过服务端回应 → 正常
            silent_for = now - rt.last_inject_at
            if silent_for < SILENT_STALL_WARN_SECONDS:
                continue
            if _stall_warn_anchor == rt.last_inject_at:
                continue  # 这次 inject 的静默已告警过
            _stall_warn_anchor = rt.last_inject_at
            try:
                qsize = rt.audio_in_queue.qsize()
            except Exception:
                qsize = -1
            _log_fn(
                f"{C_ERR}[realtime_chat·静默卡顿] 角色={sp} inject 发出后服务端已连续 "
                f"{silent_for:.0f}s 无任何响应，ws_alive={rt.ws_alive} audio_queue={qsize}。"
                f"判读：ws 活着且无后续断线=服务端/账号侧静默丢弃；"
                f"若紧随其后出现 ws 断线/重连=网络链路问题{C_RESET}"
            )
        except Exception:
            pass


def _ensure_stall_watcher():
    global _stall_watcher_started
    if _stall_watcher_started:
        return
    _stall_watcher_started = True
    threading.Thread(target=_silent_stall_watcher, daemon=True,
                     name="silent_stall_watcher").start()


def start_session(*, character_manifest: str = None, voice_id: str = None,
                  end_smooth_window_ms: int = DEFAULT_END_SMOOTH_WINDOW_MS, speaker: str = None,
                  sessions: dict[str, dict] = None,
                  active_speakers: list[str] = None):
    """启动端到端会话。

    兼容旧调用：不传 speaker 时启动默认单 session。
    新调用：传 sessions + active_speakers 时按角色启动 session pool。

    多角色路径下会在所有 session 起好之后，对每条 session 跑一遍 prime（发一个
    inject_text 然后立刻 discard），把服务端 session 状态从"只 sync 过 history"
    推进到"能接 ChatTTSText 的态"——否则某些跳过节目单的极端流程下，没被 inject 过
    的 session 发 500 ChatTTSText 服务端会静默吞掉（2026-05-16 实测）。
    """
    global _session_config, _session_id, _default_speaker, _active_speaker
    if sessions is not None:
        order = active_speakers or list(sessions.keys())
        for idx, sp in enumerate(order):
            spec = sessions[sp]
            start_session(
                speaker=sp,
                character_manifest=spec["character_manifest"],
                voice_id=spec["voice_id"],
                end_smooth_window_ms=spec.get("end_smooth_window_ms", end_smooth_window_ms),
            )
            if idx == 0:
                _default_speaker = sp
                _active_speaker = sp
        for sp in order:
            prime_session(sp)
        return

    sp = speaker or _default_speaker
    cfg = SessionConfig(
        character_manifest=character_manifest,
        voice_id=voice_id,
        end_smooth_window_ms=end_smooth_window_ms,
    )
    rt = SessionRuntime(speaker=sp, config=cfg)
    _sessions[sp] = rt
    _session_config = cfg
    _session_id = rt.session_id
    if len(_sessions) == 1:
        _default_speaker = sp
        _active_speaker = sp
    _connect_async(speaker=sp)  # 异步任务由 _spawn_ws_loop 启动
    _ensure_stall_watcher()  # 启动静默卡顿探测（仅首次生效）


def prime_session(speaker: str, wait_timeout: float = 5.0):
    """对指定 session 跑一遍"激活流程"：发一条 inject_text 然后立刻 discard。

    Why：服务端 session 起来之后，如果没有走过一次完整的 501→553→550→559 LLM
    循环就直接发 500 ChatTTSText，服务端会静默吞掉不响应、不报错。表现是字幕
    出（lumi_tts 在客户端本地推的）但 TTS 不出声。

    本函数发一个无关紧要的引导文本进 inject_text，让服务端走完一次完整循环把
    session 状态推进到位；返回的 task_id 立刻 discard 掉，配套的 553/550/559/
    audio 帧会被现有的 cancellation/drop 逻辑全部吞掉，**不会真的发声**也不会
    污染历史。
    """
    if not wait_until_connected(speaker, timeout_s=wait_timeout):
        _log_fn(f"{C_ERR}[realtime_chat] prime_session {speaker} timeout, skip{C_RESET}")
        return
    # RLock 保证 inject+discard 这段对全局态的修改不会被其他线程的 _runtime_context 切走
    with _runtime_context(speaker) as sp:
        try:
            task_id = inject_text("准备开始直播。", speaker=sp)
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat] prime_session {sp} inject 失败: {e}{C_RESET}")
            return
        if not task_id:
            return
        discard_task(task_id)
        # inject_text 给了 60s 的 speech_done 上限保守占用，discard 后归零，否则
        # PROACTIVE 兜底会在初始化后被卡 60s 不能主动说话。
        _set_speech_done_at(time.time())
        _log_fn(f"{C_RT}[realtime_chat] {sp} session primed (task={task_id} discarded){C_RESET}")


async def _ws_connect_and_handshake(speaker: str = None):
    """建立 ws + 走 StartConnection (1) + StartSession (100)。"""
    global _ws, _session_id
    sp = _resolve_speaker(speaker)
    rt = _sessions.get(sp)
    cfg = rt.config if rt is not None else _session_config
    sid = rt.session_id if rt is not None else _session_id
    headers = {
        "X-Api-App-ID": APP_ID,
        "X-Api-Access-Key": ACCESS_KEY,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-App-Key": APP_KEY,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }
    try:
        ws = await websockets.connect(
            WS_BASE_URL,
            additional_headers=headers,
            ping_interval=None,
            proxy=None,
        )
    except InvalidStatus as e:
        _log_fn(f"{C_ERR}[realtime_chat] 握手失败 HTTP {e.response.status_code}{C_RESET}")
        raise

    # StartConnection (event 1)
    frame = proto.build_event_frame_no_session(event_id=1, payload={})
    await ws.send(frame)
    raw = await ws.recv()
    resp = proto.parse_response(raw)
    if resp.get("event") != 50:
        raise RuntimeError(f"StartConnection 没收到 50: {resp}")

    # StartSession (event 100)
    session_payload = _build_start_session_payload(cfg)
    frame = proto.build_event_frame(event_id=100, session_id=sid, payload=session_payload)
    await ws.send(frame)
    raw = await ws.recv()
    resp = proto.parse_response(raw)
    if resp.get("event") != 150:
        raise RuntimeError(f"StartSession 没收到 150: {resp}")
    global _ws_alive
    if rt is not None:
        rt.ws = ws
        rt.ws_alive = True
        rt.session_id = sid
    _ws = ws
    _session_id = sid
    _ws_alive = True
    _log_fn(f"{C_RT}[realtime_chat] {sp} SessionStarted, dialog_id={resp.get('payload_msg', {}).get('dialog_id')}{C_RESET}")


def _connect_async(speaker: str = None):
    """启动后台事件循环 + ws 连接 + 接收循环。"""
    global _ws_loop, _ws_thread
    sp = _resolve_speaker(speaker)
    rt = _sessions.get(sp)

    def _run_loop():
        global _ws_loop
        loop = asyncio.new_event_loop()
        if rt is not None:
            rt.ws_loop = loop
        _ws_loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_ws_connect_and_handshake(speaker=sp))
            loop.run_until_complete(_recv_loop(speaker=sp))
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat] ws 线程异常退出: {e}{C_RESET}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    thread = threading.Thread(target=_run_loop, daemon=True, name=f"realtime-chat-ws-{sp}")
    if rt is not None:
        rt.ws_thread = thread
    _ws_thread = thread
    thread.start()


# ===== task_id / say / inject_text =====
_task_id_counter = 0
_task_id_lock = Lock()
# task_id → metadata（source: "say"/"inject"/"auto_chat", text, created_at）
_task_meta: dict = {}

# 归因体系（reply_id-based attribution）—— 端到端 SC2.0 协议每个 TTS / Chat 事件都带
# server-side reply_id，客户端 task_id 与服务端 reply_id 的映射靠下面这套数据结构维护。
#
# 三类响应入口都生成本地 task_id 并入"未配对队列"_unconfirmed_tasks 队尾：
#   - say()         (300 SayHello)        没 ack；只能等第一次 350/559 看到 reply_id 时拿队首做配对
#   - inject_text() (501 ChatTextQuery)   有 ack 553；收到 553 时把 question_id → task_id 暂存，等 350/559 拿 reply_id
#   - 459 ASREnded  自动开 auto_chat 窗口  与 inject 同样走 553 + 350/559 路径（实际只有 350/559 来配对）
#
# 配对完成（写入 _reply_id_to_task / _task_id_to_reply_id）后，后续所有该 reply_id 事件
# 都直接查表归因；359 TTSEnded 时按 reply_id 弹出本次记录。
#
# FIFO 假设的安全性：仅在"unconfirmed 队首兜底"那一步用 FIFO 假设，且仅对 SayHello
# 这种没 ack 的请求安全使用——一旦拿到 reply_id 立刻按 reply_id 归因，窗口很短。
_unconfirmed_tasks: deque = deque()      # items: (task_id, source) — 还没拿到 reply_id 的客户端 task
_task_id_to_reply_id: dict = {}          # task_id → reply_id
_reply_id_to_task: dict = {}             # reply_id → (task_id, source)
_question_id_to_task: dict = {}          # question_id → (task_id, source)，553 ack 暂存
_cancelled_task_ids: set[str] = set()
_cancelled_reply_ids: set[str] = set()
_cancelled_question_ids: set[str] = set()
_cancelled_unpaired_response_slots: int = 0
_drop_incoming_audio: bool = False
_pending_asr_question_id: str | None = None
_attribution_lock = Lock()


def _attribution_snapshot_unsafe() -> str:
    """打日志用：当前 unconfirmed 队列与已配对映射的简短描述（调用方已持锁）。"""
    unc = [f"{tid}|{src}" for tid, src in _unconfirmed_tasks]
    if len(unc) > 6:
        unc = unc[:3] + [f"...({len(unc)-5}省略)..."] + unc[-2:]
    return f"unconfirmed=[{', '.join(unc)}] paired={len(_reply_id_to_task)}"


def _enqueue_unconfirmed(task_id: str, source: str):
    """新响应入未配对队尾。"""
    with _attribution_lock:
        _unconfirmed_tasks.append((task_id, source))
        if _DEBUG_ATTRIBUTION:
            _log_fn(
                f"[时序·归因] enqueue task={task_id} source={source} "
                f"{_attribution_snapshot_unsafe()}"
            )


def _stash_question_id(question_id: str, task_id: str, source: str):
    """553 ChatTextQueryConfirmed 收到时调用：把 question_id 与 task_id 关联，等
    350/559 拿到 reply_id 再做正式配对。同时从 _unconfirmed_tasks 里把这条 task pop。"""
    with _attribution_lock:
        _question_id_to_task[question_id] = (task_id, source)


def _peek_or_pair(question_id, reply_id):
    """350 / 550 / 559 调用：拿 (task_id, source) 派发事件。
    顺序：
      1. 已经按 reply_id 配对过 → 直接查 _reply_id_to_task 返回
      2. question_id 在 553 暂存里 → 拿 (task_id, source)，若 reply_id 给了顺手做正式配对
      3. 兜底从 _unconfirmed_tasks 队首 peek；若 reply_id 给了则正式配对（pop 出队），否则不弹
    返回 (task_id, source)；都没有返回 (None, "")。"""
    with _attribution_lock:
        if reply_id and reply_id in _reply_id_to_task:
            return _reply_id_to_task[reply_id]
        if question_id and question_id in _question_id_to_task:
            tid, src = _question_id_to_task[question_id]
            if reply_id and reply_id not in _reply_id_to_task:
                _task_id_to_reply_id[tid] = reply_id
                _reply_id_to_task[reply_id] = (tid, src)
            return tid, src
        if _unconfirmed_tasks:
            tid, src = _unconfirmed_tasks[0]
            if reply_id:
                _unconfirmed_tasks.popleft()
                _task_id_to_reply_id[tid] = reply_id
                _reply_id_to_task[reply_id] = (tid, src)
                if _DEBUG_ATTRIBUTION:
                    _log_fn(
                        f"[时序·归因] pair-by-fifo task={tid} source={src} reply_id={reply_id} "
                        f"{_attribution_snapshot_unsafe()}"
                    )
            return tid, src
        return None, ""


def _finalize_by_reply_id(reply_id) -> tuple:
    """359 TTSEnded 时调用：根据 reply_id 找到 (task_id, source) 并清掉本次响应的所有归因记录。
    若 reply_id 没传或者没配对过（异常 / 测试 mock），兜底从 _unconfirmed_tasks 弹队首。
    返回 (task_id, source)；都没有返回 (None, "")。"""
    with _attribution_lock:
        if reply_id and reply_id in _reply_id_to_task:
            tid, src = _reply_id_to_task.pop(reply_id)
            _task_id_to_reply_id.pop(tid, None)
            for qid in [k for k, v in _question_id_to_task.items() if v[0] == tid]:
                _question_id_to_task.pop(qid, None)
            # 如果当时 fallback 没消费 unconfirmed（reply_id 一直没来过），现在再清一道
            for i, (utid, _usrc) in enumerate(_unconfirmed_tasks):
                if utid == tid:
                    del _unconfirmed_tasks[i]
                    break
            if _DEBUG_ATTRIBUTION:
                _log_fn(
                    f"[时序·归因] finalize-by-reply task={tid} source={src} reply_id={reply_id} "
                    f"{_attribution_snapshot_unsafe()}"
                )
            return tid, src
        # 兜底：reply_id 缺失（mock 测试或协议异常）→ FIFO 弹队首
        if _unconfirmed_tasks:
            tid, src = _unconfirmed_tasks.popleft()
            _log_fn(
                f"[时序·归因] finalize-fifo-fallback task={tid} source={src} reply_id={reply_id} "
                f"{_attribution_snapshot_unsafe()}"
            )
            return tid, src
        _log_fn(
            f"[时序·归因] finalize 但找不到任何待结的 task（reply_id={reply_id}）—— "
            f"可能服务端发了多余 359 / 状态错乱"
        )
        return None, ""


def _clear_attribution():
    global _drop_incoming_audio, _cancelled_unpaired_response_slots, _pending_asr_question_id
    """会话结束 / 错误兜底 / close_session 时清空所有归因状态。"""
    with _attribution_lock:
        if _DEBUG_ATTRIBUTION and (_unconfirmed_tasks or _reply_id_to_task or _question_id_to_task):
            _log_fn(
                f"[时序·归因] clear 清前 {_attribution_snapshot_unsafe()}"
            )
        _unconfirmed_tasks.clear()
        _task_id_to_reply_id.clear()
        _reply_id_to_task.clear()
        _question_id_to_task.clear()
        _cancelled_task_ids.clear()
        _cancelled_reply_ids.clear()
        _cancelled_question_ids.clear()
        _cancelled_unpaired_response_slots = 0
        _drop_incoming_audio = False
        _pending_asr_question_id = None


def _cancelled_response_event(question_id=None, reply_id=None) -> bool:
    """判定一条服务端响应事件（350/559/359 等）是否属于一个已被取消的响应。

    qid 判定的边界：早期某次清理预标记的 session 级 question_id 会一直停在
    `_cancelled_question_ids` 里；如果对所有路径都用 qid 判 cancel，**say_streaming
    路径**（事件源 ChatTTSText 500，本来就和 user query 无关）会被无差别误伤——
    服务端 350/559/359 全被丢弃，`_extend_speech_done_at` 的 30 秒保守上限永远
    精算不到真实值，PROACTIVE 倒计时从这个上限开始算，体感间隔暴涨到 20-30 秒。
    所以 say 路径**只看 reply_id / task_id**，不看 qid。inject / 未知路径保留 qid 判定。
    """
    with _attribution_lock:
        if reply_id and reply_id in _cancelled_reply_ids:
            return True
        if reply_id and reply_id in _reply_id_to_task:
            tid, _src = _reply_id_to_task[reply_id]
            return tid in _cancelled_task_ids
        # 未配对时先看 _unconfirmed_tasks 队首：say 路径不允许走 qid 判定
        peeked_source = None
        if _unconfirmed_tasks:
            peeked_tid, peeked_source = _unconfirmed_tasks[0]
            if peeked_tid in _cancelled_task_ids:
                return True
        if peeked_source == "say":
            return False
        if question_id and question_id in _cancelled_question_ids:
            return True
        if question_id and question_id in _question_id_to_task:
            tid, _src = _question_id_to_task[question_id]
            return tid in _cancelled_task_ids
        return False


def _consume_cancelled_unpaired_response(question_id=None, reply_id=None) -> bool:
    """取消发生在服务端 id 配对前时，丢弃随后抵达的旧响应首包。"""
    global _cancelled_unpaired_response_slots
    with _attribution_lock:
        if _cancelled_unpaired_response_slots <= 0:
            return False
        if reply_id and reply_id in _reply_id_to_task:
            return False
        if question_id and question_id in _question_id_to_task:
            return False
        if not reply_id and not question_id:
            return False
        _cancelled_unpaired_response_slots -= 1
        if reply_id:
            _cancelled_reply_ids.add(reply_id)
        if question_id:
            _cancelled_question_ids.add(question_id)
        return True


def _accept_response_audio():
    global _drop_incoming_audio
    if _drop_incoming_audio:
        _drop_incoming_audio = False


def _ignore_cancelled_response(reason: str, *, question_id=None, reply_id=None):
    global _chat_buffer, _chat_published_chars, _last_response_full_text, _drop_incoming_audio
    if reply_id:
        _cancelled_reply_ids.add(reply_id)
    if question_id:
        _cancelled_question_ids.add(question_id)
    _chat_buffer = ""
    _chat_published_chars = 0
    _last_response_full_text = ""
    if reason == "tts_end":
        _drop_incoming_audio = False
    _log_fn(
        f"{C_RT}[realtime_chat] drop cancelled response "
        f"reason={reason} qid={question_id} rid={reply_id}{C_RESET}"
    )


def discard_task(task_id: str):
    """放弃一个本地 task，避免服务端漏发完成事件后污染后续 TTS 归因。

    这个函数只清归因表和元数据，不发业务事件；调用方会自行决定是否用本地估算收敛。
    """
    global _drop_incoming_audio, _cancelled_unpaired_response_slots
    if not task_id:
        return
    with _attribution_lock:
        _cancelled_task_ids.add(task_id)
        removed_unconfirmed = False
        found_question_id = False
        for i, (tid, _src) in enumerate(list(_unconfirmed_tasks)):
            if tid == task_id:
                del _unconfirmed_tasks[i]
                removed_unconfirmed = True
                break
        reply_id = _task_id_to_reply_id.pop(task_id, None)
        if reply_id is not None:
            _cancelled_reply_ids.add(reply_id)
            _reply_id_to_task.pop(reply_id, None)
        for qid, (tid, _src) in list(_question_id_to_task.items()):
            if tid == task_id:
                found_question_id = True
                _cancelled_question_ids.add(qid)
                _question_id_to_task.pop(qid, None)
        _task_meta.pop(task_id, None)
        if removed_unconfirmed and reply_id is None and not found_question_id:
            _cancelled_unpaired_response_slots += 1
        _drop_incoming_audio = True


def _discard_runtime_inflight_tasks(rt: SessionRuntime) -> int:
    cancelled = 0
    unpaired = 0
    for tid, _src in list(rt.unconfirmed_tasks):
        rt.cancelled_task_ids.add(tid)
        _task_meta.pop(tid, None)
        cancelled += 1
        unpaired += 1
    rt.unconfirmed_tasks.clear()

    for tid, rid in list(rt.task_id_to_reply_id.items()):
        rt.cancelled_task_ids.add(tid)
        if rid:
            rt.cancelled_reply_ids.add(rid)
        _task_meta.pop(tid, None)
        cancelled += 1
    rt.task_id_to_reply_id.clear()
    rt.reply_id_to_task.clear()

    for qid, (tid, _src) in list(rt.question_id_to_task.items()):
        rt.cancelled_task_ids.add(tid)
        rt.cancelled_question_ids.add(qid)
        _task_meta.pop(tid, None)
        cancelled += 1
    rt.question_id_to_task.clear()

    rt.cancelled_unpaired_response_slots += unpaired
    rt.drop_incoming_audio = True
    rt.chat_buffer = ""
    rt.chat_published_chars = 0
    rt.last_response_full_text = ""
    return cancelled


def _discard_all_inflight_tasks(reason: str = "") -> int:
    """用户开口打断时，取消所有角色 session 里仍在途的旧回复。"""
    global _drop_incoming_audio, _cancelled_unpaired_response_slots
    global _chat_buffer, _chat_published_chars, _last_response_full_text
    with _attribution_lock:
        total = 0
        if _sessions:
            for rt in _sessions.values():
                total += _discard_runtime_inflight_tasks(rt)
            cur = _sessions.get(_resolve_speaker())
            if cur is not None:
                _drop_incoming_audio = cur.drop_incoming_audio
                _cancelled_unpaired_response_slots = cur.cancelled_unpaired_response_slots
                _chat_buffer = cur.chat_buffer
                _chat_published_chars = cur.chat_published_chars
                _last_response_full_text = cur.last_response_full_text
        else:
            dummy = SessionRuntime(speaker=_default_speaker, config=SessionConfig())
            dummy.unconfirmed_tasks = _unconfirmed_tasks
            dummy.task_id_to_reply_id = _task_id_to_reply_id
            dummy.reply_id_to_task = _reply_id_to_task
            dummy.question_id_to_task = _question_id_to_task
            dummy.cancelled_task_ids = _cancelled_task_ids
            dummy.cancelled_reply_ids = _cancelled_reply_ids
            dummy.cancelled_question_ids = _cancelled_question_ids
            dummy.cancelled_unpaired_response_slots = _cancelled_unpaired_response_slots
            total = _discard_runtime_inflight_tasks(dummy)
            _cancelled_unpaired_response_slots = dummy.cancelled_unpaired_response_slots
            _drop_incoming_audio = dummy.drop_incoming_audio
            _chat_buffer = dummy.chat_buffer
            _chat_published_chars = dummy.chat_published_chars
            _last_response_full_text = dummy.last_response_full_text
        if total:
            _log_fn(f"{C_RT}[realtime_chat] cancelled inflight responses total={total} reason={reason}{C_RESET}")
        return total

# 端到端"Lumi 估算说完时刻"——给主循环 PROACTIVE 沉默兜底用
# inject/say 时先按上限保守设到未来，避免响应期间被打断；
# 收到 ChatResponseEnd (559) 后按 reply 文本长度精算（中文 TTS ≈ 4 字/秒）重置；
# SessionFinished/error 时显式标为 now，立刻解除占用。
# 端到端服务端实测不会发 TTSEnded (359)，所以不能靠 359 来推进——必须靠估算。
_last_speech_done_at: float = 0.0
_speech_done_lock = Lock()
_user_audio_active: bool = False


def get_last_speech_done_at(speaker: str = None) -> float:
    """端到端估算的"上次任意角色说完时刻"。0 表示还没开口过。
    主循环用 time.time() - 这个值 判断是否过了沉默兜底阈值。

    **不按 speaker 分支**：端到端协议下一时刻只能有一个 session 在播音，
    "上次说完时刻" process 级只有一个。speaker 参数保留兼容性，不影响返回值。

    历史 bug 提示：之前曾按 speaker 分支读 `_sessions[sp].last_speech_done_at`
    per-session 快照——但 `_set/extend_speech_done_at` 只改全局，per-session
    字段只在 `_runtime_context` 进出时同步一次，导致从主循环读到的是初始值 0，
    elapsed 始终是 now（巨大），双角色场景下立刻误判"沉默 4 秒触发 PROACTIVE"
    + "已说完 0.5 秒立刻切麦"，造成 Lumi 还在播音 Nox 就被 inject 接龙、
    两条 audio 同时写 monitor 扬声器的级联故障（2026-05-10 实测）。
    """
    with _speech_done_lock:
        return _last_speech_done_at


def is_speaking(speaker: str = None) -> bool:
    """端到端当前是否估算还在说话（任一角色）。同 get_last_speech_done_at，
    端到端协议下一时刻只能有一个 session 在播音，speaker 参数保留兼容性。"""
    with _speech_done_lock:
        return _last_speech_done_at > time.time()


def is_user_audio_active() -> bool:
    """服务端已经识别到用户正在说话，但还没发 ASREnded。"""
    return _user_audio_active


def _set_speech_done_at(target: float):
    """直接覆盖 _last_speech_done_at（精算值用）。"""
    global _last_speech_done_at
    with _speech_done_lock:
        _last_speech_done_at = target


def _extend_speech_done_at(target: float):
    """只延长不缩短（保守上限用）。"""
    global _last_speech_done_at
    with _speech_done_lock:
        if target > _last_speech_done_at:
            _last_speech_done_at = target


# 本轮响应累计音频字节数 — inject/say 时清零，359 时基于此精确算播放总时长
# 24kHz / float32 / mono → 1 秒 = 24000 × 4 = 96000 字节
_AUDIO_BYTES_PER_SECOND = 24000 * 4
_response_audio_bytes: int = 0
_response_started_at: float = 0.0
_response_tts_types: set[str] = set()

# 字幕"已切句发出"的进度（按 _chat_buffer 剥情绪标签后的字符索引计）。
# 端到端 SC2.0 实测 350(TTSSentenceStart) 事件 tts_type='default' 且 text 字段为空，
# 无法用做字幕文本源；改从 550(ChatResponse) 的流式文本里扫断句标点切句入队。
_chat_published_chars: int = 0
# 559 时存一下完整回复（含情绪标签），359 时用来算"总字数"做 ms_per_char 物理标定。
_last_response_full_text: str = ""

# 情绪标签正则——回复开头形如 "[开心] 你好啊"，剥掉它再扫断句，避免把 "]" 当成句子起点
# 或在 chunks 半截时漏掉前缀。
_EMOTION_TAG_RE = re.compile(r"^\s*\[\w+\]\s*")
# 中文 + 英文断句符。'.' 容易误命中数字/缩写所以不放进来，靠 '。'/'!'/'?' 这些覆盖。
_SENTENCE_ENDINGS = "。？！；…!?;"


def _strip_emotion_tag(text: str) -> str:
    """剥掉开头的 [情绪] 标签和紧跟的空白。tag 还没收齐时（半截 "[Hap"）正则不匹配，
    返回原文，等下一轮 chunk 进来再剥。"""
    return _EMOTION_TAG_RE.sub("", text, count=1)


def _reset_response_audio_counter():
    """新一轮 inject/say 开始时清零累计字节，重置开始时刻。

    诊断（2026-05-10 调查"段落播完倒计时已播=0"问题）：打印调用源 + before/after
    值，看是不是某条路径意外频繁调 _reset 或者 _response_started_at 被别的路径改。
    """
    global _response_audio_bytes, _response_started_at, _response_tts_types
    global _chat_published_chars, _last_response_full_text
    import sys
    caller = sys._getframe(1).f_code.co_name
    before_started = _response_started_at
    before_bytes = _response_audio_bytes
    _response_audio_bytes = 0
    _response_started_at = time.time()
    _response_tts_types = set()
    _chat_published_chars = 0
    _last_response_full_text = ""
    sp = _resolve_speaker()
    _log_fn(
        f"{C_RT}[reset_counter] @ {time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} "
        f"caller={caller} sp={sp} "
        f"before(started_at={before_started:.2f} bytes={before_bytes}) "
        f"after(started_at={_response_started_at:.2f} bytes=0){C_RESET}"
    )


def _audio_frame_stats(nbytes: int):
    """累加本轮响应收到的音频字节数 — 359 时按此精确算 TTS 播放总时长。"""
    global _response_audio_bytes
    _response_audio_bytes += nbytes


def _drain_queue(q: queue.Queue) -> int:
    drained = 0
    while True:
        try:
            q.get_nowait()
            drained += 1
        except queue.Empty:
            break
    return drained


def _abort_all_audio_queues() -> tuple[int, list[str]]:
    """用户开口时打断所有角色未播完的音频，避免双角色尾音重叠。"""
    drained = 0
    affected = []
    if _sessions:
        for sp, rt in list(_sessions.items()):
            n = (
                _drain_queue(rt.audio_in_queue)
                + _drain_queue(rt.monitor_queue)
                + _drain_queue(rt.aec_queue)
            )
            if n:
                drained += n
                affected.append(sp)
        return drained, affected

    drained = (
        _drain_queue(_audio_in_queue)
        + _drain_queue(_monitor_queue)
        + _drain_queue(_aec_queue)
    )
    return drained, [_active_speaker] if drained else []


def abort_audio_queues() -> dict:
    """Public wrapper used by the speech output arbiter."""
    global _chat_buffer, _chat_published_chars, _last_response_full_text
    drained, affected = _abort_all_audio_queues()
    _chat_buffer = ""
    _chat_published_chars = 0
    _last_response_full_text = ""
    _set_speech_done_at(time.time())
    return {"drained": drained, "affected_speakers": affected}


# ChatResponse (550) 流式 content 累积缓冲。端到端把一句完整回复按字/词流式推过来，
# 每个 550 事件只带一个片段；要累积到 ChatResponseEnd (559) 才是一次完整逻辑回复。
# 之前实现成"每个 550 都 publish 一次 reply"会让 emotion_sidecar / log_turn / VTS 表情
# 被 13+ 个 token 风暴反复触发，1.6 flash 限流 + VTS 抽搐。
_chat_buffer: str = ""

# ASRResponse (451) 最终稿累积。端到端把识别结果分中间稿（is_interim=True）和最终稿
# （is_interim=False）流式推过来，文字只在 451 里有，459(ASREnded) 只是"用户说完"信号、
# payload 里没有 text 字段。所以要把 451 里所有最终稿文本拼起来，等 459 时一次性 publish。
_asr_final_buffer: str = ""
_last_user_query: str = ""

# 音频 chunk 队列（Task 9 的播放线程从这里消费）
_audio_in_queue: queue.Queue = queue.Queue()


def _next_task_id() -> str:
    """生成单调递增的 task_id（str 格式 rt_<毫秒时间戳>_<计数>）。

    跨毫秒/同毫秒下都按字典序单调递增（计数器全局递增 + 13 位时间戳长度恒定）。
    """
    global _task_id_counter
    with _task_id_lock:
        _task_id_counter += 1
        return f"rt_{int(time.time() * 1000)}_{_task_id_counter}"


def _send_frame_threadsafe(frame: bytes, speaker: str = None):
    """从主线程把帧丢进 ws 线程异步发出。"""
    sp = _resolve_speaker(speaker)
    rt = _sessions.get(sp)
    ws = rt.ws if rt is not None else _ws
    loop = rt.ws_loop if rt is not None else _ws_loop
    alive = rt.ws_alive if rt is not None else _ws_alive
    if ws is None or loop is None or not alive:
        _log_fn(f"{C_ERR}[realtime_chat] ws 未连，丢弃帧{C_RESET}")
        return False

    async def _send():
        await ws.send(frame)

    asyncio.run_coroutine_threadsafe(_send(), loop)
    return True


def _send_frame_for_speaker(frame: bytes, speaker: str):
    """Keep old one-arg monkeypatch tests compatible for the active/default speaker."""
    if speaker == _resolve_speaker():
        return _send_frame_threadsafe(frame)
    else:
        return _send_frame_threadsafe(frame, speaker=speaker)


def say(text: str, speaker: str = None) -> str:
    """SayHello (event 300) — 客户端提交文本让端到端 TTS 直接合成（跳过 LLM）。

    返回 task_id（str），调用方可订阅 tts_segment_done / tts_done 按 task_id 过滤。
    """
    with _runtime_context(speaker) as sp:
        task_id = _next_task_id()
        _task_meta[task_id] = {"source": "say", "text": text, "created_at": time.time()}
        _enqueue_unconfirmed(task_id, "say")
        frame = proto.build_event_frame(
            event_id=300, session_id=_session_id, payload={"content": text},
        )
        _send_frame_for_speaker(frame, sp)
        # 保守上限：say 跳过 LLM 直接 TTS，预留 30s 上限防 PROACTIVE 在响应期间打断
        _extend_speech_done_at(time.time() + 30.0)
        _reset_response_audio_counter()
    return task_id


def say_streaming(text: str, *, is_first: bool, is_last: bool,
                  task_id: str = None, speaker: str = None) -> str:
    """ChatTTSText (event 500) —— 流式 TTS 合成。一次完整 speak 调用对应一次流，多句拼成
    同一个 reply（一个 reply_id），避免 SayHello 多次"取消上一段、开始新一段"导致的"只剩
    最后一句被听到"问题。

    协议（来自正本文档）：
      - 第一包：{ start: true,  content: <首句>, end: false }
      - 中间包：{ start: false, content: <续句>, end: false }
      - 最后一包：{ start: false, content: "" 或 <尾句>, end: true }（标记结束）

    若 speak 被打断且尚未发出最后一包，调用方应当 **不发** 最后一包，避免服务端流程异常。

    参数：
      is_first：是否本次流的第一包（仅第一包入 _unconfirmed_tasks 与上层关联 task_id）
      is_last：是否最后一包（end=true）
      task_id：is_first=True 时可不传，内部生成；后续包必须传第一包返回的 task_id。
    返回：本次流的 client task_id（is_first=True 时返回新生成的；否则原样返回入参）。
    """
    with _runtime_context(speaker) as sp:
        if is_first:
            if task_id is None:
                task_id = _next_task_id()
                _task_meta[task_id] = {"source": "say", "text": text, "created_at": time.time()}
            _enqueue_unconfirmed(task_id, "say")
            # 第一包发出时按上限保守预留，等服务端 350/559 回流再精算
            _extend_speech_done_at(time.time() + 30.0)
            _reset_response_audio_counter()
        elif task_id is None:
            raise ValueError("say_streaming: 中间/末尾包必须传入第一包返回的 task_id")

        payload = {"start": is_first, "content": text or "", "end": is_last}
        frame = proto.build_event_frame(
            event_id=500, session_id=_session_id, payload=payload,
        )
        _send_frame_for_speaker(frame, sp)
        # 仅在第一包（新 TTS 流起点）记 inject 时刻，作为"期待服务端回应"的起点
        if is_first:
            _rt_say = _sessions.get(sp)
            if _rt_say is not None:
                _rt_say.last_inject_at = time.time()
    return task_id


def inject_text(text: str, speaker: str = None) -> str:
    """ChatTextQuery (event 501) — 客户端提交文字作为 user 输入，触发 LLM + TTS。

    返回 task_id（str）。

    把 inject 的文本同步设为该 session 的 _last_user_query —— 让本次 inject 触发的
    reply 事件能在 user_query 字段里带上 inject 原文（弹幕原文 / PROACTIVE 提示词
    / 导演笔记等）。否则 _last_user_query 只在 ASREnded 时更新，纯弹幕互动场景下
    一直是空 → _on_realtime_reply 跨角色 sync_history 拿不到 user_query → Nox
    看不见 Lumi 回弹幕的内容（实测 2026-05-10 11:59 日志全程无 sync ack）。
    """
    with _runtime_context(speaker) as sp:
        global _last_user_query
        _last_user_query = text
        task_id = _next_task_id()
        _task_meta[task_id] = {"source": "inject", "text": text, "created_at": time.time()}
        _enqueue_unconfirmed(task_id, "inject")
        frame = proto.build_event_frame(
            event_id=501, session_id=_session_id, payload={"content": text},
        )
        if _send_frame_for_speaker(frame, sp) is False:
            discard_task(task_id)
            if _event_bus is not None:
                _event_bus.publish("realtime_transport_failed", {
                    "speaker": sp,
                    "task_id": task_id,
                    "reason": "ws_not_connected",
                    "operation": "inject_text",
                }, source="realtime_chat")
            raise RuntimeError(f"realtime ws not connected for {sp}")
        # 保守上限：inject 走 LLM + TTS，预留 60s 上限；ChatResponseEnd (559) 收到后按精算重置
        _extend_speech_done_at(time.time() + 60.0)
        _reset_response_audio_counter()
        _rt_inj = _sessions.get(sp)
        if _rt_inj is not None:
            _rt_inj.last_inject_at = time.time()
    return task_id


def sync_history(speaker: str, qa_pair: list[dict]) -> str:
    """ConversationCreate (510)：静默追加 QA 到指定角色 session history。

    **不进 _runtime_context** —— 嵌套 _runtime_context 会污染外层。

    bug 重现：ws 接收线程收 Lumi 的 559 → 外层 _runtime_context(Lumi) →
    publish reply 事件（同步）→ _on_realtime_reply 调 sync_history(Nox)
    → 内层 _runtime_context(Nox) enter 加载 Nox 状态（含 _response_started_at=0）
    → 内层 exit store rt[Nox]（Nox 状态保持）→ **全局没自动恢复成 Lumi 状态** →
    外层 exit 时 store → rt[Lumi].response_started_at = 0（被 Nox 状态覆盖错位）
    → 后续 Lumi 的 359 处理 elapsed=0 倒计时虚高（实测 2026-05-10 14:52
    诊断日志确认）。

    sync_history 只需要 session_id 和 ws 发帧能力 —— 这些都是 per-session
    永久字段（commit 899c440 之后稳定不被 swap 污染），直接读 rt[sp] 即可，
    不必走 _runtime_context 的 swap。
    """
    sp = _resolve_speaker(speaker)
    rt = _sessions.get(sp)
    if rt is None:
        return ""
    task_id = _next_task_id()
    payload = {"items": qa_pair}
    frame = proto.build_event_frame(
        event_id=proto.EVENT_CONVERSATION_CREATE,
        session_id=rt.session_id,
        payload=payload,
    )
    _send_frame_for_speaker(frame, sp)
    return task_id


# ===== 服务端事件派发 =====
def _dispatch_event(resp: dict, speaker: str = None):
    """把端到端服务端事件派发到事件总线。

    事件分类：
      - 音频帧（SERVER_ACK + bytes payload）→ 入 _audio_in_queue
      - 错误帧（SERVER_ERROR_RESPONSE 或 event 599）→ realtime_error
      - ASREnded (459) → user_speech_done + 开启一次 auto_chat 响应窗口
      - ChatResponse (550) → reply
      - TTSSentenceStart (350) → 字幕推送
      - TTSEnded (359) → tts_segment_done；source 为 auto_chat/inject 时同时发 tts_done
      - SessionFinished (152) → 仅做 session 终止兜底（清空残余 buffer / 状态）
    """
    global _chat_buffer, _asr_final_buffer, _last_user_query, _user_audio_active
    global _chat_published_chars, _last_response_full_text
    global _pending_asr_question_id
    sp = _resolve_speaker(speaker)

    if not resp:
        return
    # 静默卡顿探测：任何来自服务端的数据都刷新这个时刻（含音频帧）。
    _rt_evt = _sessions.get(sp)
    if _rt_evt is not None:
        _rt_evt.last_server_event_at = time.time()
    mt = resp.get("message_type")
    event = resp.get("event")
    payload = resp.get("payload_msg")

    # 音频帧：丢入播放队列
    if mt == "SERVER_ACK" and isinstance(payload, (bytes, bytearray)):
        if _drop_incoming_audio:
            return
        _audio_in_queue.put(bytes(payload))
        _audio_frame_stats(len(payload))
        return

    # 错误帧
    if mt == "SERVER_ERROR_RESPONSE" or event == 599:
        _log_fn(f"{C_ERR}[realtime_chat] 服务端错误 code={resp.get('code')} {payload}{C_RESET}")
        if "network" in _response_tts_types or _build_websearch_extra_fields():
            _write_websearch_trace({
                "event": "service_error",
                "speaker": sp,
                "code": resp.get("code"),
                "payload": payload,
                "user_query": _last_user_query,
                "tts_types": sorted(_response_tts_types),
            })
        # 错误意味着这一段 TTS 不会到 → 立刻解除 PROACTIVE 占用，否则 60s 上限内不会主动开话题
        _set_speech_done_at(time.time())
        # 错误兜底：清空所有归因状态，避免上游 speak 一直等不到 segment_done
        _clear_attribution()
        if _event_bus:
            _event_bus.publish("realtime_error", {
                "code": resp.get("code"),
                "payload": payload,
                "speaker": sp,
            }, source="realtime_chat")
        return

    if event == proto.EVENT_CONVERSATION_CREATED:
        _log_fn(f"{C_RT}[realtime_chat] {sp} sync_history ack{C_RESET}")
        return

    # ASRInfo：服务端在识别到用户开口的第一个字时发出。
    # 含义是"客户端立刻停止播放当前 TTS 音频"——把还在客户端缓冲的音频帧丢弃，避免开口后还能听到 Lumi 上一段的尾巴。
    # 注意：这里只清音频播放层，不要触发 tts_state.interrupted。speak 链路的"用户讲话了"信号走 459 → user_speech_done → speak 自然退出。
    if event == 450:
        _user_audio_active = True
        drained, affected = _abort_all_audio_queues()
        cancelled = _discard_all_inflight_tasks("asr_info")
        _extend_speech_done_at(time.time() + 10.0)
        question_id = (payload or {}).get("question_id") if isinstance(payload, dict) else None
        _pending_asr_question_id = question_id
        _log_fn(
            f"{C_RT}[realtime_chat] 450 ASRInfo 收到，打断未播音频 "
            f"drained={drained} affected={affected} cancelled={cancelled} "
            f"qid={question_id}{C_RESET}"
        )
        if _event_bus:
            _event_bus.publish("tts_audio_aborted", {
                "question_id": question_id,
                "speaker": sp,
                "affected_speakers": affected,
                "drained": drained,
            }, source="realtime_chat")
        return

    # ASRResponse：用户说话识别中。每条 result 有 is_interim 标志，True=中间稿、False=最终稿。
    # 文字只在这里出现；459(ASREnded) 不带 text。把所有最终稿拼起来，留给 459 一次性发出。
    if event == 451:
        saw_text = False
        for r in (payload or {}).get("results", []):
            if not r.get("is_interim"):
                seg = r.get("text", "")
                if seg:
                    _asr_final_buffer += seg
                    saw_text = True
        if saw_text:
            _user_audio_active = True
        return

    # ASREnded：用户说完
    if event == 459:
        _user_audio_active = False
        text = _asr_final_buffer
        _asr_final_buffer = ""
        _last_user_query = text
        # 用户说话也开启一次响应窗口（auto_chat）。空文本不会生成服务端回复，
        # 不创建 task，避免留下永远不会收敛的归因记录。
        new_task_id = None
        if text:
            new_task_id = _next_task_id()
            _task_meta[new_task_id] = {
                "source": "auto_chat", "text": text, "created_at": time.time(),
            }
            if _pending_asr_question_id:
                _stash_question_id(_pending_asr_question_id, new_task_id, "auto_chat")
            else:
                _enqueue_unconfirmed(new_task_id, "auto_chat")
        _pending_asr_question_id = None
        # 关键：开新一轮响应必须重置音频字节累计器和起始时刻，否则 359 会用上一段 inject
        # 起算的累计字节算"还要播多久"，导致倒计时被严重低估（直接给出 1 秒余量），
        # 沉默兜底跟着提前到 5 秒就触发，主动开新话题瞬间打断真正的回复。
        _reset_response_audio_counter()
        # 同时清空上一轮没结束的 ChatResponse 流式累积——用户在前一轮 559 还没到时就开口
        # 触发了新的 459，否则前轮 550 chunks 的残留会和本轮 chunks 拼到一起，最终 559
        # publish 出去的 reply 文本是"前轮+本轮"两段拼接，字幕会显示完整两段，但服务端
        # barge-in 已经把前一轮的音频砍掉了，听感就是"字幕完整、音频只有后半段"。
        _chat_buffer = ""
        # 只有真识别到 ASR 文本时才锁 60 秒等模型回复。空 text（噪声打断 / 用户
        # 短暂咳嗽 / 半句被中断）端到端服务端不会生成 reply —— 没有 559/359 来
        # 精算覆盖 _last_speech_done_at，会让 PROACTIVE 兜底卡 60 秒不触发，
        # 用户体感"Lumi/Nox 触发字幕打断之后突然不再主动说话了"（实测 2026-05-10）。
        if text:
            _extend_speech_done_at(time.time() + 60.0)
        else:
            _set_speech_done_at(time.time())
        if _event_bus:
            _event_bus.publish("user_speech_done", {
                "text": text,
                "task_id": new_task_id,
                "speaker": sp,
            }, source="realtime_chat")
        return

    # ChatResponse：LLM 文本流式 chunk —— 累积到缓冲，等 ChatResponseEnd (559) 才 publish 完整 reply。
    # 同时也是字幕文本的来源：端到端 350 事件 tts_type='default'/text 为空，没法直接拿断句文本；
    # 改从这里的流式 content 累积里扫断句标点（。？！；…!?;）切段入队，业务层（lumi.py）订阅
    # tts_segment_start 把切好的句子交给字幕光标 worker 按字推进。
    if event == 550:
        qid = (payload or {}).get("question_id") if isinstance(payload, dict) else None
        rid = (payload or {}).get("reply_id") if isinstance(payload, dict) else None
        if _cancelled_response_event(qid, rid) or _consume_cancelled_unpaired_response(qid, rid):
            _ignore_cancelled_response("chunk", question_id=qid, reply_id=rid)
            return
        _accept_response_audio()
        chunk = (payload or {}).get("content", "")
        if chunk:
            _chat_buffer += chunk
        # 尝试从已累积 buffer 中切出新完成的句子段
        stripped = _strip_emotion_tag(_chat_buffer)
        unpub = stripped[_chat_published_chars:]
        last_end = -1
        for i, ch in enumerate(unpub):
            if ch in _SENTENCE_ENDINGS:
                last_end = i
        if last_end >= 0:
            seg = unpub[: last_end + 1]
            _chat_published_chars += len(seg)
            if _event_bus:
                # 用 reply_id（兜底 question_id / unconfirmed 队首）归因到 task_id
                seg_task_id, _ = _peek_or_pair(qid, rid)
                _event_bus.publish("tts_segment_start", {
                    "text": seg,
                    "tts_type": "chat_tts_text",  # 合成标记，业务层过滤逻辑无需改
                    "speaker": sp,
                    "task_id": seg_task_id,
                }, source="realtime_chat")
        return

    # ChatResponseEnd：一次完整逻辑回复结束。把累积的 _chat_buffer 一次性 publish 出去。
    if event == 559:
        qid = (payload or {}).get("question_id") if isinstance(payload, dict) else None
        rid = (payload or {}).get("reply_id") if isinstance(payload, dict) else None
        if _cancelled_response_event(qid, rid) or _consume_cancelled_unpaired_response(qid, rid):
            _ignore_cancelled_response("response_end", question_id=qid, reply_id=rid)
            return
        _accept_response_audio()
        full_text = _chat_buffer
        _chat_buffer = ""
        # 留给 359 做物理时长标定用（按总音频字节 ÷ 总字数 反推 ms_per_char）
        _last_response_full_text = full_text
        if full_text:
            head_task_id, source = _peek_or_pair(qid, rid)
            # 兜底切段：若末段没标点结尾（如疑问/未结束的句子），550 流里没扫到断句符 →
            # 把剩余未发布部分作为最后一段补发出去，避免末句字幕丢失。
            stripped = _strip_emotion_tag(full_text)
            tail = stripped[_chat_published_chars:]
            if tail and _event_bus:
                _chat_published_chars += len(tail)
                _event_bus.publish("tts_segment_start", {
                    "text": tail,
                    "tts_type": "chat_tts_text",
                    "speaker": sp,
                    "task_id": head_task_id,
                }, source="realtime_chat")
            # 按 reply 文本长度精算 TTS 还要播多久（中文 ≈ 4 字/秒，加 1.5 秒缓冲，最少 3 秒）
            estimated_tts = max(len(full_text) / 4.0 + 1.5, 3.0)
            _set_speech_done_at(time.time() + estimated_tts)
            if "network" in _response_tts_types:
                _write_websearch_trace({
                    "event": "network_complete",
                    "speaker": sp,
                    "question_id": qid,
                    "reply_id": rid,
                    "task_id": head_task_id,
                    "source": source,
                    "user_query": _last_user_query,
                    "answer": full_text,
                    "tts_types": sorted(_response_tts_types),
                    "raw_results_available": False,
                    "note": "端到端内置联网未向客户端返回原始搜索结果列表；这里只记录最终回答。",
                })
                _log_fn(
                    f"{C_RT}[realtime_chat] {sp} 内置联网搜索完成 "
                    f"task={head_task_id} qid={qid} rid={rid} 日志=logs/realtime_websearch.jsonl{C_RESET}"
                )
            if _event_bus:
                _event_bus.publish("reply", {
                    "text": full_text,
                    "speaker": sp,
                    "task_id": head_task_id,
                    "source": source,
                    "user_query": _last_user_query,
                }, source="realtime_chat")
        return

    # TTSSentenceStart：端到端 SC2.0 实测 tts_type='default' 且 text 为空，没法用做字幕
    # 文本源——字幕文本改从 550 流式累积里切句拿。这里仅用作 SayHello 路径首次拿到 reply_id
    # 时的归因配对时机：调一下 _peek_or_pair，让 task_id ↔ reply_id 表写入。不发任何事件。
    if event == 350:
        qid = (payload or {}).get("question_id") if isinstance(payload, dict) else None
        rid = (payload or {}).get("reply_id") if isinstance(payload, dict) else None
        tts_type = (payload or {}).get("tts_type") if isinstance(payload, dict) else None
        if _cancelled_response_event(qid, rid) or _consume_cancelled_unpaired_response(qid, rid):
            _ignore_cancelled_response("tts_start", question_id=qid, reply_id=rid)
            return
        _accept_response_audio()
        task_id, source = _peek_or_pair(qid, rid)
        if tts_type:
            first_network = tts_type == "network" and "network" not in _response_tts_types
            _response_tts_types.add(tts_type)
            if first_network:
                _write_websearch_trace({
                    "event": "network_start",
                    "speaker": sp,
                    "question_id": qid,
                    "reply_id": rid,
                    "task_id": task_id,
                    "source": source,
                    "user_query": _last_user_query,
                    "tts_type": tts_type,
                    "text": (payload or {}).get("text", ""),
                })
                _log_fn(
                    f"{C_RT}[realtime_chat] {sp} 本轮触发内置联网搜索 "
                    f"task={task_id} qid={qid} rid={rid}{C_RESET}"
                )
        return

    # TTSSentenceEnd：同样 text 为空、单句时长无法和文本对齐。物理 ms_per_char 标定改到
    # 359 一次性算（总响应音频字节 ÷ 总响应字数）。这里也不做副作用。
    if event == 351:
        return

    # ChatTextQueryConfirmed：inject_text(501) 的 ack。携带 question_id，供后续 350/559 用
    # question_id → task_id 暂存做正式配对（reply_id 在 350/559 才回来）。
    # 从 _unconfirmed_tasks 弹队首关联——前提是 inject 之后没立刻插 say（业务流程上确实如此）。
    if event == 553:
        qid = (payload or {}).get("question_id") if isinstance(payload, dict) else None
        if qid:
            if _cancelled_response_event(qid, None) or _consume_cancelled_unpaired_response(qid, None):
                _ignore_cancelled_response("query_confirmed", question_id=qid)
                return
            with _attribution_lock:
                if _unconfirmed_tasks:
                    tid, src = _unconfirmed_tasks.popleft()
                    _question_id_to_task[qid] = (tid, src)
                    if _DEBUG_ATTRIBUTION:
                        _log_fn(
                            f"[时序·归因] 553 stash question_id={qid} → task={tid} source={src} "
                            f"{_attribution_snapshot_unsafe()}"
                        )
        return

    # TTSEnded：服务端 TTS 流结束（所有音频帧都发完了，但客户端还在播）。
    # 此刻基于"自 inject 起累积接收的音频字节数"精确算客户端播放总时长 ——
    # 这比 559 时按文本长度估算精确得多，且不依赖中文/英文/标点等 TTS 节奏。
    # 24kHz/float32/mono → 1 秒 = 96000 字节
    if event == 359:
        # 用 reply_id 归因到 task_id（兜底：mock 测试或 reply_id 缺失时 FIFO 弹 unconfirmed 队首）
        rid = (payload or {}).get("reply_id") if isinstance(payload, dict) else None
        with _attribution_lock:
            unknown_during_drop = _drop_incoming_audio and (not rid or rid not in _reply_id_to_task)
        if _cancelled_response_event(None, rid) or _consume_cancelled_unpaired_response(None, rid) or unknown_during_drop:
            _ignore_cancelled_response("tts_end", reply_id=rid)
            return
        _accept_response_audio()
        task_id, source = _finalize_by_reply_id(rid)
        # 服务端发的音频总时长（秒）
        total_audio_seconds = _response_audio_bytes / _AUDIO_BYTES_PER_SECOND
        # 截至现在已经过去多少秒（这部分已经在播放）
        elapsed_since_start = time.time() - _response_started_at if _response_started_at > 0 else 0.0
        # 还要播多久 = 总长 - 已过去（注意客户端播放可能略慢于接收，加 1 秒余量）
        remaining = max(total_audio_seconds - elapsed_since_start, 0.0) + 1.0
        _set_speech_done_at(time.time() + remaining)
        # 诊断：打印 _response_started_at 实际浮点值（看是 0 还是 now 还是历史时刻）
        _log_fn(
            f"[realtime_chat] 段落播完倒计时: 总长{total_audio_seconds:.1f}s / "
            f"已播{elapsed_since_start:.1f}s / 还要{remaining:.1f}s "
            f"[diag started_at={_response_started_at:.2f} now={time.time():.2f} sp={sp}]"
        )
        # 物理标定：本轮总响应的"每字毫秒"= 音频物理总时长 ÷ 干净文本字数。下一轮回复的字幕
        # 光标推进就跟着这个真实音色语速走，不再用默认 200ms/字 估算。
        if _last_response_full_text and total_audio_seconds > 0 and _event_bus:
            stripped_full = _strip_emotion_tag(_last_response_full_text)
            if stripped_full:
                _event_bus.publish("tts_segment_chunk_done", {
                    "text": stripped_full,
                    "duration_s": total_audio_seconds,
                    "speaker": sp,
                    "task_id": task_id,
                }, source="realtime_chat")
        # 兜底：若 559 事件没收到（异常 / 协议变化），把还在 buffer 里的累积文本 publish 一次
        if _chat_buffer:
            if _event_bus:
                _event_bus.publish("reply", {
                    "text": _chat_buffer,
                    "speaker": sp,
                    "task_id": task_id,
                    "user_query": _last_user_query,
                }, source="realtime_chat")
            _chat_buffer = ""
        if _event_bus:
            _event_bus.publish("tts_segment_done", {
                "task_id": task_id, "source": source, "speaker": sp,
            }, source="realtime_chat")
            # auto_chat 和 inject 单段就是一次完整逻辑回复 → 同时发 tts_done
            # say 是被 lumi_tts.speak 切句后多段拼合的一段，逻辑回复结束由 speak 自己 publish
            if source in ("auto_chat", "inject"):
                _event_bus.publish("tts_done", {
                    "task_id": task_id, "source": source, "speaker": sp,
                }, source="realtime_chat")
        return

    # SessionFinished：整个 session 结束（close_session 或服务端主动断）
    # 仅做兜底清理：若 buffer 还有残余 / 还有未结的响应窗口，把状态收敛掉
    if event == 152:
        if _chat_buffer and _event_bus:
            # 兜底归因：拿 unconfirmed 队首或第一个 paired reply 任意一个 task_id 即可
            head_task_id, _ = _peek_or_pair(None, None)
            _event_bus.publish("reply", {
                "text": _chat_buffer,
                "speaker": sp,
                "task_id": head_task_id,
                "user_query": _last_user_query,
            }, source="realtime_chat")
        _chat_buffer = ""
        _clear_attribution()
        # session 结束 → 不再有 TTS 在播 → 立刻解除 PROACTIVE 占用
        _set_speech_done_at(time.time())
        return


async def _recv_loop(speaker: str = None):
    """接收服务端事件 → 解析 → 派发。recv 异常意味着 ws 已断（服务端主动关或网络），置 _ws_alive=False。"""
    global _ws_alive
    sp = _resolve_speaker(speaker)
    rt = _sessions.get(sp)
    ws = rt.ws if rt is not None else _ws
    while True:
        try:
            raw = await ws.recv()
        except Exception as e:
            _ws_alive = False
            if sp in _sessions:
                _sessions[sp].ws_alive = False
            _log_fn(f"{C_ERR}[realtime_chat] recv 异常: {e}{C_RESET}")
            break
        try:
            resp = proto.parse_response(raw)
            with _runtime_context(sp):
                _dispatch_event(resp, speaker=sp)
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat] 解析失败: {e}{C_RESET}")


# ===== 音频播放线程 + AEC 参考缓冲（Task 9） =====
import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly

# 共享给 ASR 模块的 AEC 参考缓冲（与 lumi_tts.tts_ref_buffer 同理念，但端到端独立维护一份）
# 30 秒上限，16kHz mono int16
audio_ref_buffer: deque = deque(maxlen=16000 * 30)

_output_stream = None
_monitor_stream = None
_monitor_device_index: Optional[int] = None
_cable_device_index: Optional[int] = None
_player_thread: Optional[threading.Thread] = None
_player_running = False
_monitor_queue: queue.Queue = queue.Queue(maxsize=200)
_monitor_thread: Optional[threading.Thread] = None
_aec_queue: queue.Queue = queue.Queue(maxsize=200)
_aec_thread: Optional[threading.Thread] = None


def set_output_devices(cable_index: int = None, monitor_index: Optional[int] = None,
                       speaker: str = None, device_map: dict[str, int] = None):
    """运行时设置 TTS 输出虚拟声卡 + 监听设备索引。可在 init 之后任意时刻调。"""
    global _cable_device_index, _monitor_device_index
    if device_map:
        _speaker_output_devices.update(device_map)
        _monitor_device_index = monitor_index
        return
    if speaker:
        _speaker_output_devices[speaker] = cable_index
        _monitor_device_index = monitor_index
        return
    _cable_device_index = cable_index
    _monitor_device_index = monitor_index


def _open_output_stream(device_index: int):
    """端到端 SC2.0 默认返回 24kHz mono float32 PCM —— 播放流必须用 paFloat32。
    用 paInt16 直接写会把 4 字节 float 当成 2 个 int16 样本，听起来就是嘈杂电流声。
    """
    return _pa_instance.open(
        format=pyaudio.paFloat32, channels=1, rate=24000,
        output=True, output_device_index=device_index,
        frames_per_buffer=4800,
    )


def _player_loop():
    """主路径：从 _audio_in_queue 取数据 → 同步写 cable（驱动 VTS 嘴型）→ 投给 monitor / aec 后台线程。

    主路径只做 cable.write + 两个 put_nowait（O(1) 不阻塞），保证 cable 节奏跟上端到端推送。
    monitor 由 _monitor_loop 异步写，AEC 重采样由 _aec_loop 在后台做，都不阻塞主路径。
    """
    while _player_running:
        try:
            data = _audio_in_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if data is None:
            break
        if not data:
            continue
        try:
            if _output_stream is not None:
                _output_stream.write(data)
            if _monitor_stream is not None:
                try:
                    _monitor_queue.put_nowait(data)
                except queue.Full:
                    pass
            try:
                _aec_queue.put_nowait(data)
            except queue.Full:
                pass  # AEC 缓冲溢出不影响播放
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat·player] {e}{C_RESET}")


def _monitor_loop():
    """后台线程：从 _monitor_queue 取数据写 monitor_stream，与 cable 写解耦。"""
    while _player_running:
        try:
            data = _monitor_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if data is None:
            break
        if _monitor_stream is None:
            continue
        try:
            _monitor_stream.write(data)
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat·monitor] {e}{C_RESET}")


def _aec_loop():
    """后台线程：把端到端 float32 24kHz → int16 16kHz 写入 audio_ref_buffer 供 ASR 做 AEC。

    numpy 处理（frombuffer + clip + resample_poly + deque.extend）单帧几毫秒，
    放主路径会拖慢 cable.write 节奏，搬后台不影响 lumi_asr 读 ref buffer。
    """
    while _player_running:
        try:
            data = _aec_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if data is None:
            break
        try:
            f32 = np.frombuffer(data, dtype=np.float32)
            i16_24k = np.clip(f32 * 32767.0, -32768, 32767).astype(np.int16)
            ref_16k = resample_poly(i16_24k, up=2, down=3).astype(np.int16)
            audio_ref_buffer.extend(ref_16k.tolist())
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat·aec] {e}{C_RESET}")


def _player_loop_for_runtime(rt: SessionRuntime):
    while rt.player_running:
        try:
            data = rt.audio_in_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if data is None:
            break
        try:
            if rt.output_stream is not None:
                rt.output_stream.write(data)
            if rt.monitor_stream is not None:
                try:
                    rt.monitor_queue.put_nowait(data)
                except queue.Full:
                    pass
            try:
                rt.aec_queue.put_nowait(data)
            except queue.Full:
                pass
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat·player·{rt.speaker}] {e}{C_RESET}")


def _monitor_loop_for_runtime(rt: SessionRuntime):
    while rt.player_running:
        try:
            data = rt.monitor_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if data is None:
            break
        if rt.monitor_stream is None:
            continue
        try:
            rt.monitor_stream.write(data)
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat·monitor·{rt.speaker}] {e}{C_RESET}")


def _aec_loop_for_runtime(rt: SessionRuntime):
    while rt.player_running:
        try:
            data = rt.aec_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if data is None:
            break
        try:
            f32 = np.frombuffer(data, dtype=np.float32)
            i16_24k = np.clip(f32 * 32767.0, -32768, 32767).astype(np.int16)
            ref_16k = resample_poly(i16_24k, up=2, down=3).astype(np.int16)
            audio_ref_buffer.extend(ref_16k.tolist())
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat·aec·{rt.speaker}] {e}{C_RESET}")


def start_audio_output():
    """启动音频输出线程（cable 主播放）+ monitor / AEC 两条后台线程。需要先调 set_output_devices。"""
    global _output_stream, _monitor_stream, _player_thread, _player_running
    global _monitor_thread, _aec_thread
    if _sessions and _speaker_output_devices:
        for sp, rt in _sessions.items():
            cable_index = _speaker_output_devices.get(sp)
            if cable_index is None:
                continue
            rt.output_stream = _open_output_stream(cable_index)
            # 双角色场景下每条 session 都开 monitor_stream 写到外放扬声器，
            # 否则非 default speaker（如 Nox）的音频只到虚拟声卡 → OBS 推流，
            # 本地耳听就听不到。两条音频按当前架构是轮流播（不会同时），
            # PyAudio 多个 output stream 写同一设备索引在轮播下不会冲突。
            if _monitor_device_index is not None:
                rt.monitor_stream = _open_output_stream(_monitor_device_index)
            rt.player_running = True
            rt.player_thread = threading.Thread(target=_player_loop_for_runtime, args=(rt,), daemon=True, name=f"realtime-chat-player-{sp}")
            rt.player_thread.start()
            if rt.monitor_stream is not None:
                rt.monitor_thread = threading.Thread(target=_monitor_loop_for_runtime, args=(rt,), daemon=True, name=f"realtime-chat-monitor-{sp}")
                rt.monitor_thread.start()
            rt.aec_thread = threading.Thread(target=_aec_loop_for_runtime, args=(rt,), daemon=True, name=f"realtime-chat-aec-{sp}")
            rt.aec_thread.start()
        _log_fn(f"{C_RT}[realtime_chat] 播放线程启动 speakers={list(_speaker_output_devices.keys())} monitor={_monitor_device_index}{C_RESET}")
        return
    if _cable_device_index is None:
        raise RuntimeError("set_output_devices 还没调")
    _output_stream = _open_output_stream(_cable_device_index)
    if _monitor_device_index is not None:
        _monitor_stream = _open_output_stream(_monitor_device_index)
    _player_running = True
    _player_thread = threading.Thread(target=_player_loop, daemon=True, name="realtime-chat-player")
    _player_thread.start()
    if _monitor_stream is not None:
        _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True, name="realtime-chat-monitor")
        _monitor_thread.start()
    _aec_thread = threading.Thread(target=_aec_loop, daemon=True, name="realtime-chat-aec")
    _aec_thread.start()
    _log_fn(f"{C_RT}[realtime_chat] 播放线程启动 cable={_cable_device_index} monitor={_monitor_device_index}{C_RESET}")


def stop_audio_output():
    global _player_running, _output_stream, _monitor_stream
    if _sessions and _speaker_output_devices:
        for rt in _sessions.values():
            rt.player_running = False
            for q in (rt.audio_in_queue, rt.monitor_queue, rt.aec_queue):
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            for thread in (rt.player_thread, rt.monitor_thread, rt.aec_thread):
                if thread:
                    thread.join(timeout=2)
            for stream in (rt.output_stream, rt.monitor_stream):
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
            rt.output_stream = None
            rt.monitor_stream = None
        return
    _player_running = False
    _audio_in_queue.put(None)
    for q in (_monitor_queue, _aec_queue):
        try:
            q.put_nowait(None)
        except queue.Full:
            pass
    if _player_thread:
        _player_thread.join(timeout=2)
    if _monitor_thread:
        _monitor_thread.join(timeout=2)
    if _aec_thread:
        _aec_thread.join(timeout=2)
    for stream_attr in ("_output_stream", "_monitor_stream"):
        s = globals().get(stream_attr)
        if s is not None:
            try:
                s.stop_stream()
                s.close()
            except Exception:
                pass
    _output_stream = None
    _monitor_stream = None


# ===== 麦克风推送 + pause/resume（Task 10） =====
_mic_running = False
_mic_paused = True   # 默认暂停（启动序列把直播带到 CHATTING 后再 resume）
_mic_thread: Optional[threading.Thread] = None
_mic_input_stream = None
_mic_device_index: Optional[int] = None
_media_input_enabled = False
_media_input_device_index: Optional[int] = None
_media_input_stream = None
_media_input_rate = 16000
_media_input_channels = 1
_media_input_gain = 1.0
_media_input_lock = Lock()
_media_monitor_stream = None
_media_monitor_queue: Optional[queue.Queue] = None
_media_monitor_thread: Optional[threading.Thread] = None
_media_self_echo_drop_count = 0
_MEDIA_ECHO_REF_SECONDS = 4
_MEDIA_ECHO_CORR_THRESHOLD = 0.55
_MEDIA_ECHO_MIN_RMS = 0.003


def set_mic_device(device_index: int):
    """设置麦克风设备索引。"""
    global _mic_device_index
    _mic_device_index = device_index


def set_media_input_device(device_index: int, *, gain: float = 1.0):
    """设置外部媒体输入设备索引，例如 CABLE-A Output。"""
    global _media_input_device_index, _media_input_gain
    with _media_input_lock:
        _media_input_device_index = device_index
        _media_input_gain = max(0.0, float(gain))
    _log_fn(
        f"{C_RT}[realtime_chat] 外部媒体输入设备设置为 device={device_index} "
        f"gain={_media_input_gain:.2f}{C_RESET}"
    )


def set_media_input_enabled(enabled: bool):
    """开关外部媒体输入。关闭时保留设备配置，但不混入端到端输入。"""
    global _media_input_enabled
    with _media_input_lock:
        if enabled and _media_input_device_index is None:
            _media_input_enabled = False
            _log_fn(f"{C_ERR}[realtime_chat] 外部媒体输入未设置设备，无法开启{C_RESET}")
            return
        _media_input_enabled = bool(enabled)
    _log_fn(f"{C_RT}[realtime_chat] 外部媒体输入 {'开启' if enabled else '关闭'}{C_RESET}")


def get_media_input_status() -> dict:
    with _media_input_lock:
        return {
            "enabled": _media_input_enabled,
            "device_index": _media_input_device_index,
            "gain": _media_input_gain,
        }


def _close_media_input_stream_locked():
    global _media_input_stream, _media_input_rate, _media_input_channels
    _close_media_monitor_stream_locked()
    if _media_input_stream is None:
        return
    try:
        _media_input_stream.stop_stream()
        _media_input_stream.close()
    except Exception:
        pass
    _media_input_stream = None
    _media_input_rate = 16000
    _media_input_channels = 1


def _drain_media_monitor_queue_locked():
    q = _media_monitor_queue
    if q is None:
        return
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def _media_monitor_loop(stream, q: queue.Queue):
    while True:
        try:
            data = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if data is None:
            break
        try:
            stream.write(data)
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat·media-monitor] {e}{C_RESET}")
            break


def _ensure_media_monitor_stream_locked(rate: int, channels: int, frames_per_buffer: int):
    global _media_monitor_stream, _media_monitor_queue, _media_monitor_thread
    if _monitor_device_index is None or _media_monitor_stream is not None:
        return
    try:
        _media_monitor_stream = _pa_instance.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            output=True,
            output_device_index=_monitor_device_index,
            frames_per_buffer=frames_per_buffer,
        )
        _media_monitor_queue = queue.Queue(maxsize=50)
        _media_monitor_thread = threading.Thread(
            target=_media_monitor_loop,
            args=(_media_monitor_stream, _media_monitor_queue),
            daemon=True,
            name="realtime-chat-media-monitor",
        )
        _media_monitor_thread.start()
        _log_fn(
            f"{C_RT}[realtime_chat] 外部媒体监听已开启 "
            f"device={_monitor_device_index} rate={rate} channels={channels}{C_RESET}"
        )
    except Exception as e:
        _media_monitor_stream = None
        _media_monitor_queue = None
        _media_monitor_thread = None
        _log_fn(f"{C_ERR}[realtime_chat·media-monitor] 打开监听失败: {e}{C_RESET}")


def _close_media_monitor_stream_locked():
    global _media_monitor_stream, _media_monitor_queue, _media_monitor_thread
    q = _media_monitor_queue
    thread = _media_monitor_thread
    stream = _media_monitor_stream
    if q is not None:
        _drain_media_monitor_queue_locked()
        try:
            q.put_nowait(None)
        except queue.Full:
            pass
    if thread is not None:
        thread.join(timeout=1)
    if stream is not None:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
    _media_monitor_stream = None
    _media_monitor_queue = None
    _media_monitor_thread = None


def _queue_media_monitor_audio(data: bytes):
    q = _media_monitor_queue
    if q is None or not data:
        return
    try:
        q.put_nowait(data)
    except queue.Full:
        pass


def _looks_like_self_tts_echo(pcm_16k: np.ndarray) -> bool:
    if pcm_16k.size < 400:
        return False
    media = pcm_16k.astype(np.float32)
    media -= float(np.mean(media))
    media_rms = float(np.sqrt(np.mean(media * media))) / 32768.0
    if media_rms < _MEDIA_ECHO_MIN_RMS:
        return False
    ref_len = min(len(audio_ref_buffer), 16000 * _MEDIA_ECHO_REF_SECONDS)
    if ref_len < len(media):
        return False
    ref = np.asarray(list(audio_ref_buffer)[-ref_len:], dtype=np.float32)
    ref -= float(np.mean(ref))
    ref_rms = float(np.sqrt(np.mean(ref * ref))) / 32768.0
    if ref_rms < _MEDIA_ECHO_MIN_RMS:
        return False

    media_norm = float(np.linalg.norm(media)) + 1e-6
    step = max(800, len(media) // 4)
    best = 0.0
    for start in range(0, len(ref) - len(media) + 1, step):
        win = ref[start:start + len(media)]
        score = abs(float(np.dot(media, win)) / (media_norm * (float(np.linalg.norm(win)) + 1e-6)))
        if score > best:
            best = score
            if best >= _MEDIA_ECHO_CORR_THRESHOLD:
                return True
    return False


def _read_media_input(frames: int) -> Optional[bytes]:
    """读取外部媒体输入。返回 int16/16k/mono PCM；未启用或失败时返回 None。"""
    global _media_input_stream, _media_input_enabled, _media_input_rate, _media_input_channels
    global _media_self_echo_drop_count
    with _media_input_lock:
        if not _media_input_enabled or _media_input_device_index is None:
            _close_media_input_stream_locked()
            return None
        if _media_input_stream is None:
            try:
                info = _pa_instance.get_device_info_by_index(_media_input_device_index)
                _media_input_rate = int(info.get("defaultSampleRate", 16000)) or 16000
                _media_input_channels = min(2, int(info.get("maxInputChannels", 1)) or 1)
                input_frames = max(1, int(frames * _media_input_rate / 16000))
                _media_input_stream = _pa_instance.open(
                    format=pyaudio.paInt16, channels=_media_input_channels, rate=_media_input_rate,
                    input=True, input_device_index=_media_input_device_index,
                    frames_per_buffer=input_frames,
                )
                _ensure_media_monitor_stream_locked(_media_input_rate, _media_input_channels, input_frames)
                _log_fn(
                    f"{C_RT}[realtime_chat] 外部媒体输入流已打开 "
                    f"device={_media_input_device_index} rate={_media_input_rate} "
                    f"channels={_media_input_channels}{C_RESET}"
                )
            except Exception as e:
                _log_fn(f"{C_ERR}[realtime_chat·media] 打开输入失败: {e}{C_RESET}")
                _media_input_enabled = False
                _media_input_stream = None
                return None
        stream = _media_input_stream
        input_frames = max(1, int(frames * _media_input_rate / 16000))
        rate = _media_input_rate
        channels = _media_input_channels
    try:
        data = stream.read(input_frames, exception_on_overflow=False)
        pcm = np.frombuffer(data, dtype=np.int16)
        if channels > 1:
            pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
        if rate != 16000:
            pcm = resample_poly(pcm, up=16000, down=rate).astype(np.int16)
        if len(pcm) < frames:
            pcm = np.pad(pcm, (0, frames - len(pcm)))
        elif len(pcm) > frames:
            pcm = pcm[:frames]
        if _looks_like_self_tts_echo(pcm):
            _media_self_echo_drop_count += 1
            if _media_self_echo_drop_count <= 3 or _media_self_echo_drop_count % 20 == 0:
                _log_fn(f"{C_STATUS}[realtime_chat] 外部媒体丢弃疑似角色回声 count={_media_self_echo_drop_count}{C_RESET}")
            return None
        _queue_media_monitor_audio(data)
        return pcm.tobytes()
    except Exception as e:
        _log_fn(f"{C_ERR}[realtime_chat·media] 读取失败: {e}{C_RESET}")
        with _media_input_lock:
            _close_media_input_stream_locked()
        return None


def _mix_int16_pcm(primary: bytes, secondary: Optional[bytes]) -> bytes:
    if not secondary:
        return primary
    mic = np.frombuffer(primary, dtype=np.int16).astype(np.int32)
    media = np.frombuffer(secondary, dtype=np.int16).astype(np.float32)
    if len(media) < len(mic):
        media = np.pad(media, (0, len(mic) - len(media)))
    elif len(media) > len(mic):
        media = media[:len(mic)]
    with _media_input_lock:
        gain = _media_input_gain
    mixed = mic + (media * gain).astype(np.int32)
    return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()


def _mic_pump_loop():
    """持续从麦克风读 16kHz mono PCM，可选混入外部媒体输入，再推 audio_only 帧 → ws。

    paused 时直接 continue，不推任何帧——StartSession 已声明 input_mod=keep_alive，
    服务端不会因长时间不收音频踢会话，不需要再用零 PCM 偏方保活。
    """
    global _mic_input_stream
    if _mic_device_index is None:
        _log_fn(f"{C_ERR}[realtime_chat] 麦克风未设置{C_RESET}")
        return
    _mic_input_stream = _pa_instance.open(
        format=pyaudio.paInt16, channels=1, rate=16000,
        input=True, input_device_index=_mic_device_index,
        frames_per_buffer=3200,
    )
    while _mic_running:
        try:
            audio = _mic_input_stream.read(3200, exception_on_overflow=False)
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat·mic] {e}{C_RESET}")
            break
        # 无条件广播麦克风音量给 OBS 叠层（mio_block.html）—— pause 期间也广播，
        # 让 Mio 在游戏/聊天/下播全程都能用麦克风音量驱动透明度闪烁。
        # 老链路这条路在 lumi.py listen()/interrupt_monitor 里做的，端到端模式
        # 跳过了老链路，必须在这里补一条广播。
        if _event_bus is not None:
            try:
                chunk = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(chunk * chunk)))
                _event_bus.publish("mic_volume", {"volume": rms}, source="realtime_chat")
            except Exception:
                pass
        if _mic_paused:
            with _media_input_lock:
                _close_media_input_stream_locked()
            continue
        media_audio = _read_media_input(3200)
        if media_audio and _event_bus:
            try:
                media_chunk = np.frombuffer(media_audio, dtype=np.int16).astype(np.float32) / 32768.0
                _event_bus.publish(
                    "media_input_volume",
                    {"volume": float(np.sqrt(np.mean(media_chunk * media_chunk)))},
                    source="realtime_chat",
                )
            except Exception:
                pass
        audio = _mix_int16_pcm(audio, media_audio)
        try:
            sp = _resolve_speaker()
            rt = _sessions.get(sp)
            sid = rt.session_id if rt is not None else _session_id
            frame = proto.build_audio_frame(session_id=sid, audio=audio)
            _send_frame_for_speaker(frame, sp)
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat·mic] 推帧失败: {e}{C_RESET}")
    try:
        _mic_input_stream.stop_stream()
        _mic_input_stream.close()
    except Exception:
        pass
    with _media_input_lock:
        _close_media_input_stream_locked()


def start_mic_pump():
    """启动麦克风推送后台线程。需要先调 set_mic_device。"""
    global _mic_running, _mic_thread
    if _mic_device_index is None:
        raise RuntimeError("set_mic_device 还没调")
    _mic_running = True
    _mic_thread = threading.Thread(target=_mic_pump_loop, daemon=True, name="realtime-chat-mic")
    _mic_thread.start()
    _log_fn(f"{C_RT}[realtime_chat] 麦克风线程启动 device={_mic_device_index}（默认 paused）{C_RESET}")


def stop_mic_pump():
    global _mic_running
    _mic_running = False
    if _mic_thread:
        _mic_thread.join(timeout=2)


def pause(speaker: str = None):
    """停推麦克风音频，session 不关。

    **不清空** audio_in_queue 或 monitor_queue —— 让该 speaker 的 TTS 尾音
    自然播完。

    历史踩坑：曾经在这里清空过 audio_in_queue 来解决"双角色重叠说话"，
    但底层 swap race（_store_runtime_globals 错误覆盖 rt 永久字段）才是
    重叠的真根因，已在 899c440 修复。清空 queue 反而把合法尾音杀掉，
    用户体感"字幕完整显示但话说一半突然消失"——见 2026-05-10 日志
    cleared_audio=15 的实测。

    诊断保留：打印 queue 深度（不清），下次切麦时能看到旧 active 还剩
    多少未播帧。如果再现"重叠"，可以根据 queue 深度推迟切麦或更精细
    判断"真说完"。
    """
    global _mic_paused
    sp = _resolve_speaker(speaker)
    if speaker is None or sp == _active_speaker:
        _mic_paused = True
    # 仅观测队列深度，不清队列
    queue_depth_audio = 0
    queue_depth_monitor = 0
    rt = _sessions.get(sp)
    if rt is not None:
        queue_depth_audio = rt.audio_in_queue.qsize()
        queue_depth_monitor = rt.monitor_queue.qsize()
    else:
        queue_depth_audio = _audio_in_queue.qsize()
        queue_depth_monitor = _monitor_queue.qsize()
    # 诊断：毫秒时间戳 + last_done + 队列深度
    with _speech_done_lock:
        ld = _last_speech_done_at
    ld_delta = ld - time.time()
    _log_fn(
        f"{C_RT}[realtime_chat] paused {sp} 麦克风推送 "
        f"@ {time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} "
        f"last_done_delta={ld_delta:+.2f}s "
        f"queue_audio={queue_depth_audio} queue_monitor={queue_depth_monitor}{C_RESET}"
    )


def resume(speaker: str = None):
    """游戏 → 聊天切换：恢复推麦克风。"""
    global _mic_paused, _active_speaker
    if speaker:
        if speaker not in _sessions:
            raise ValueError(f"realtime_chat 未启动 speaker={speaker} 的 session")
        _active_speaker = speaker
    _mic_paused = False
    # 诊断：毫秒时间戳 + last_done（同 pause）
    with _speech_done_lock:
        ld = _last_speech_done_at
    ld_delta = ld - time.time()
    _log_fn(
        f"{C_RT}[realtime_chat] resumed {_active_speaker} 麦克风推送 "
        f"@ {time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} "
        f"last_done_delta={ld_delta:+.2f}s{C_RESET}"
    )


# ===== close_session + 重连（Task 11） =====
_should_reconnect = True
_reconnect_attempts = 0


def get_playback_queue_depth(speaker: str = None) -> tuple[int, int]:
    """Return pending playback queue depth for the speaker: (audio, monitor)."""
    sp = _resolve_speaker(speaker)
    rt = _sessions.get(sp)
    if rt is not None:
        return rt.audio_in_queue.qsize(), rt.monitor_queue.qsize()
    return _audio_in_queue.qsize(), _monitor_queue.qsize()


def close_session(speaker: str = None):
    """优雅关闭：发 FinishSession (102) + FinishConnection (2)，让服务端主动关 ws，
    _recv_loop 收到关闭后 break + _run_loop finally 关 loop，整套自然退出。

    不再投单独的 _do_close 协程等 fut.result —— 那个模式会在 _ws_loop 同时被 _recv_loop
    break 后的 finally 关闭时留下'Task was destroyed but it is pending'警告。
    现在改成 best-effort 投两个 send 协程（_send_frame_threadsafe 已经做这件事），
    再 join _ws_thread 等线程自然退出。
    """
    global _should_reconnect, _ws, _ws_alive
    if speaker is None and _sessions:
        for sp in list(_sessions.keys()):
            close_session(speaker=sp)
        stop_mic_pump()
        stop_audio_output()
        _sessions.clear()
        _speaker_output_devices.clear()
        _log_fn(f"{C_RT}[realtime_chat] close_session 完成{C_RESET}")
        return

    sp = _resolve_speaker(speaker)
    if sp in _sessions:
        _load_runtime_globals(sp)
    _should_reconnect = False
    if _ws_alive and _ws_loop is not None and _ws is not None:
        try:
            _send_frame_for_speaker(proto.build_event_frame(102, _session_id, {}), sp)
            time.sleep(0.3)
            _send_frame_for_speaker(proto.build_event_frame_no_session(2, {}), sp)
            time.sleep(0.2)
        except Exception:
            pass
    _ws_alive = False
    stop_mic_pump()
    stop_audio_output()
    # 关连接前清空所有归因状态，避免上游 speak() / wait_for_task 永久挂起
    _clear_attribution()
    # 等 ws 线程自然退出（_recv_loop break → _run_loop finally → _ws_loop.close()）
    if _ws_thread is not None and _ws_thread.is_alive():
        _ws_thread.join(timeout=2)
    _ws = None
    if sp in _sessions:
        _store_runtime_globals(sp)
    _log_fn(f"{C_RT}[realtime_chat] close_session 完成{C_RESET}")


def _connect_with_retry():
    """重连骨架。当前 _connect_async 是 spawn 线程的版本，重连真正生效要等 Phase 7 联调时迭代。

    本函数提供一个 backoff + 失败计数的接口，给后续把 _ws_connect_and_handshake 改成
    在 _run_loop 内重试时用。
    """
    global _reconnect_attempts
    backoff = [1, 2, 5, 10, 30]
    while _should_reconnect:
        try:
            _connect_async()
            _reconnect_attempts = 0
            return
        except Exception as e:
            _log_fn(f"{C_ERR}[realtime_chat] 连接失败: {e}, 第 {_reconnect_attempts + 1} 次重试{C_RESET}")
            wait = backoff[min(_reconnect_attempts, len(backoff) - 1)]
            time.sleep(wait)
            _reconnect_attempts += 1
            if _reconnect_attempts >= 5:
                if _event_bus:
                    _event_bus.publish("realtime_error", {
                        "type": "reconnect_failed",
                        "attempts": _reconnect_attempts,
                    }, source="realtime_chat")
                return
