"""
Wordle 游戏桥接模块 - 作为 Lumi 子模块运行
本地 HTTP+WebSocket 服务提供 Wordle 网页，信息熵算法提供候选，快脑选词+解说

OBS 浏览器源加载 http://localhost:8770/wordle.html 即可显示游戏画面。

可独立运行（调试用）：python wordle_bridge.py
"""

import asyncio
import json
import random
import re
import sys
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aiohttp import web
import aiohttp

from wordle_engine import (
    OPTIMAL_FIRST_GUESS,
    PATTERN_ALL_GREEN,
    ConstraintTracker,
    decode_pattern,
    filter_candidates,
    get_best_guesses,
    get_pattern,
    load_words,
    pattern_to_emoji,
)

# ==================== 颜色 ====================

C_CYAN = "\033[96m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"

MAX_TURNS = 6
DECISION_TIMEOUT_S = 35.0

_COMMENTARY_BANNED_RE = re.compile(r"(算法|候选|备选|选项|列表|推荐|信息熵|entropy|candidate|枚举|首选|不对不对|等等|推理|排除)")


def _sanitize_commentary(text: str, fallback: str) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ").strip()
    text = _COMMENTARY_BANNED_RE.sub("", text)
    text = re.sub(r"(不对)+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ，,、。")
    parts = re.split(r"(?<=[。！？!?~])", text)
    picked = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if _COMMENTARY_BANNED_RE.search(part):
            continue
        picked.append(part)
        if len(picked) >= 2:
            break
    text = "".join(picked).strip()
    if not text:
        text = fallback
    if len(text) > 54:
        text = text[:54].rstrip("，,、 ") + "。"
    if text[-1] not in "。！？!?~":
        text += "。"
    return text

# 高质量开局词池（信息熵 Top 级，随机选择避免千篇一律）
GOOD_OPENERS = ["salet", "reast", "crate", "trace", "slate", "crane", "slant", "carte", "least", "tares"]

# ==================== 提示词 ====================

WORDLE_GAME_PROMPT = """
## Wordle 游戏规则
你正在直播玩 Wordle（猜英文单词）。每局猜一个5字母英文单词，最多6次机会。
- 绿色：字母正确且位置正确
- 黄色：字母存在但位置不对
- 灰色：字母不存在

你必须同时做两件事——
1. 用中文给观众说1句话（你的直播感受、直觉、吐槽、期待，什么都行，自然就好，每次说不一样的话）
2. 同时调用 guess_word 工具提交猜测
绝对不能只调工具不说话！观众听不到工具调用，只能听到你说的话。
保持简短自然，不要分析字母、不要解释策略。

【重要·别穿帮】你能选的词是系统帮你筛过的方向，但你要**当成自己的直觉**。
绝对不要对观众说出"选项 / 候选 / 备选 / 这几个词 / 列表 / 算法"这类词，
也别说"从里面挑 / 扫掉无关XX"——观众不该知道有人给你列了词，要说得像你自己脑子里想到的。
"""


# ==================== 数据结构 ====================

@dataclass
class GameDecisionRequest:
    """由 bridge 线程创建，投递给 Lumi 快脑，等待结果"""
    state_text: str                     # 当前局面描述（给操作者看，含已选/候选词）
    intel_text: str                     # 额外信息（可空）
    tools: list                         # Function Calling 工具定义
    spectator_text: str = ""            # 给旁观者看的局面（不剧透操作者要选的词）
    result_event: threading.Event = field(default_factory=threading.Event)
    result: dict = field(default_factory=dict)  # Lumi 快脑写入决策结果
    cancelled: bool = False
    output_id: str | None = None        # 本手操作者这次说话的 output_id，用于精确等它播完


def build_wordle_tools(valid_words: list[str]) -> list[dict]:
    """构建 guess_word 工具定义"""
    return [
        {
            "type": "function",
            "function": {
                "name": "guess_word",
                "description": "提交Wordle猜测。reasoning用中文简短解说。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "word": {
                            "type": "string",
                            "enum": valid_words,
                            "description": "要猜的单词",
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "用中文给观众解说1-2句，语气活泼可爱，不要过度分析。",
                            "maxLength": 200,
                        },
                    },
                    "required": ["word", "reasoning"],
                },
            },
        }
    ]


