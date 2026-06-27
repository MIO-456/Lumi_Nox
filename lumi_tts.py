"""Lumi TTS 模块 — 语音合成、播放、字幕显示

从 lumi.py 拆分出来。通过 init() 注入所有外部依赖，无反向 import。
"""

import os
import re
import json
import time
import queue
import threading
import asyncio
import traceback
from threading import Lock, Event
from collections import deque

import numpy as np
import pyaudiowpatch as pyaudio

from scipy.signal import resample_poly
from dotenv import load_dotenv

import realtime_chat
import run_architecture
import cosyvoice_tts
import 音色库
from tts_emitter import BorrowE2EEmitter, IndependentTTSEmitter

load_dotenv()

# ======= 终端颜色（复制自 lumi.py，避免反向依赖）=======
C_FAST = "\033[36m"
C_EMOTION = "\033[38;5;213m"
C_STATUS = "\033[33m"
C_ERR = "\033[31m"
C_RESET = "\033[0m"

# ======= 模块配置 =======
SUBTITLE_MS_PER_CHAR = 200   # 每字对应的音频时长（24kHz 语速估算）
SUBTITLE_LOOKAHEAD_MS = 300  # 字幕超前声音的提前量（字幕领先约 1-2 字）
ENABLE_SUBTITLE_DESKTOP = False
ENABLE_SUBTITLE_OBS = True


class TtsState:
    """TTS 运行状态，供外部模块读取"""
    def __init__(self):
        self.is_speaking: bool = False
        self.interrupted: Event = Event()
        self.last_speak_done_time: float = time.time()
        # 双角色：哪些 speaker 当前正在说话（idle_motion_loop 按 speaker 查避免不说话的角色也跟着动）
        self.speaking_speakers: set = set()

    def is_speaker_speaking(self, speaker: str = None) -> bool:
        """speaker=None 兼容单角色（任一角色在说就 True）；指定 speaker 时只看那个。"""
        if speaker:
            return speaker in self.speaking_speakers
        return self.is_speaking

tts_state = TtsState()
tts_ref_buffer: deque = deque()  # AEC 参考缓冲（TTS 写，ASR 读）
_subtitle_clear_ver = 0  # 字幕清除版本号，防止旧定时器误清新字幕

# ======= 注入依赖（由 init() 设置）=======
_log_fn = lambda msg: print(msg, flush=True)
_llm_client = None
_llm_model: str = ""
_brand_params_fn = None
_resolve_call_target_fn = None  # 带工具请求的模型回退解析（fast_brain.resolve_call_target）
_trigger_expression_fn = None
_pa_instance = None  # pyaudio.PyAudio
_sentence_endings = None  # compiled regex
_monitor_device_index = None
_event_bus = None  # 由 lumi.py 注入；speak() 用它发 logical tts_done 事件


def init(*, llm_client, llm_model: str, brand_params_fn,
         trigger_expression_fn, pa_instance,
         sentence_endings, log_fn=None,
         monitor_device_index=None, enable_subtitle_obs: bool = True,
         enable_subtitle_desktop: bool = False, event_bus=None,
         resolve_call_target_fn=None):
    """初始化模块依赖，由 lumi.py 在启动时调用一次"""
    global _log_fn, _llm_client, _llm_model, _brand_params_fn
    global _resolve_call_target_fn
    global _trigger_expression_fn, _pa_instance
    global _sentence_endings
    global _monitor_device_index, _event_bus
    global ENABLE_SUBTITLE_OBS, ENABLE_SUBTITLE_DESKTOP

    if log_fn:
        _log_fn = log_fn
    _llm_client = llm_client
    _llm_model = llm_model
    _brand_params_fn = brand_params_fn
    _resolve_call_target_fn = resolve_call_target_fn
    _trigger_expression_fn = trigger_expression_fn
    _pa_instance = pa_instance
    _sentence_endings = sentence_endings
    _monitor_device_index = monitor_device_index
    _event_bus = event_bus
    ENABLE_SUBTITLE_OBS = enable_subtitle_obs
    ENABLE_SUBTITLE_DESKTOP = enable_subtitle_desktop

    # 文本架构：独立 Qwen3-TTS 合成器复用同一套依赖（pyaudio / AEC 参考缓冲 / 监听 / 日志）。
    # 端到端架构下它不会被调用，init 无副作用。
    cosyvoice_tts.init(
        pa_instance=pa_instance,
        ref_buffer=tts_ref_buffer,
        monitor_device_index=monitor_device_index,
        log_fn=_log_fn,
    )


def set_monitor_device(index):
    """运行时更新监听设备索引"""
    global _monitor_device_index
    _monitor_device_index = index
    cosyvoice_tts.set_monitor_device(index)


# 独立 TTS 兜底音色（音色名解析失败时用）：CosyVoice 系统音色 + 模型。
INDEPENDENT_TTS_FALLBACK_VOICE_ID = "longanyang"
INDEPENDENT_TTS_FALLBACK_MODEL = "cosyvoice-v3-plus"


def _resolve_voice_name(speaker):
    """从 voice_config 取该角色的 voice_name（音色库 key）；取不到返回空，
    由 _make_emitter 回退到 CosyVoice 系统音色。"""
    try:
        from voice_config import get_speaker_config
        cfg = get_speaker_config(speaker) if speaker else None
        return (getattr(cfg, "voice_name", None) if cfg else None) or ""
    except Exception:
        return ""


def _make_emitter(speaker, cable_index):
    """按运行架构真相源选发声器：
    - 文本架构 → 独立 CosyVoice TTS（按角色 voice_name 经音色库解析 voice_id + model，
      支持百炼声音复刻音色）
    - 端到端架构 → 借端到端 say_streaming
    """
    if run_architecture.use_independent_tts():
        voice_name = _resolve_voice_name(speaker)
        try:
            voice_id = 音色库.get_voice_id(voice_name)
            model = 音色库.get_voice_model(voice_name)
        except Exception as e:
            _log_fn(f"{C_ERR}[独立TTS] 音色解析失败 '{voice_name}': {e}，回退系统音色{C_RESET}")
            voice_id = INDEPENDENT_TTS_FALLBACK_VOICE_ID
            model = INDEPENDENT_TTS_FALLBACK_MODEL
        return IndependentTTSEmitter(
            cosyvoice_tts.IndependentSynth(), voice_id=voice_id, model=model,
            cable_index=cable_index,
        )
    return BorrowE2EEmitter(realtime_chat, speaker)


def interrupt_current_speech():
    """Request the current speak() call to stop as soon as possible."""
    tts_state.interrupted.set()
    abort_subtitle_segments()


def _rescue_tool_call_json(raw: str) -> dict:
    """正则抢救损坏的 tool_call JSON — 提取关键字段避免兜底"""
    result = {}
    # 通用：提取所有 "key": "value" 对
    for m in re.finditer(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
        result[m.group(1)] = m.group(2)
    if result:
        _log_fn(f"{C_FAST}[快脑·JSON抢救] 从损坏JSON中提取到: {result}{C_RESET}")
    return result


# ======= 字幕系统 =======
subtitle_queue: queue.Queue = queue.Queue()


def run_subtitle_window():
    """置顶透明字幕窗口，贴在大屏幕底部，流式显示 Lumi 说话内容"""
    import tkinter as tk
    import tkinter.font as tkfont
    import mss

    def subtitle_log(msg: str, level=C_STATUS):
        full = f"{level}[字幕] {msg}{C_RESET}"
        try:
            _log_fn(full)
        except Exception:
            print(full, flush=True)

    def _trace_summary(limit=4):
        return " | ".join(line.strip() for line in traceback.format_exc(limit=limit).strip().splitlines())

    try:
        try:
            with mss.mss() as sct:
                mon = sct.monitors[2]
                mon_x, mon_y = mon["left"], mon["top"]
                mon_w, mon_h = mon["width"], mon["height"]
        except Exception:
            mon_x, mon_y, mon_w, mon_h = 0, 0, 1920, 1080
            subtitle_log("未拿到 monitors[2]，回退到 1920x1080", C_ERR)

        WIN_H = 220
        MARGIN_BOTTOM = 60
        FONT_FAMILY = "KeinannMaruPOP"
        FONT_WEIGHT = "bold"
        FONT_MAX_SIZE = 34
        FONT_MIN_SIZE = 14
        TEXT_WIDTH_RATIO = 0.72
        TEXT_PADDING_X = 48
        TEXT_PADDING_Y = 20
        BG = "#010101"
        TEXT_COLOR = "#F3D37A"
        OUTLINE_COLOR = "#15110F"

        root = tk.Tk()
        root.title("Lumi Subtitle")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", BG)
        root.configure(bg=BG)
        root.geometry(f"{mon_w}x{WIN_H}+{mon_x}+{mon_y + mon_h - WIN_H - MARGIN_BOTTOM}")

        canvas = tk.Canvas(root, bg=BG, highlightthickness=0, width=mon_w, height=WIN_H)
        canvas.pack(fill="both", expand=True)

        cx, cy = mon_w // 2, WIN_H // 2
        current = [""]
        text_box_width = int(mon_w * TEXT_WIDTH_RATIO)
        text_max_width = max(200, text_box_width - TEXT_PADDING_X * 2)
        text_max_height = WIN_H - TEXT_PADDING_Y * 2
        diag = {"font_logged": False, "first_draw_logged": False}

        try:
            families = set(tkfont.families(root))
            font_probe = tkfont.Font(root=root, family=FONT_FAMILY, size=FONT_MAX_SIZE, weight=FONT_WEIGHT)
            actual = font_probe.actual()
            subtitle_log(
                "启动成功 "
                f"monitor={mon_w}x{mon_h}@({mon_x},{mon_y}) "
                f"window_h={WIN_H} "
                f"font_req={FONT_FAMILY}/{FONT_WEIGHT} "
                f"font_available={FONT_FAMILY in families} "
                f"font_actual={actual.get('family')} "
                f"color={TEXT_COLOR} outline={OUTLINE_COLOR}"
            )
        except Exception:
            subtitle_log(f"字体探测失败: {_trace_summary()}", C_ERR)

        def wrap_text(font, text, max_width):
            lines = []
            for raw_line in text.splitlines() or [""]:
                if not raw_line:
                    lines.append("")
                    continue
                current_line = ""
                for ch in raw_line:
                    candidate = current_line + ch
                    if current_line and font.measure(candidate) > max_width:
                        lines.append(current_line)
                        current_line = ch
                    else:
                        current_line = candidate
                if current_line or not lines:
                    lines.append(current_line)
            return "\n".join(lines)

        def fit_subtitle(text):
            clean = (text or "").strip()
            if not clean:
                return None, "", 0

            best_font = None
            best_text = clean
            best_spacing = 0

            for size in range(FONT_MAX_SIZE, FONT_MIN_SIZE - 1, -1):
                font = tkfont.Font(root=root, family=FONT_FAMILY, size=size, weight=FONT_WEIGHT)
                wrapped = wrap_text(font, clean, text_max_width)
                line_count = max(1, wrapped.count("\n") + 1)
                linespace = font.metrics("linespace")
                spacing = max(0, int(size * 0.15))
                total_height = line_count * linespace + (line_count - 1) * spacing
                if total_height <= text_max_height:
                    best_font = font
                    best_text = wrapped
                    best_spacing = spacing
                    break

                best_font = font
                best_text = wrapped
                best_spacing = spacing

            return best_font, best_text, best_spacing

        def draw(text):
            try:
                canvas.delete("all")
                if not text:
                    return
                font, wrapped_text, spacing = fit_subtitle(text)
                if font is None:
                    return
                lines = wrapped_text.split("\n")
                line_height = font.metrics("linespace")
                total_height = len(lines) * line_height + max(0, len(lines) - 1) * spacing
                start_y = cy - (total_height / 2) + (line_height / 2)
                outline_offsets = [(-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, -2), (-2, 2), (2, 2)]
                if font.cget("size") <= 20:
                    outline_offsets = [(-1, 0), (1, 0), (0, -1), (0, 1)]
                main_ids = []
                for idx, line in enumerate(lines):
                    y = start_y + idx * (line_height + spacing)
                    for dx, dy in outline_offsets:
                        canvas.create_text(
                            cx + dx, y + dy, text=line, font=font,
                            fill=OUTLINE_COLOR, anchor="center",
                            tags=("subtitle_outline",)
                        )
                    main_ids.append(
                        canvas.create_text(
                            cx, y, text=line, font=font,
                            fill=TEXT_COLOR, anchor="center",
                            tags=("subtitle_main",)
                        )
                    )
                if not diag["first_draw_logged"]:
                    bbox = canvas.bbox("subtitle_main")
                    diag["first_draw_logged"] = True
                    subtitle_log(
                        "首次绘制成功 "
                        f"font_actual={font.actual().get('family')} "
                        f"size={font.cget('size')} "
                        f"lines={wrapped_text.count(chr(10)) + 1} "
                        f"bbox={bbox}"
                    )
            except Exception:
                subtitle_log(f"draw异常: {_trace_summary()}", C_ERR)

        def poll():
            try:
                while True:
                    msg = subtitle_queue.get_nowait()
                    current[0] = msg if msg is not None else ""
                    draw(current[0])
            except queue.Empty:
                pass
            except Exception:
                subtitle_log(f"poll异常: {_trace_summary()}", C_ERR)
            finally:
                try:
                    root.after(30, poll)
                except Exception:
                    subtitle_log(f"after调度失败: {_trace_summary()}", C_ERR)

        root.after(30, poll)
        root.mainloop()
    except Exception:
        subtitle_log(f"字幕线程崩溃: {_trace_summary(limit=6)}", C_ERR)


def start_subtitle_window():
    """在后台线程启动字幕窗口"""
    t = threading.Thread(target=run_subtitle_window, daemon=True, name="subtitle-window")
    t.start()
    return t


# ======= 字幕 OBS WebSocket 服务器 =======
_subtitle_ws_clients: set = set()
_subtitle_ws_loop = None
_subtitle_ws_ready = threading.Event()
_subtitle_ws_queue: queue.Queue = queue.Queue()
_subtitle_ws_last_msg = json.dumps({"type": "clear"}, ensure_ascii=False)

# ======= 麦克风实时音量广播（给 mio_block 等 OBS 叠层用）=======
# 由 lumi.py 的两个麦克风读取循环（listen_for_speech / 打断监听）调用，
# WS 客户端（mio_block.html）订阅 mic_volume 消息驱动透明度。
_last_mic_volume_ts = [0.0]


def broadcast_mic_volume(volume: float):
    """把麦克风当前 RMS 音量推给所有 WS 客户端。volume 期望已归一化到 0..1。
    内置 ~33ms 节流，可在调用方任意频率调用，不会刷爆 WS。"""
    if not _subtitle_ws_loop or not _subtitle_ws_clients:
        return
    now_ts = time.time()
    if now_ts - _last_mic_volume_ts[0] < 0.033:
        return
    _last_mic_volume_ts[0] = now_ts
    try:
        _subtitle_ws_queue.put(json.dumps(
            {"type": "mic_volume", "volume": float(volume)},
            ensure_ascii=False,
        ))
    except Exception:
        pass


_subtitle_last_meta_logged = None  # 仅当 meta speaker 切换时打印一次，避免每帧刷屏


def _subtitle_ws_broadcast(text, meta: dict = None):
    """向所有 OBS 字幕 WS 客户端推送字幕内容（线程安全，通过队列）。

    meta: 可选 dict，含 speaker / color / label，浏览器端按字段切颜色和前缀；
          单角色场景或不需要区分时传 None，浏览器端用默认样式。
    """
    global _subtitle_ws_last_msg, _subtitle_last_meta_logged
    if text is None:
        msg = json.dumps({"type": "clear"}, ensure_ascii=False)
    else:
        payload = {"type": "subtitle", "text": text}
        if meta:
            payload.update({k: v for k, v in meta.items() if v is not None})
        msg = json.dumps(payload, ensure_ascii=False)
        # 仅当 speaker 切换时打印一次（用于调试双角色字幕路由是否生效）
        _meta_speaker = (meta or {}).get("speaker")
        if _meta_speaker and _meta_speaker != _subtitle_last_meta_logged:
            _meta_color = (meta or {}).get("color")
            _meta_label = (meta or {}).get("label")
            _log_fn(f"{C_STATUS}[字幕] 切到 {_meta_speaker} ({_meta_color} 「{_meta_label}」){C_RESET}")
            _subtitle_last_meta_logged = _meta_speaker
    _subtitle_ws_last_msg = msg
    if not _subtitle_ws_loop or not _subtitle_ws_clients:
        return
    _subtitle_ws_queue.put(msg)


# ======= 字幕光标推进（端到端 350 事件 → 按字推进 + lookahead）=======
# 端到端 TTS 服务端按句返回 TTSSentenceStart(350)，里面带这一句的完整文本但没有"这一句要
# 播多久"的精确数字。整句一次性渲染会"啪一下出现"，没有字符流入感；老主线本地 TTS 是按字
# 推进 + lookahead 的，所以这里复刻同样的体验。
#
# "每字多少毫秒"由 351(TTSSentenceEnd) 时算出来的物理时长（音频字节数 ÷ 96000）÷ 字数
# 标定，跟当前音色真实语速一致；第一句来不及标定时用 200ms/字 兜底（基本接近大多数中文
# TTS 节奏）。
#
# 关键：服务端生成速度比客户端播放快好几倍，多个 350 事件可能在很短时间内连发过来。如果每
# 个 350 立即开光标推进，下一个 350 来时上一句还没推完，就会发生覆盖闪烁。所以用一个串行
# 队列：每段排在前一段的"估算播完时刻"之后开始，自然跟随播放节奏。
_subtitle_segment_queue: queue.Queue = queue.Queue()
_subtitle_cursor_thread: threading.Thread = None
_subtitle_cursor_lock = Lock()
# 打断信号——上层（聊天的 459 用户说话回调 / 游戏的 speak() interrupt 路径）调
# abort_subtitle_segments() 时设置；worker 在三个时机检查它：刚 pop 出新段、等
# next_segment_can_start_at 期间、按字推进的内循环每一帧。
_subtitle_cursor_abort = Event()

# 每字多少毫秒——初始用老主线常量兜底，从端到端第一个 351 事件开始用实测值覆盖。
# 单一来源：lumi_tts 模块全局，realtime_chat 351 → lumi.py 转发 → 这里更新。
_realtime_ms_per_char: float = float(SUBTITLE_MS_PER_CHAR)


def calibrate_realtime_ms_per_char(duration_s: float, text: str):
    """端到端 TTSSentenceEnd 事件触发时调用——用"这一句的音频字节数 ÷ 96000"得到的精
    确秒数和字数，反推当前音色"每字多少毫秒"，覆盖默认值，让下一段字幕推进按真实语速走。

    入参合法性兜底：duration 非正、文本为空、计算出的每字时长落在 50–800ms 这个合理范围
    外，都不接受（避免审核段或异常段把节拍拉飞）。
    """
    global _realtime_ms_per_char
    if duration_s <= 0 or not text:
        return
    chars = len(text)
    if chars <= 0:
        return
    candidate = duration_s * 1000.0 / chars
    if 50.0 <= candidate <= 800.0:
        _realtime_ms_per_char = candidate


def enqueue_subtitle_segment(text: str, meta: dict = None):
    """业务层接收到端到端 TTSSentenceStart 事件后调用。把这一句排进字幕光标队列，
    worker 会按当前标定的"每字毫秒"节奏推进 + 领先 300ms 广播前缀。空文本直接丢弃。"""
    if not text:
        return
    _subtitle_segment_queue.put((text, dict(meta) if meta else {}))
    _ensure_subtitle_cursor_worker()


def abort_subtitle_segments():
    """打断时调：清空待推队列 + 让 cursor worker 立即终止当前段 + OBS 字幕清屏。

    时序保证：调完此函数后，未来的 enqueue_subtitle_segment 会被 worker 正常处理——
    worker 在每段开始处理之前主动 clear abort 事件，新段不会被旧的中止信号带歪。

    使用场景：
    - 聊天环节用户开口打断 Lumi 当前回复（459 user_speech_done + Lumi 还在说话）
    - 游戏环节 VAD 监听到打断，speak() 提前退出
    """
    drained = 0
    while True:
        try:
            _subtitle_segment_queue.get_nowait()
            drained += 1
        except queue.Empty:
            break
    _subtitle_cursor_abort.set()
    _subtitle_ws_broadcast(None)
    if drained > 0:
        try:
            _log_fn(f"[字幕] 打断：清掉 {drained} 段待推 + 中止当前段")
        except Exception:
            pass


def _ensure_subtitle_cursor_worker():
    """惰性启动字幕光标 worker —— 第一次调 enqueue_subtitle_segment 时启动一次。"""
    global _subtitle_cursor_thread
    with _subtitle_cursor_lock:
        if _subtitle_cursor_thread is not None and _subtitle_cursor_thread.is_alive():
            return
        _subtitle_cursor_thread = threading.Thread(
            target=_subtitle_cursor_worker_loop,
            daemon=True,
            name="realtime-subtitle-cursor",
        )
        _subtitle_cursor_thread.start()


def _subtitle_cursor_worker_loop():
    """串行处理 _subtitle_segment_queue 里的每一段：
    - 段开始时读一次 _realtime_ms_per_char 锁定本段节奏，避免标定值中途变化引起光标跳变
    - 在 max(now, 上一段播完时刻) 才开始本段，跟住播放节奏不超前
    - 本段内每 30ms 算一次"光标该到第几字"= (已过去毫秒 + LOOKAHEAD) / 段内 ms_per_char
    - 光标位置变化才广播一次，避免刷爆 WS
    - 推进到末尾后更新"播完时刻" = 段开始时刻 + 字数 × 段内 ms_per_char

    打断响应：在三个时机检查 _subtitle_cursor_abort 事件——刚 pop 出新段、等待下一段开
    始那段 sleep、按字推进的内循环每一帧。任何一个时机检测到中止就重置时序、清掉中止信号、
    放弃当前段，回到 get() 等下一段（abort_subtitle_segments 已经把队列清干净）。
    """
    next_segment_can_start_at = time.time()
    while True:
        text, meta = _subtitle_segment_queue.get()  # 阻塞等下一段
        # 中止可能是在 get() 阻塞期间触发的——这种情况下队列被清干净了之后又有新段进来；
        # 检查并清掉旧的 abort 信号，让新段从干净状态开始
        if _subtitle_cursor_abort.is_set():
            _subtitle_cursor_abort.clear()
            next_segment_can_start_at = time.time()

        # 跟住前一段的"播完时刻"，但中止可在 wait 期间发生，所以用 abort.wait 而不是 sleep
        wait = next_segment_can_start_at - time.time()
        if wait > 0 and _subtitle_cursor_abort.wait(wait):
            _subtitle_cursor_abort.clear()
            next_segment_can_start_at = time.time()
            continue  # 等待期间被中止 → 跳过本段，等队列里的新段

        # 进入推进前再次确保中止信号是干净的
        _subtitle_cursor_abort.clear()

        # 锁定本段节奏：第一句用默认 200ms/字 兜底，之后用聊天 559 标定的实测音色语速
        ms_per_char = _realtime_ms_per_char
        seg_start_at = time.time()
        seg_duration_s = max(len(text) * ms_per_char, 200) / 1000.0
        next_segment_can_start_at = seg_start_at + seg_duration_s

        last_cursor = -1
        aborted = False
        while True:
            if _subtitle_cursor_abort.is_set():
                aborted = True
                break
            elapsed_ms = (time.time() - seg_start_at) * 1000.0
            cursor = min(
                len(text),
                int((elapsed_ms + SUBTITLE_LOOKAHEAD_MS) // ms_per_char),
            )
            if cursor > last_cursor:
                _subtitle_ws_broadcast(text[:cursor], meta=meta)
                last_cursor = cursor
            if cursor >= len(text):
                break
            time.sleep(0.03)

        if aborted:
            _subtitle_cursor_abort.clear()
            # 中止后下一段应立即开始（音频也停了）
            next_segment_can_start_at = time.time()


def run_subtitle_ws_server(port: int = 8767):
    """在后台线程运行字幕 WebSocket + overlay HTTP 服务器"""
    global _subtitle_ws_loop
    from aiohttp import web

    async def _ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        _subtitle_ws_clients.add(ws)
        await ws.send_str(_subtitle_ws_last_msg)
        _log_fn(f"{C_STATUS}[字幕OBS] 客户端连接 (共 {len(_subtitle_ws_clients)}){C_RESET}")
        try:
            async for msg in ws:
                pass
        finally:
            _subtitle_ws_clients.discard(ws)
        return ws

    async def _main():
        ws_app = web.Application()
        ws_app.router.add_get("/ws", _ws_handler)
        ws_runner = web.AppRunner(ws_app, access_log=None)
        await ws_runner.setup()
        ws_site = web.TCPSite(ws_runner, "0.0.0.0", port)
        await ws_site.start()

        http_app = web.Application()
        overlay_dir = os.path.join(os.path.dirname(__file__), "overlay")
        # 美术素材/ 被 .gitignore 忽略 —— main 仓库下有，worktree 切出来时没有。
        # 优先用本地（main 直接命中），找不到时上溯到 main 仓库（worktree 上溯两级
        # 到 <project-root>/）。这样 worktree 跑直播也能拿到 mio_block 等叠层的图片。
        _local_assets = os.path.join(os.path.dirname(__file__), "美术素材")
        _parent_assets = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "美术素材")
        )
        if os.path.isdir(_local_assets):
            assets_dir = _local_assets
        elif os.path.isdir(_parent_assets):
            assets_dir = _parent_assets
        else:
            assets_dir = _local_assets  # 都没有就用本地路径，HTTP 路由会 404

        async def _overlay_file_handler(request):
            file_name = request.match_info.get("filename", "")
            file_path = os.path.join(overlay_dir, file_name)
            if os.path.isfile(file_path):
                _log_fn(f"{C_STATUS}[Overlay] HTTP 请求: {file_name} ← {request.remote}{C_RESET}")
                return web.FileResponse(file_path)
            return web.Response(status=404, text="Not Found")

        async def _assets_file_handler(request):
            """对外暴露 美术素材/ 目录（mio_block.html 等叠层取图用），支持子目录。"""
            rel_path = request.match_info.get("filename", "")
            file_path = os.path.normpath(os.path.join(assets_dir, rel_path))
            if not file_path.startswith(os.path.normpath(assets_dir)):
                return web.Response(status=403, text="Forbidden")
            if os.path.isfile(file_path):
                return web.FileResponse(file_path)
            return web.Response(status=404, text="Not Found")

        http_app.router.add_get("/assets/{filename:.*}", _assets_file_handler)
        http_app.router.add_get("/{filename}", _overlay_file_handler)
        http_runner = web.AppRunner(http_app, access_log=None)
        await http_runner.setup()
        http_site = web.TCPSite(http_runner, "0.0.0.0", port + 1)
        await http_site.start()

        _log_fn(f"{C_STATUS}[字幕OBS] 服务器已启动: http://localhost:{port + 1}/subtitle.html  ws://localhost:{port}/ws{C_RESET}")
        _subtitle_ws_ready.set()

        while True:
            while not _subtitle_ws_queue.empty():
                try:
                    msg = _subtitle_ws_queue.get_nowait()
                    dead = set()
                    for ws in list(_subtitle_ws_clients):
                        try:
                            await ws.send_str(msg)
                        except Exception:
                            dead.add(ws)
                    _subtitle_ws_clients.difference_update(dead)
                except queue.Empty:
                    break
            await asyncio.sleep(0.03)

    _subtitle_ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_subtitle_ws_loop)

    def _handle_loop_exception(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError):
            return
        loop.default_exception_handler(context)

    _subtitle_ws_loop.set_exception_handler(_handle_loop_exception)
    try:
        _subtitle_ws_loop.run_until_complete(_main())
    except Exception:
        _log_fn(f"{C_ERR}[字幕OBS] WS 服务器崩溃:\n{traceback.format_exc()}{C_RESET}")