# ==================== WordleBridge ====================

class WordleBridge:
    EXPECTED_GLOBAL_STATE = "PLAYING_WORDLE"
    """Wordle 桥接器 — 可作为 Lumi 子线程或独立运行"""

    SERVE_PORT = 8786  # Wordle 游戏服务端口，OBS 浏览器源加载此地址

    def __init__(self, event_callback=None, headless: bool = False, bus=None):
        """
        event_callback: 可选回调函数 (event_type: str, data: dict) -> None
            event_type: "game_event" | "game_state" | "need_decision" | "decision_executed"
        bus: 可选事件总线实例，传入后优先用总线通信
        """
        self._bus = bus
        # 统一走总线通信，event_callback 仅作为无总线时的降级
        self.event_callback = self._publish_to_bus if bus else (event_callback or (lambda t, d: None))
        self.running = False
        self.headless = headless
        self._shutdown = False
        self._server_started = False
        self._pending_lock = threading.Lock()
        self.pending_decision: GameDecisionRequest | None = None
        # 词库
        self.answers, self.all_words = load_words()
        # WebSocket RPC
        self._ws_connected = threading.Event()
        self._rpc_id = 0
        self._rpc_pending: dict[int, tuple[dict, threading.Event]] = {}
        self._outbox: asyncio.Queue | None = None  # 发送队列（在 server loop 上创建）
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._server_thread: threading.Thread | None = None
        self.html_path = Path(__file__).parent / "wordle.html"
        self._current_answer: str | None = None
        self._activation_event = threading.Event()
        self._last_global_state = ""
        if self._bus:
            self._bus.subscribe("state_changed", self._on_state_changed)
        else:
            self._activation_event.set()
        # 统计
        self.games_played = 0
        self.games_won = 0
        self.total_turns = 0
        # 日志
        self.game_logs = []
        # 游戏状态（供 build_slot_prompt 使用）
        self.current_turn = 0
        # 当前是否在一局游戏中（供导演系统判断）
        self.in_round = False
        self.current_guesses = []
        self.game_active = False

    def _publish_to_bus(self, event_type: str, data: dict):
        """把桥接器事件转发到总线"""
        self._bus.publish(event_type, data, source="wordle")

    def _on_state_changed(self, event):
        new_state = str(event.data.get("new") or "")
        self._last_global_state = new_state
        # 过渡中且目标就是本游戏时也放行：切环节仪式要求"游戏先推首条状态证明就绪、
        # 再把状态正式翻到 PLAYING_WORDLE"，但开局推状态又被本门控挡在 PLAYING_WORDLE
        # 之前 → 死锁。允许"正在朝本游戏过渡"时开局，解开这个先后矛盾。
        meta = event.data.get("metadata") or {}
        transitioning_to_self = (
            new_state == "TRANSITIONING" and meta.get("to") == self.EXPECTED_GLOBAL_STATE
        )
        if new_state == self.EXPECTED_GLOBAL_STATE or transitioning_to_self:
            self._activation_event.set()
            self._log_event(f"[Wordle][状态门控] 已进入 {new_state}，允许开局/决策")
        else:
            self._activation_event.clear()

    def _wait_until_active(self) -> bool:
        if not self._bus:
            return True
        if self._activation_event.is_set():
            return True
        self._log_event(
            f"[Wordle][状态门控] 等待全局状态 {self.EXPECTED_GLOBAL_STATE}，当前={self._last_global_state or 'UNKNOWN'}"
        )
        while self.running and not self._shutdown:
            if self._activation_event.wait(timeout=0.5):
                return True
        return False

    # ─── HTTP + WebSocket 服务器 ───

    def _start_server(self):
        """在后台线程启动 aiohttp 服务器，提供 HTML 和 WebSocket"""
        if self._server_started:
            return
        self._server_started = True
        loop = asyncio.new_event_loop()
        self._server_loop = loop

        async def _run():
            self._outbox = asyncio.Queue()
            app = web.Application()
            app.router.add_get('/ws', self._ws_handler)
            app.router.add_get('/wordle.html', self._serve_html)
            app.router.add_get('/', self._serve_html)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, 'localhost', self.SERVE_PORT)
            await site.start()
            print(f"  {C_CYAN}[Wordle] 游戏服务已启动: http://127.0.0.1:{self.SERVE_PORT}/wordle.html{C_RESET}")
            while not self._shutdown:
                await asyncio.sleep(0.5)
            await runner.cleanup()

        def _thread():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_run())

        self._server_thread = threading.Thread(target=_thread, daemon=True, name="wordle-server")
        self._server_thread.start()

    async def _serve_html(self, request):
        return web.FileResponse(self.html_path)

    async def _ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_connected.set()
        print(f"  {C_CYAN}[Wordle] 浏览器已连接 (WebSocket){C_RESET}")

        # 发送协程：从 outbox 队列取消息发给浏览器（避免跨协程操作 ws）
        async def _sender_loop():
            while not ws.closed:
                try:
                    payload = await asyncio.wait_for(self._outbox.get(), timeout=1.0)
                    await ws.send_str(payload)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break

        sender_task = asyncio.ensure_future(_sender_loop())
        self._sync_overlay_state()

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    msg_id = data.get('id')
                    if msg_id is not None and msg_id in self._rpc_pending:
                        holder, event = self._rpc_pending.pop(msg_id)
                        holder['result'] = data.get('result')
                        event.set()
        except Exception:
            pass
        finally:
            sender_task.cancel()
            self._ws_connected.clear()
        return ws

    def _rpc_call(self, method: str, *args, timeout: float = 30.0):
        """同步 RPC 调用：发送指令到浏览器，等待返回结果"""
        self._rpc_id += 1
        msg_id = self._rpc_id
        holder: dict = {}
        done_event = threading.Event()
        self._rpc_pending[msg_id] = (holder, done_event)

        payload = json.dumps({'id': msg_id, 'method': method, 'args': list(args)})
        self._server_loop.call_soon_threadsafe(self._outbox.put_nowait, payload)

        if not done_event.wait(timeout=timeout):
            self._rpc_pending.pop(msg_id, None)
            raise TimeoutError(f"RPC '{method}' timed out after {timeout}s")
        return holder.get('result')

    def _rpc_fire(self, method: str, *args):
        """发送指令但不等待回复"""
        self._rpc_id += 1
        msg_id = self._rpc_id
        payload = json.dumps({'id': msg_id, 'method': method, 'args': list(args)})
        self._server_loop.call_soon_threadsafe(self._outbox.put_nowait, payload)

    def _hide_overlay(self):
        if not self._ws_connected.is_set():
            return
        try:
            self._rpc_fire('eval', "if (typeof resetBoard === 'function') resetBoard(); if (typeof setOverlayVisible === 'function') setOverlayVisible(false);")
        except Exception:
            pass

    def _sync_overlay_state(self):
        if not self._ws_connected.is_set():
            return
        try:
            self._rpc_fire('setWordLists', self.answers, self.all_words)
            if self.game_active and self._current_answer:
                self._rpc_fire('newGameWithAnswer', self._current_answer)
                for guess_info in self.current_guesses:
                    guess = (guess_info.get('guess') or '').lower()
                    if guess:
                        self._rpc_fire('submitGuess', guess)
            else:
                self._hide_overlay()
            self._wordlist_injected = True
        except Exception:
            pass

    @staticmethod
    def _log_event(msg: str):
        try:
            from lumi import log_event
            log_event(msg)
        except (ImportError, AttributeError):
            pass

    def _ensure_wordlist(self):
        """浏览器首次连接时注入词库"""
        if not self._wordlist_injected and self._ws_connected.is_set():
            self._rpc_fire('setWordLists', self.answers, self.all_words)
            self._wordlist_injected = True
            print(f"  {C_CYAN}[Wordle] 词库已注入 ({len(self.answers)} answers){C_RESET}")

    def _wait_tts_done(self, output_id=None, timeout: float = 12):
        """等"指定 output_id 的那次说话"播完再继续，避免画面先于语音、且不被别的角色/别次
        说话的 tts_done 串扰（原来被任意 tts_done 唤醒，双角色游戏里被围观者的 tts_done 提前
        放行，导致决策 2 秒一手失控）。output_id=None 时退化为被任意 tts_done 唤醒（端到端/
        无 id 路径的兜底，保证不卡死）。"""
        done = threading.Event()
        def on_tts_done(event):
            data = getattr(event, "data", None) or {}
            if output_id is None or data.get("output_id") == output_id:
                done.set()
        self._bus.subscribe("tts_done", on_tts_done)
        done.wait(timeout=timeout)
        self._bus.unsubscribe("tts_done", on_tts_done)

    def _interrupt_lumi_reply(self):
        if self._bus:
            self._bus.publish("interrupt_request", {}, source="wordle")

    def _new_game(self, answer: str | None = None) -> str:
        """开始新一局（引擎记录答案 + 浏览器显示）"""
        self.clear_pending_decision("new_game")
        if answer is None:
            answer = random.choice(self.answers)
        self._current_answer = answer
        self._ensure_wordlist()
        # 首局等待浏览器源加载（OBS 刷新后需要几秒加载 HTML + 连接 WS）
        if not self._ws_connected.is_set():
            self._ws_connected.wait(timeout=8)
        ws_ok = self._ws_connected.is_set()
        if ws_ok:
            self._rpc_fire('newGameWithAnswer', answer)
        print(f"  {C_CYAN}[Wordle] _new_game: answer={answer.upper()}, ws_connected={ws_ok}{C_RESET}")
        self._log_event(f"[Wordle] _new_game: ws_connected={ws_ok}")
        return answer

    def _submit_guess_visual(self, word: str):
        """在浏览器上显示猜测动画（fire-and-forget）"""
        if self._ws_connected.is_set():
            self._rpc_fire('submitGuess', word)

    def get_pending_decision(self) -> GameDecisionRequest | None:
        """供 Lumi 主线程检查是否有待处理的游戏决策"""
        with self._pending_lock:
            return self.pending_decision

    def clear_pending_decision(self, reason: str = "") -> None:
        with self._pending_lock:
            request = self.pending_decision
            self.pending_decision = None
        if request is not None:
            request.cancelled = True
            request.result_event.set()
            if reason:
                self._log_event(f"[Wordle] clear_pending_decision: {reason}")

    def _build_state_text(
        self, turn: int, tracker: ConstraintTracker,
        candidates_left: int, top_guesses: list[tuple[str, float]],
        is_opener: bool = False, is_direct_pick: bool = False,
        direct_candidates: list[str] | None = None,
        opener_word: str | None = None,
    ) -> str:
        """构建给 LLM 的局面描述。

        合法词由工具 enum 限定，这里**不在 prose 里摆候选单子**——否则模型会顺嘴跟观众
        说「从这些选项里挑」之类，穿帮。模型当成自己想词即可。
        """
        if is_opener:
            return (
                f"Turn 1/{MAX_TURNS} 开局！由你自己想一个 5 字母英文词开局，"
                f"调用 guess_word 选词，并用中文简短说说你的开局感受，1-2句话。"
            )
        if is_direct_pick:
            return (
                f"Turn {turn}/{MAX_TURNS}. 快猜出来啦，没剩几个可能了！\n"
                f"已知线索:\n{tracker.describe()}\n\n"
                f"调用 guess_word 选一个词猜，并用中文简短说说感受，1-2句话。"
            )
        # 正常轮：合法词在工具 enum 里，prose 不摆编号单子
        return (
            f"Turn {turn}/{MAX_TURNS}. 还有 {candidates_left} 个可能。\n\n"
            f"已知线索:\n{tracker.describe()}\n\n"
            f"根据线索调用 guess_word 选一个你最有感觉的词猜，并用中文简短说说感受，1-2句话。"
        )

    def _build_spectator_text(self, turn: int) -> str:
        """给旁观者看的局面——只描述已出现的结果，绝不剧透操作者本手要选的词。

        旁观者会「先于操作者」接一句，如果把含「你选择了 X」的决策文本丢给她，她会抢着
        把操作者的选词念出来（实测 Lumi 在 Nox 出招前就说『Nox 选了 CARTE』穿帮）。
        """
        if turn <= 1 or not self.current_guesses:
            return (
                "对局刚开始，棋盘还是空的，操作者马上要选开局词了。\n"
                "你是旁观者，先说一句期待或起哄的话——但**还不知道**操作者会选什么词，"
                "不要替他报词、不要编造具体单词。"
            )
        board = "\n".join(
            f"  第{g['turn']}手 {g['guess']} → {g['feedback']}"
            for g in self.current_guesses
        )
        left = self.current_guesses[-1].get("candidates_left", "?")
        return (
            f"目前已经猜过的词和反馈：\n{board}\n\n"
            f"还剩 {left} 个可能的词，轮到操作者挑下一手了。\n"
            f"你是旁观者，针对**已经出现**的结果聊两句即可——还不知道操作者下一手选什么，"
            f"不要替他报词、不要编造具体单词。"
        )

    def _request_decision(
        self, turn: int, valid_words: list[str], state_text: str,
    ) -> tuple[str, str]:
        """
        创建 GameDecisionRequest 并等待快脑返回。

        Returns: (word, reasoning)
        """
        tools = build_wordle_tools(valid_words)

        # TODO: decision_request 走总线需要快脑侧订阅，暂时始终用 pending_decision 旧路径
        request = GameDecisionRequest(
            state_text=state_text,
            intel_text="说1句话就好（保持简短），话题随你发散——吐槽、闲聊、联想、回应弹幕都行，不必只说这一手；别重复之前说过的话。必须调用guess_word工具。",
            tools=tools,
            spectator_text=self._build_spectator_text(turn),
        )

        with self._pending_lock:
            self.pending_decision = request

        self.event_callback("need_decision", {
            "state_text": state_text[:200],
            "turn": turn,
        })

        print(f"  {C_CYAN}[Wordle] Turn {turn}: 等待快脑决策... enum={valid_words}{C_RESET}")

        # 等待快脑返回（最长 DECISION_TIMEOUT_S 秒，每秒检查 running）
        _waited = 0.0
        while _waited < DECISION_TIMEOUT_S and not request.result_event.is_set() and self.running:
            request.result_event.wait(timeout=1.0)
            _waited += 1.0
        if not self.running:
            with self._pending_lock:
                self.pending_decision = None
            return valid_words[0], _sanitize_commentary("", f"游戏结束啦！")
        if request.cancelled:
            return "", ""
        if request.result_event.is_set():
            result = request.result
            word = result.get("word", "").lower().strip()
            fallback_reasoning = f"这手我想先试试 {word.upper()}，看看手感会不会顺起来！" if word else "这手先凭感觉试一下！"
            reasoning = _sanitize_commentary(result.get("reasoning", ""), fallback_reasoning)
            if word in valid_words:
                print(f"  {C_GREEN}[Wordle] 快脑选择: {word.upper()} — {reasoning[:60]}{C_RESET}")
                # 等"这手操作者这次解说"播完再提交到浏览器，避免画面先于语音、且不被别的
                # 角色/别次说话的 tts_done 串扰提前放行（output_id 由 conversation 写入 request）
                self._wait_tts_done(request.output_id, timeout=12)
                return word, reasoning
            else:
                msg = f"[Wordle] Turn {turn}: 快脑选了无效词 '{word}'，用算法首选"
                print(f"  {C_YELLOW}{msg}{C_RESET}")
                self._log_event(msg)
        else:
            msg = f"[Wordle] Turn {turn}: 快脑超时({DECISION_TIMEOUT_S}s)，用算法首选"
            print(f"  {C_RED}{msg}{C_RESET}")
            self._log_event(msg)

            self._interrupt_lumi_reply()
        with self._pending_lock:
            self.pending_decision = None

        # 兜底
        fallback = valid_words[0]
        return fallback, _sanitize_commentary("", f"这手我先试试 {fallback.upper()}，希望能开个好头！")

    def _play_one(self, answer: str | None = None) -> dict:
        """玩一局 Wordle"""
        answer = self._new_game(answer)
        self.game_active = True
        self.current_turn = 0
        self.current_guesses = []

        self.event_callback("game_event", {
            "text": "Wordle 新一局开始！",
            "event": "game_start",
        })
        self.in_round = True

        # 提前发一条「就绪」game_state：导演的就绪握手原本要等第一手出招才完成，而第一手
        # 搭载主播解说（过渡播报 TTS + LLM 延迟）常超过 10s 就绪超时 → 回滚。这里在棋盘
        # 搭好（_new_game 已等到浏览器连上）后立即发就绪，让握手 1-2s 完成；真实局面随
        # 第一手覆盖。turn=0 仅作就绪标记。
        self.event_callback("game_state", {
            "turn": 0, "guess": "", "feedback": "",
            "candidates_left": len(self.answers), "solved": False, "guesses": [],
        })

        print(f"\n  {C_CYAN}{'='*50}")
        print(f"  WORDLE — 新一局")
        print(f"  {'='*50}{C_RESET}\n")

        tracker = ConstraintTracker()
        candidates = list(self.answers)
        guesses_log = []

        for turn in range(1, MAX_TURNS + 1):
            if not self.running:
                print(f"  {C_YELLOW}[Wordle] 游戏被中断（running=False）{C_RESET}")
                self.in_round = False
                self.game_active = False
                return {"answer": answer, "guesses": guesses_log, "turns": turn - 1, "solved": False, "interrupted": True, "timestamp": datetime.now().isoformat()}
            self.current_turn = turn

            # 选词：所有轮都「算法筛候选 + 模型从候选里自己挑」，词只在模型调用工具后才产生
            # （这样旁观者先发言时词还不存在，从根上杜绝剧透；用户明确不追求胜率）。
            if turn == 1:
                # 开局：高熵开局词池当候选，模型自己挑（shuffle 让超时兜底也有变化）
                valid_words = list(GOOD_OPENERS)
                random.shuffle(valid_words)
                state_text = self._build_state_text(
                    turn, tracker, len(candidates), [(w, 0.0) for w in valid_words], is_opener=True)
                guess, reasoning = self._request_decision(turn, valid_words, state_text)
            elif len(candidates) <= 2:
                # 残局：剩下的 1-2 个候选交给模型挑
                valid_words = list(candidates)
                state_text = self._build_state_text(
                    turn, tracker, len(candidates), [(c, 0.0) for c in valid_words],
                    is_direct_pick=True, direct_candidates=valid_words,
                )
                guess, reasoning = self._request_decision(turn, valid_words, state_text)
            else:
                # 中间轮：算法按信息量排前 5，模型从中挑（原有逻辑）
                top = get_best_guesses(candidates, n=5)
                valid_words = [w for w, _ in top]
                state_text = self._build_state_text(turn, tracker, len(candidates), top)
                guess, reasoning = self._request_decision(turn, valid_words, state_text)

            # 清除 pending
            with self._pending_lock:
                self.pending_decision = None

            if not guess:
                msg = f"[Wordle] Turn {turn}: 决策已取消，跳过本轮提交"
                print(f"  {C_YELLOW}{msg}{C_RESET}")
                self._log_event(msg)
                if not self.running:
                    self.in_round = False
                    self.game_active = False
                    return {"answer": answer, "guesses": guesses_log, "turns": turn - 1, "solved": False, "interrupted": True, "timestamp": datetime.now().isoformat()}
                continue

            # 引擎计算 pattern + 浏览器显示动画
            pattern = get_pattern(guess, answer)
            self._submit_guess_visual(guess)
            tracker.update(guess, pattern)
            candidates = filter_candidates(candidates, guess, pattern)

            # 记录
            decoded = decode_pattern(pattern)
            feedback_str = " ".join(
                {"0": "灰", "1": "黄", "2": "绿"}[str(d)]
                for d in decoded
            )
            guess_info = {
                "turn": turn,
                "guess": guess.upper(),
                "pattern": decoded,
                "feedback": feedback_str,
                "reasoning": reasoning,
                "candidates_left": len(candidates),
            }
            guesses_log.append(guess_info)
            self.current_guesses.append(guess_info)

            # 推送事件
            self.event_callback("game_event", {
                "text": f"Turn {turn}: {guess.upper()} → {feedback_str} (剩{len(candidates)}词)",
                "event": "guess",
            })
            self.event_callback("game_state", {
                "turn": turn,
                "guess": guess.upper(),
                "feedback": feedback_str,
                "candidates_left": len(candidates),
                "solved": pattern == PATTERN_ALL_GREEN,
                "guesses": [g["guess"] for g in guesses_log],
            })

            solved = pattern == PATTERN_ALL_GREEN
            if solved:
                print(f"  {C_GREEN}{C_BOLD}  Solved in {turn} turn(s)!{C_RESET}")
                self.games_won += 1
                self.total_turns += turn
                self.in_round = False
                self._wait_tts_done(timeout=12)
                self.event_callback("game_event", {
                    "text": f"Wordle 胜利！{turn}步猜出 {answer.upper()}！",
                    "event": "victory",
                })
                break

            # 等一下让浏览器动画播放
            time.sleep(0.5)

        else:
            # 6次没猜中
            print(f"  {C_RED}  Failed! Answer was: {answer.upper()}{C_RESET}")
            self.total_turns += MAX_TURNS
            self.in_round = False
            self._wait_tts_done(timeout=12)
            self.event_callback("game_event", {
                "text": f"Wordle 失败了...答案是 {answer.upper()}",
                "event": "defeat",
            })

        self.games_played += 1
        self.game_active = False

        result = {
            "answer": answer,
            "guesses": guesses_log,
            "turns": turn if solved else MAX_TURNS,
            "solved": solved,
            "timestamp": datetime.now().isoformat(),
        }
        self.game_logs.append(result)
        return result

    def run(self):
        """主循环 — 在独立线程中运行，连续玩 Wordle"""
        print(f"{C_CYAN}{'='*50}")
        print(f"  Wordle Bridge 已启动")
        print(f"{'='*50}{C_RESET}")

        self._start_browser()
        self.running = True

        try:
            while self.running:
                if not self._wait_until_active():
                    break
                result = self._play_one()

                if self.running:
                    time.sleep(5)

        except KeyboardInterrupt:
            print(f"\n  {C_YELLOW}[Wordle] 用户中断{C_RESET}")
        except Exception as e:
            print(f"  {C_RED}[Wordle] 异常: {e}{C_RESET}")
        finally:
            self._stop_browser()
            self.running = False
            self.save_logs()

    def _start_browser(self):
        self._start_server()
        print(f"  {C_CYAN}[Wordle] OBS 浏览器源请加载: http://127.0.0.1:8770/wordle.html{C_RESET}")
        self._wordlist_injected = False

    def start_overlay_server(self):
        self._start_browser()

    def _stop_browser(self):
        self._hide_overlay()

    def stop(self):
        self.clear_pending_decision("stop")
        self._activation_event.clear()
        self.running = False
        self._hide_overlay()
        self.save_logs()

    def shutdown(self):
        self.clear_pending_decision("shutdown")
        self._activation_event.clear()
        self.running = False
        self._shutdown = True
        self._hide_overlay()

    def save_logs(self):
        """保存游戏日志"""
        if not self.game_logs:
            return
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = log_dir / f"wordle_lumi_{ts}.json"
        summary = {
            "games_played": self.games_played,
            "games_won": self.games_won,
            "avg_turns": round(self.total_turns / max(self.games_played, 1), 2),
            "games": self.game_logs,
        }
        log_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {C_CYAN}[Wordle] 日志已保存: {log_file}{C_RESET}")


# ==================== 独立运行入口（调试用）====================

def main():
    """独立运行：不连接 Lumi，快脑决策用算法首选替代"""
    bridge = WordleBridge()
    try:
        bridge.run()
    except KeyboardInterrupt:
        bridge.stop()
        print("\n  已退出")


if __name__ == "__main__":
    main()