def start_subtitle_ws_server(port: int = 8767):
    """在后台线程启动字幕 WS 服务器"""
    t = threading.Thread(target=run_subtitle_ws_server, args=(port,), daemon=True, name="subtitle-ws")
    t.start()
    return t


# ======= 核心：LLM 流式生成 + TTS 流式播放 =======
# ===== speak_legacy task_id 桥（lumi.py 用）=====
_pending_speak_legacy_task_id: str = ""


def set_pending_speak_legacy_task_id(task_id: str):
    """lumi.py 在老链路 director_instruction 触发时，预先生成 task_id 寄存到本模块。

    下一次 speak() 进入时消费一次。这样 director publish 的 director_instruction_dispatched
    携带的 task_id 与 speak() 末尾 publish 的 tts_done(speak_legacy) task_id 是同一个。
    """
    global _pending_speak_legacy_task_id
    _pending_speak_legacy_task_id = task_id


def _consume_pending_speak_legacy_task_id() -> str:
    """speak() 进入时消费寄存的 task_id；没有就返回空串，调用方退化到本地生成。"""
    global _pending_speak_legacy_task_id
    tid = _pending_speak_legacy_task_id
    _pending_speak_legacy_task_id = ""
    return tid


def _wait_segments_or_interrupt(done_event: Event, interrupted_event: Event,
                                 timeout: float = 30.0,
                                 poll_interval: float = 0.2,
                                 estimated_done_at_fn=None) -> str:
    """speak 末尾的复合等待：所有段完成 OR 用户打断 OR 超时——任一满足立即返回。

    返回：'done' / 'interrupted' / 'estimated_done' / 'timeout'。

    单纯 done_event.wait(timeout) 收不到打断信号，被打断的 speak 会熬完 30 秒才退。
    轮询模式让 200ms 内能感知打断，speak 退出后 ASR 链路立刻能接手。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        wait = min(poll_interval, max(remaining, 0.0))
        if done_event.wait(timeout=wait):
            return "done"
        if interrupted_event.is_set():
            return "interrupted"
        if estimated_done_at_fn is not None:
            try:
                estimated_done_at = float(estimated_done_at_fn() or 0.0)
            except Exception:
                estimated_done_at = 0.0
            if estimated_done_at > 0 and time.time() >= estimated_done_at:
                return "estimated_done"
    # done_event 收完会立刻返回；这里是真超时
    return "timeout"


def _is_runaway_repeat(text: str, tail: int = 36, min_span: int = 14, max_unit: int = 12) -> bool:
    """检测文本末尾是否在高频重复一个短串（如"算了算了算了…"）—— LLM 退化复读特征。

    判定：末尾正好是某个 1~max_unit 长度的"单元"连续重复、且重复总长 ≥ min_span 个字符。
    用 min_span（而非固定次数）是为了**不误伤**正常的"哈哈哈""好好好"——单字符要连续 14 个
    才算，两字符要 7 组（"算了"×7），正常笑/强调达不到，但退化复读很快就够。空白单元不算。
    """
    t = text[-tail:]
    n = len(t)
    for unit in range(1, max_unit + 1):
        repeats = max(3, -(-min_span // unit))  # ceil(min_span/unit)，至少 3 次
        need = unit * repeats
        if n >= need:
            seg = t[-unit:]
            if seg.strip() and t[-need:] == seg * repeats:
                return True
    return False


def speak(messages, vad_model=None, cable_index=None,
          max_tokens=200, temperature=1.0,
          tools=None, tool_result_holder=None,
          on_tool_calls_parsed=None,
          interrupt_monitor_fn=None,
          turn_metrics=None,
          voice_name: str = None,
          subtitle_meta: dict = None,
          output_id: str = None,
          is_output_current_fn=None,
          max_chars: int | None = None) -> str:
    """LLM 流式生成 → 按句切 → 借端到端 ChatTTSText 合成（音色和聊天端到端一致），返回完整回复文本

    端到端集成后，TTS 音频从 realtime_chat 模块的播放线程统一输出（虚拟声卡 + 监听 + AEC），
    speak() 不再开音频流。每次进入按句调 realtime_chat.say_streaming(...)，所有段播完后
    publish 一次 "tts_done"（source="speak_legacy"），供 director 等待这次 speak 触发的"逻辑回复"结束。

    保留：LLM 流式输出 / tool_calls 流式累积 + 提前触发 / 情绪标签解析 / 打断监听 / turn_metrics
    放弃（已知能力变化）：char-by-char 字幕光标推进 — 改由 realtime_chat 的 TTSSentenceStart
                          事件按句推送字幕（粒度从字变成句）

    cable_index / voice_name 入参保留兼容老调用方，本路径下不实际使用（音频走 realtime_chat 的
    set_output_devices 配置；音色走 start_session 时的 voice_id）。
    """
    global _subtitle_clear_ver

    # 本次 speak 的逻辑 task_id：优先消费 lumi.py 在 director_instruction 老链路分支寄存的 id；
    # 没有就本地生成。最终 publish tts_done 用这个 id，让 director 按 task_id 等待。
    _logical_task_id = (
        _consume_pending_speak_legacy_task_id()
        or f"speak_legacy_{int(time.time() * 1000)}_{id(messages)}"
    )
    if turn_metrics is not None:
        turn_metrics["task_id"] = _logical_task_id

    tts_state.is_speaking = True
    _current_speaker = (subtitle_meta or {}).get("speaker")
    if _current_speaker:
        tts_state.speaking_speakers.add(_current_speaker)
    _subtitle_clear_ver += 1  # 新一轮说话，使旧的清除定时器失效
    tts_state.interrupted.clear()
    _log_fn(f"[时序·speak进入] {time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} task={_logical_task_id}")
    # 不清 tts_ref_buffer：老链路 buffer 在端到端集成后已无写入方，留空不影响

    # 运行架构分流：文本架构走独立 Qwen3-TTS（_indep_emitter 自管发声+播完），
    # 端到端架构走下方原借端到端逻辑（_pending_segment_task_ids 那套）一行不改。
    _use_indep_tts = run_architecture.use_independent_tts()
    _indep_emitter = _make_emitter(_current_speaker, cable_index) if _use_indep_tts else None

    # 累积的干净文本（由 _output_filtered 追加）— 仍然累积，只为 return + log，不再驱动字幕光标
    _full_text = ""

    start = time.time()
    full_reply = ""

    # 端到端 ChatTTSText 流式：整个一次 speak 对应一个 reply_id（一个 client task_id）。
    # 多句拼接到同一个流里，服务端把整段视作一份合成，不再因新句子取消旧句子。
    # _pending_segment_task_ids 现在最多 1 个元素（首次有内容 flush 时由 say_streaming 入队），
    # _remaining_segments 也是。保留这两个名字是为了不动等待逻辑（_segments_done_event 仍然
    # 在收到这唯一一段的 tts_segment_done 时 set）。
    _pending_segment_task_ids: list = []
    _remaining_segments: set = set()
    _streaming_task_id = [None]   # 包装成 list 以便闭包内可写
    _segments_lock = Lock()
    _segments_done_event = Event()
    _llm_done = [False]   # LLM 流是否已结束（_remaining_segments 为空且 _llm_done 才算"逻辑回复完整"）
    _estimated_tts_done_at = [0.0]  # 服务端漏发 359 时，本地按文本长度估算播完时间兜底

    def _on_segment_done(evt):
        tid = evt.data.get("task_id")
        with _segments_lock:
            # 只关心本次 speak 自己派出去的那批流水号。被打断的旧 speak 迟到的回流、
            # 或者其他路径（auto_chat / inject）的 segment_done 都会经过这条订阅，
            # 但不属于本次 speak —— 直接静默跳过，不要污染日志。
            if tid not in _pending_segment_task_ids:
                return
            if tid in _remaining_segments:
                _remaining_segments.discard(tid)
            else:
                # 流水号在本次 speak 的全集里、却不在还没收到的清单里 —— 服务端发了重复 359。
                # 罕见情况，仅记一行用于观测，不影响推进。
                _log_fn(
                    f"[时序·segment_done] 重复 收到 task={tid} 已经在更早 publish 过"
                )
            if _llm_done[0] and not _remaining_segments:
                _segments_done_event.set()

    if _event_bus is not None:
        _event_bus.subscribe("tts_segment_done", _on_segment_done)

    # LLM 流式生成
    bp = _brand_params_fn()
    _call_client, _call_model = _llm_client, _llm_model
    # 选中模型不支持工具时，带工具的游戏决策请求回退到支持工具的模型；聊天请求仍用选中模型。
    if tools and _resolve_call_target_fn is not None:
        _call_client, _call_model, bp = _resolve_call_target_fn(needs_tools=True)
    llm_kwargs = dict(
        model=_call_model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=max_tokens,
        temperature=temperature if temperature != 1.0 else bp["temperature"],
        top_p=bp["top_p"],
        frequency_penalty=bp["frequency_penalty"],
        presence_penalty=bp["presence_penalty"],
    )
    if bp["logit_bias"]:
        llm_kwargs["logit_bias"] = bp["logit_bias"]
    if bp["extra_body"]:
        llm_kwargs["extra_body"] = bp["extra_body"]
    if tools:
        llm_kwargs["tools"] = tools
        llm_kwargs["tool_choice"] = "auto"  # 允许同时输出文本和 tool_call
    response = _call_client.chat.completions.create(**llm_kwargs)

    # 启动打断监听
    interrupt_thread = None
    if vad_model is not None and interrupt_monitor_fn is not None:
        interrupt_thread = threading.Thread(
            target=interrupt_monitor_fn, args=(vad_model,), daemon=True,
        )
        interrupt_thread.start()

    first_token = True
    _tts_bracket = False    # 括号过滤状态
    _tts_bracket_buf = ""
    tts_buffer = ""         # 干净文本缓冲，只收集过滤后的内容

    def _output_filtered(text):
        """流式输出，实时过滤括号内容；只累积 _full_text，字幕推送由光标线程接管"""
        nonlocal _tts_bracket, _tts_bracket_buf, _full_text
        out = ""
        for ch in text:
            # 括号内容一律剥掉：不念出来、也不累积进 _full_text（不进对话历史）。
            # 除圆括号外，把【】[] 也纳入——模型偶发用【动作】/[动作] 描写动作，一旦进历史
            # 会被下一轮自回归学样、一发不可收（实测 Lumi 用【】包裹动作后全程污染）。
            if ch in '（(【[':
                _tts_bracket = True
                _tts_bracket_buf = ch
            elif ch in '）)】]' and _tts_bracket:
                _tts_bracket = False
                _tts_bracket_buf = ""
            elif _tts_bracket:
                _tts_bracket_buf += ch
            else:
                out += ch
        if out:
            # 不再逐字流式 print（旧逻辑因管道行缓冲会延迟到下一轮才刷、且和按句 [X·说] 日志重复）。
            # 只累积 _full_text 供 return；终端输出统一走 _flush_tts 里的按句 [X·说] 日志。
            _full_text += out
        return out

    # 过滤 LLM 误输出的 tool_call 文本（如 shoot{"target":"dealer"}）
    _toolcall_text_re = re.compile(
        r'(?:shoot|use_item)\s*\{[^}]*\}|'       # shoot{"target":"dealer"}
        r'(?:^|\s)(?:shoot|use_item)\s*$',         # 独立的 shoot / use_item
        re.IGNORECASE
    )

    def _flush_tts():
        """干净文本缓冲到端到端 ChatTTSText 流（按句切）；括号内部不 flush。
        多句共属一个流：首次 flush 发 start=true 开流，后续 flush 发中间包；末尾包由
        LLM 流结束后的收尾分支统一发。"""
        nonlocal tts_buffer
        if _tts_bracket:
            return
        sentence = tts_buffer.strip()
        if sentence:
            sentence = _toolcall_text_re.sub("", sentence).strip()
        if sentence and not tts_state.interrupted.is_set():
            _estimated_tts_done_at[0] = max(
                _estimated_tts_done_at[0],
                time.time() + max(len(sentence) / 4.0 + 2.0, 3.0),
            )
            # 字幕：游戏环节没 LLM 流（500 路径拿不到文本），所以这里在调 say_streaming
            # 之前手动喂一句给字幕光标 worker，按字推进 + lookahead 跟聊天一致
            enqueue_subtitle_segment(sentence, meta=subtitle_meta)
            # 按句打日志（带换行→即使后面卡住/复读也能在日志里看到这句）：解决"整段说完才
            # 进日志、卡死时没记录"的问题。
            _now_say = time.time()
            _log_fn(
                f"{C_FAST}[{time.strftime('%H:%M:%S', time.localtime(_now_say))}."
                f"{int(_now_say*1000)%1000:03d}][{_current_speaker or '?'}·说] {sentence}{C_RESET}"
            )
            if _use_indep_tts:
                # 文本架构：喂给独立 Qwen3-TTS（首句 open 流），不进借端到端的分段跟踪
                if _indep_emitter.stream_task_id is None and turn_metrics is not None:
                    turn_metrics["tts_audio_start_at"] = time.time()
                _indep_emitter.feed(sentence)
            else:
                is_first = (_streaming_task_id[0] is None)
                if is_first and turn_metrics is not None:
                    turn_metrics["tts_audio_start_at"] = time.time()
                if is_first:
                    _now = time.time()
                    _log_fn(
                        f"[时序·speak首段送TTS] {time.strftime('%H:%M:%S', time.localtime(_now))}."
                        f"{int(_now*1000)%1000:03d} sentence_len={len(sentence)} "
                        f"speaker={_current_speaker}"
                    )
                seg_task_id = realtime_chat.say_streaming(
                    sentence, is_first=is_first, is_last=False,
                    task_id=_streaming_task_id[0],
                    speaker=_current_speaker,
                )
                if is_first:
                    _streaming_task_id[0] = seg_task_id
                    with _segments_lock:
                        _remaining_segments.add(seg_task_id)
                    _pending_segment_task_ids.append(seg_task_id)
        tts_buffer = ""

    _fb_usage = None
    # tool_call 流式累积
    _tc_chunks = {}  # {index: {"name": str, "arguments": str}}
    ttft = 0.0
    for chunk in response:
        if tts_state.interrupted.is_set():
            break
        # 捕获流式 usage（最后一个 chunk 携带）
        if hasattr(chunk, "usage") and chunk.usage is not None:
            _fb_usage = chunk.usage
        if not chunk.choices:
            continue
        delta_obj = chunk.choices[0].delta
        # 累积 tool_call chunks
        if hasattr(delta_obj, "tool_calls") and delta_obj.tool_calls:
            for tc in delta_obj.tool_calls:
                idx = tc.index if hasattr(tc, "index") else 0
                if idx not in _tc_chunks:
                    _tc_chunks[idx] = {"name": "", "arguments": ""}
                if hasattr(tc.function, "name") and tc.function.name:
                    _tc_chunks[idx]["name"] = tc.function.name
                if hasattr(tc.function, "arguments") and tc.function.arguments:
                    _tc_chunks[idx]["arguments"] += tc.function.arguments
        delta = delta_obj.content
        if delta:
            if first_token:
                ttft = time.time() - start
                first_token = False
                _now = time.time()
                _log_fn(
                    f"[时序·speak首token] {time.strftime('%H:%M:%S', time.localtime(_now))}."
                    f"{int(_now*1000)%1000:03d} ttft={ttft:.2f}s speaker={_current_speaker}"
                )
                if turn_metrics is not None:
                    turn_metrics["fast_brain_ttft_ms"] = round(ttft * 1000, 1)
                    if "e2e_start" in turn_metrics:
                        turn_metrics["e2e_ms"] = round((time.time() - turn_metrics["e2e_start"]) * 1000, 1)
            full_reply += delta

            # 文本从第一个字就直接走括号过滤进 TTS 缓冲。早期那道"等情感标签 [开心] 出现/攒够
            # 20 字才放行"的门已废弃（2026-06-05 起表情统一交 emotion_sidecar，快脑不再输出标签），
            # 留着反而会把 ≤20 字短回复永久卡死。偶发漏出的 [动作]/【动作】仍由 _output_filtered 剥掉。
            tts_buffer += _output_filtered(delta)

            # 遇到句末标点且不在括号内 → flush TTS
            if _sentence_endings.search(delta):
                _flush_tts()

            # 字数硬截：游戏环节等场景给 max_chars 限制超长复读。
            # 注：max_tokens 限服务端 token，max_chars 限客户端有效中文字数。
            # 截断后跳出循环，残余 tts_buffer 会走结尾分支作为末尾包发出去，
            # tool_call 的累积已经在 _tc_chunks 里，不受影响。
            if max_chars is not None and len(_full_text) >= max_chars:
                _log_fn(
                    f"[字数截断] 累积 {len(_full_text)} 字 ≥ {max_chars}，停止接收 LLM 流"
                )
                break

            # 复读检测：末尾短串高频重复（如"算了算了算了算了"）→ 立刻 break，不等字数/token
            # 跑满。豆包 2.0-mini 偶发退化复读，否则 TTS 会把整段复读念出来（实测读了约一分钟）。
            if _is_runaway_repeat(_full_text):
                _log_fn(
                    f"{C_ERR}[复读检测] 检测到退化复读，提前截断（已出 {len(_full_text)} 字）："
                    f"…{_full_text[-24:]}{C_RESET}"
                )
                break

    # 记录快脑 token 用量
    if turn_metrics is not None:
        turn_metrics["reply_text_done_at"] = time.time()
    if _fb_usage and turn_metrics is not None:
        turn_metrics["fast_brain_input_tokens"] = getattr(_fb_usage, "prompt_tokens", 0) or getattr(_fb_usage, "input_tokens", 0)
        turn_metrics["fast_brain_output_tokens"] = getattr(_fb_usage, "completion_tokens", 0) or getattr(_fb_usage, "output_tokens", 0)
        # 缓存命中验证（豆包 2.0 系列自动隐式缓存）
        _ptd = getattr(_fb_usage, "prompt_tokens_details", None)
        _cached = 0
        if _ptd is not None:
            if isinstance(_ptd, dict):
                _cached = _ptd.get("cached_tokens", 0) or 0
            else:
                _cached = getattr(_ptd, "cached_tokens", 0) or 0
        turn_metrics["fast_brain_cached_tokens"] = _cached

    # 末尾残余 + 收尾：LLM 流结束（无论被打断与否）。
    # 协议要求：被打断且最后一包尚未发送 → 不发末尾包，避免服务端流程异常。
    if not tts_state.interrupted.is_set() and not _tts_bracket:
        tts_buffer_final = _toolcall_text_re.sub("", tts_buffer).strip()
        if _use_indep_tts:
            # 文本架构：末尾残余喂给独立 TTS；finish() 在下方统一调（阻塞到播完）
            if tts_buffer_final:
                _estimated_tts_done_at[0] = max(
                    _estimated_tts_done_at[0],
                    time.time() + max(len(tts_buffer_final) / 4.0 + 2.0, 3.0),
                )
                enqueue_subtitle_segment(tts_buffer_final, meta=subtitle_meta)
                if _indep_emitter.stream_task_id is None and turn_metrics is not None:
                    turn_metrics["tts_audio_start_at"] = time.time()
                _indep_emitter.feed(tts_buffer_final)
        elif _streaming_task_id[0] is None:
            # 整段 LLM 流没切句过（短回复 / 没标点结尾）→ 把残余作为开始 + 结束一气合成
            # 协议没明示能不能 start=true & end=true 同包，安全做法是分两包（先 start、再 end 空包）
            if tts_buffer_final:
                _estimated_tts_done_at[0] = max(
                    _estimated_tts_done_at[0],
                    time.time() + max(len(tts_buffer_final) / 4.0 + 2.0, 3.0),
                )
                enqueue_subtitle_segment(tts_buffer_final, meta=subtitle_meta)
                if turn_metrics is not None:
                    turn_metrics["tts_audio_start_at"] = time.time()
                seg_task_id = realtime_chat.say_streaming(
                    tts_buffer_final, is_first=True, is_last=False, task_id=None,
                    speaker=_current_speaker,
                )
                _streaming_task_id[0] = seg_task_id
                with _segments_lock:
                    _remaining_segments.add(seg_task_id)
                _pending_segment_task_ids.append(seg_task_id)
                # 立刻补一个 end=true 空包标记结束
                realtime_chat.say_streaming(
                    "", is_first=False, is_last=True, task_id=seg_task_id,
                    speaker=_current_speaker,
                )
        else:
            # 流已经开过 → 把残余文本作为最后一包带过去（协议允许末尾包 content 非空）
            if tts_buffer_final:
                _estimated_tts_done_at[0] = max(
                    _estimated_tts_done_at[0],
                    time.time() + max(len(tts_buffer_final) / 4.0 + 2.0, 3.0),
                )
                enqueue_subtitle_segment(tts_buffer_final, meta=subtitle_meta)
            realtime_chat.say_streaming(
                tts_buffer_final, is_first=False, is_last=True,
                task_id=_streaming_task_id[0],
                speaker=_current_speaker,
            )
    tts_buffer = ""

    # 解析 tool_call 结果
    if _tc_chunks and tool_result_holder is not None:
        for idx in sorted(_tc_chunks.keys()):
            tc = _tc_chunks[idx]
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = _rescue_tool_call_json(tc["arguments"])
            tool_result_holder.append({"name": tc["name"], "arguments": args})
            _log_fn(f"{C_FAST}[快脑·tool_call] {tc['name']}({args}){C_RESET}")

    # tool_call 解析完毕后立即通知调用方（不等 TTS 播完，游戏操作和语音并行）
    if on_tool_calls_parsed and tool_result_holder:
        on_tool_calls_parsed(tool_result_holder)

    # 在这里（LLM 文本已生成完、但 TTS 还没开始/正在阻塞播放）就 publish "reply"，让
    # emotion_sidecar 等订阅方在**播音时**就能切表情，而不是等播完。文本架构下 speak() 会
    # 阻塞到播完才返回，所以不能等调用方在返回后再发 reply（那样表情总慢一拍）。
    # 事件总线契约不变，只是发得早。被取消的输出不发，避免污染。
    if _event_bus is not None and full_reply.strip():
        _reply_is_current = True
        if output_id and is_output_current_fn is not None:
            try:
                _reply_is_current = bool(is_output_current_fn(output_id))
            except Exception:
                _reply_is_current = True
        if _reply_is_current:
            _event_bus.publish("reply", {
                "text": full_reply,
                "speaker": _current_speaker,
                "output_id": output_id,
            }, source="fast_brain")

    # 文本架构：独立 TTS 收尾——finish() 阻塞到 DashScope 合成完 + 客户端播完；被打断则 abort。
    # 借端到端那套分段等待（下方）因 _pending_segment_task_ids 为空，文本架构下天然跳过。
    if _use_indep_tts:
        if _indep_emitter is not None:
            if tts_state.interrupted.is_set():
                _indep_emitter.abort()
            else:
                _indep_emitter.finish()
        if turn_metrics is not None:
            turn_metrics["tts_done_source"] = "independent_tts"

    # LLM 流结束。如果一段都没发出去（没文本 / 全被打断 / 全是 tool_call），_segments_done_event 立刻 set
    with _segments_lock:
        _llm_done[0] = True
        if not _remaining_segments:
            _segments_done_event.set()

    # 等所有段播完（每段由 realtime_chat 收到 359 后 publish tts_segment_done）。
    # 等待要同时响应两件事——所有段都完成，或者用户开口打断——后者以前没接，导致 speak
    # 即使被打断也要熬到 30s 上限才退，期间 ASR 链路被堵在原地等不到放手机会。
    if _pending_segment_task_ids:
        _tts_wait_started_at = time.time()
        outcome = _wait_segments_or_interrupt(
            _segments_done_event, tts_state.interrupted, timeout=30.0,
            estimated_done_at_fn=lambda: _estimated_tts_done_at[0] if _llm_done[0] else 0.0,
        )
        if turn_metrics is not None:
            turn_metrics["tts_wait_ms"] = round((time.time() - _tts_wait_started_at) * 1000, 1)
            turn_metrics["tts_done_source"] = outcome
            if outcome == "timeout":
                turn_metrics["blocked_ms"] = round(30.0 * 1000, 1)
        if outcome in ("interrupted", "estimated_done", "timeout"):
            with _segments_lock:
                remaining_ids = list(_remaining_segments)
                if outcome == "timeout":
                    _log_fn(
                        f"{C_ERR}[lumi_tts·speak] 等 ChatTTSText 完成事件超时 30s，清理剩余 "
                        f"{len(_remaining_segments)}/{len(_pending_segment_task_ids)} 段{C_RESET}"
                    )
                elif outcome == "estimated_done" and _remaining_segments:
                    _log_fn(
                        f"{C_STATUS}[lumi_tts·speak] ChatTTSText 完成事件缺失，按本地播放估算收敛，清理剩余 "
                        f"{len(_remaining_segments)}/{len(_pending_segment_task_ids)} 段{C_RESET}"
                    )
                for tid in remaining_ids:
                    try:
                        realtime_chat.discard_task(tid)
                    except Exception:
                        pass
                _remaining_segments.clear()
                _segments_done_event.set()
    elif turn_metrics is not None and not _use_indep_tts:
        turn_metrics["tts_done_source"] = "no_audio"

    # 359 (TTSEnded) 只表示服务端合成结束，客户端音频缓冲区里还有数据在播
    # （官方文档 2026-02-26 起明确区分"模型合成完毕"与"客户端实际播报进度"）。
    # 这里再等 realtime_chat 客户端真正播完，否则上层 arbiter 提前 mark_done，
    # 下一个 speaker 立刻进入 → 双角色尾音叠声（典型现象：泰拉瑞亚环节）。
    if _pending_segment_task_ids and not tts_state.interrupted.is_set():
        _playback_deadline_at = max(
            realtime_chat.get_last_speech_done_at(),
            _estimated_tts_done_at[0],
        ) + 2.0
        while not tts_state.interrupted.is_set():
            if not realtime_chat.is_speaking():
                break
            if time.time() >= _playback_deadline_at:
                _log_fn(
                    f"{C_STATUS}[lumi_tts·speak] 等客户端音频播完超时，按 deadline 收敛 "
                    f"(deadline={_playback_deadline_at:.2f} now={time.time():.2f}){C_RESET}"
                )
                break
            time.sleep(0.05)

    if turn_metrics is not None:
        turn_metrics["tts_audio_done_at"] = time.time()

    _output_still_current = True
    if output_id and is_output_current_fn is not None:
        try:
            _output_still_current = bool(is_output_current_fn(output_id))
        except Exception:
            _output_still_current = True

    if _event_bus is not None:
        try:
            _event_bus.unsubscribe("tts_segment_done", _on_segment_done)
        except Exception:
            pass

        # publish 逻辑 tts_done：source="speak_legacy"，task_id 是 speak 这次调用的逻辑 id
        # director 用 wait_for_task(task_id) 来等"这次 speak 触发的整段回复结束"，不会被
        # 端到端聊天 / 弹幕注入触发的 tts_done 事件串扰（spec 第 5.4 节）
        if _output_still_current:
            _event_bus.publish("tts_done", {
                "task_id": _logical_task_id,
                "source": "speak_legacy",
                "output_id": output_id,
            }, source="lumi_tts")

    # 字幕：每段已经在 say() 之前 enqueue 给光标 worker，正常结束时 worker 会按字推完
    # 最后一段、停在末尾。下面的清屏定时器会在 2 秒后清掉。
    # 中止时主动清掉队列里还没推的段，并立即清屏——避免"声音断了字幕还在继续刷"。
    if tts_state.interrupted.is_set():
        abort_subtitle_segments()

    # 回复结束 → 恢复空闲状态
    tts_state.is_speaking = False
    if _current_speaker:
        tts_state.speaking_speakers.discard(_current_speaker)
    _trigger_expression_fn("平静", _current_speaker)
    _subtitle_clear_ver += 1
    _ver_snapshot = _subtitle_clear_ver

    def _clear_subtitle():
        if _subtitle_clear_ver != _ver_snapshot:
            return  # 已经在说新的话了，不清除
        subtitle_queue.put(None)
        if ENABLE_SUBTITLE_OBS:
            _subtitle_ws_broadcast(None)

    threading.Timer(2.0, _clear_subtitle).start()

    was_interrupted = tts_state.interrupted.is_set()
    if interrupt_thread is not None:
        tts_state.interrupted.set()
        interrupt_thread.join(timeout=1)

    total = time.time() - start
    if turn_metrics is not None:
        turn_metrics["fast_brain_total_ms"] = round(total * 1000, 1)
        turn_metrics["interrupted"] = was_interrupted
    if not first_token and was_interrupted:
        _log_fn(f"{C_FAST}[快脑·响应] 被打断 | {total:.2f}s{C_RESET}")

    tts_state.last_speak_done_time = time.time()
    _log_fn(
        f"[时序·speak返回] {time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d} "
        f"task={_logical_task_id} 耗时={total:.2f}s 打断={was_interrupted} "
        f"段数={len(_pending_segment_task_ids)} 剩余未完成={len(_remaining_segments)}"
    )
    return full_reply
