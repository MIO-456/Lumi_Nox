"""
汉兜（成语 Wordle）桥接模块 - 作为 Lumi 子模块运行
本地 HTTP+WebSocket 服务提供汉兜网页，四维信息熵算法提供候选，快脑选词+解说

OBS 浏览器源加载 http://localhost:8770/handle.html 即可显示游戏画面。

可独立运行（调试用）：python handle_bridge.py
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

import solver_client  # 求解丢独立子进程，避免重算占CPU把皮套动作挤卡（毛病三）
from handle_engine import (
    PATTERN_ALL_GREEN,
    MAX_TURNS,
    ConstraintTracker,
    ParsedIdiom,
    decode_pattern,
    filter_candidates,
    get_best_guesses,
    get_pattern,
    load_idioms,
    pattern_to_text,
    GOOD_OPENERS,
    get_idiom_by_word,
)

# ==================== 颜色 ====================

C_CYAN = "\033[96m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"
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

# ==================== 提示词 ====================

HANDLE_GAME_PROMPT = """
## 成语猜猜（汉兜）游戏规则
你正在直播玩成语猜猜。每局猜一个四字成语，最多6次机会。
每次猜测后会在三个维度上分别反馈：
- 汉字：这个字本身对不对、位置对不对（绿/黄/灰）
- 声母：拼音声母对不对（绿/黄/灰）
- 韵母：拼音韵母对不对（绿/黄/灰）

绿色=完全正确，黄色=存在但位置不对，灰色=不存在。
三个维度是独立判定的，所以即使字猜错了，声母韵母可能是对的！

你必须同时做两件事——
1. 用中文给观众说1句话（你的直播感受、直觉、吐槽、期待，什么都行，自然就好，每次说不一样的话）
2. 同时调用 guess_idiom 工具提交猜测
绝对不能只调工具不说话！观众听不到工具调用，只能听到你说的话。
保持简短自然，不要逐字分析拼音、不要解释策略。

【重要·别穿帮】给你的参考成语是系统帮你筛过的方向，但你要**当成自己想到的**。
绝对不要对观众说出"选项 / 候选 / 备选 / 这几个成语 / 列表 / 算法"这类词，
观众不该知道有人给你列了词，要说得像你自己脑子里蹦出来的。
"""


# ==================== 数据结构 ====================

@dataclass
class GameDecisionRequest:
    """由 bridge 线程创建，投递给 Lumi 快脑，等待结果"""
    state_text: str
    intel_text: str
    tools: list
    spectator_text: str = ""            # 给旁观者看的局面（不剧透操作者要选的成语）
    result_event: threading.Event = field(default_factory=threading.Event)
    result: dict = field(default_factory=dict)
    cancelled: bool = False
    output_id: str | None = None        # 本手操作者这次说话的 output_id，用于精确等它播完


def build_handle_tools() -> list[dict]:
    """构建 guess_idiom 工具定义 — 不设 enum，LLM 自由选成语"""
    return [
        {
            "type": "function",
            "function": {
                "name": "guess_idiom",
                "description": "提交成语猜测。你可以自由选择任何四字成语。reasoning用中文简短解说。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "idiom": {
                            "type": "string",
                            "description": "要猜的四字成语（必须是真实存在的四字成语）",
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "用中文给观众说1句感受，自然活泼。",
                            "maxLength": 200,
                        },
                    },
                    "required": ["idiom", "reasoning"],
                },
            },
        }
    ]


# ==================== HandleBridge ====================

class HandleBridge:
    EXPECTED_GLOBAL_STATE = "PLAYING_HANDLE"
    """汉兜桥接器 — 可作为 Lumi 子线程或独立运行"""

    SERVE_PORT = 8787  # 汉兜游戏服务端口，OBS 浏览器源加载此地址

    def __init__(self, event_callback=None, headless: bool = False, bus=None):
        self._bus = bus
        self.event_callback = self._publish_to_bus if bus else (event_callback or (lambda t, d: None))
        self.running = False
        self.headless = headless
        self._shutdown = False
        self._server_started = False
        self._pending_lock = threading.Lock()
        self.pending_decision: GameDecisionRequest | None = None
        # 词库
        self.idioms: list[ParsedIdiom] = load_idioms()
        self._idiom_map: dict[str, ParsedIdiom] = {idm.word: idm for idm in self.idioms}
        # 过滤出开局词池中实际存在的成语
        self._openers = [w for w in GOOD_OPENERS if w in self._idiom_map]
        if not self._openers:
            self._openers = [self.idioms[0].word]
        # WebSocket RPC
        self._ws_connected = threading.Event()
        self._rpc_id = 0
        self._rpc_pending: dict[int, tuple[dict, threading.Event]] = {}
        self._outbox: asyncio.Queue | None = None
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._server_thread: threading.Thread | None = None
        self.html_path = Path(__file__).parent / "handle.html"
        self._current_answer: ParsedIdiom | None = None
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
        self.current_guesses = []
        self.game_active = False
        # 当前是否在一局游戏中（供导演系统判断）
        self.in_round = False

    # ─── HTTP + WebSocket 服务器 ───

    def _start_server(self):
        """在后台线程启动 aiohttp 服务器"""
        if self._server_started:
            return
        self._server_started = True
        loop = asyncio.new_event_loop()
        self._server_loop = loop

        async def _run():
            self._outbox = asyncio.Queue()
            app = web.Application()
            app.router.add_get('/ws', self._ws_handler)
            app.router.add_get('/handle.html', self._serve_html)
            app.router.add_get('/', self._serve_html)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, 'localhost', self.SERVE_PORT)
            await site.start()
            print(f"  {C_CYAN}[汉兜] 游戏服务已启动: http://127.0.0.1:{self.SERVE_PORT}/handle.html{C_RESET}")
            while not self._shutdown:
                await asyncio.sleep(0.5)
            await runner.cleanup()

        def _thread():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_run())

        self._server_thread = threading.Thread(target=_thread, daemon=True, name="handle-server")
        self._server_thread.start()

    async def _serve_html(self, request):
        return web.FileResponse(self.html_path)

    async def _ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_connected.set()
        print(f"  {C_CYAN}[汉兜] 浏览器已连接 (WebSocket){C_RESET}")

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

    def _publish_to_bus(self, event_type: str, data: dict):
        """把桥接器事件转发到总线"""
        self._bus.publish(event_type, data, source="handle")

    def _on_state_changed(self, event):
        new_state = str(event.data.get("new") or "")
        self._last_global_state = new_state
        # 过渡中且目标就是本游戏时也放行：切环节仪式要求"游戏先推首条状态证明就绪、
        # 再把状态正式翻到 PLAYING_HANDLE"，但开局推状态又被本门控挡在 PLAYING_HANDLE
        # 之前 → 死锁。允许"正在朝本游戏过渡"时开局，解开这个先后矛盾。
        meta = event.data.get("metadata") or {}
        transitioning_to_self = (
            new_state == "TRANSITIONING" and meta.get("to") == self.EXPECTED_GLOBAL_STATE
        )
        if new_state == self.EXPECTED_GLOBAL_STATE or transitioning_to_self:
            self._activation_event.set()
            self._log_event(f"[汉兜][状态门控] 已进入 {new_state}，允许开局/决策")
        else:
            self._activation_event.clear()

    def _wait_until_active(self) -> bool:
        if not self._bus:
            return True
        if self._activation_event.is_set():
            return True
        self._log_event(
            f"[汉兜][状态门控] 等待全局状态 {self.EXPECTED_GLOBAL_STATE}，当前={self._last_global_state or 'UNKNOWN'}"
        )
        while self.running and not self._shutdown:
            if self._activation_event.wait(timeout=0.5):
                return True
        return False

    @staticmethod
    def _log_event(msg: str):
        try:
            from lumi import log_event
            log_event(msg)
        except (ImportError, AttributeError):
            pass

    def _wait_tts_done(self, output_id=None, timeout: float = 12):
        """等"指定 output_id 的那次说话"播完再继续，避免画面先于语音、且不被别的角色/别次
        说话的 tts_done 串扰提前放行。output_id=None 时退化为被任意 tts_done 唤醒（兜底，
        保证无 id 路径不卡死）。"""
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
            self._bus.publish("interrupt_request", {}, source="handle")

    def _parsed_to_js(self, parsed: ParsedIdiom) -> list[dict]:
        """转换 ParsedIdiom 到 JS 需要的格式"""
        return [
            {"initial": c.initial, "final": c.final, "tone": c.tone}
            for c in parsed.chars
        ]

    def _new_game(self, answer_word: str | None = None) -> ParsedIdiom:
        """开始新一局（引擎记录答案 + 浏览器显示）"""
        self.clear_pending_decision("new_game")
        if answer_word is None:
            answer_parsed = random.choice(self.idioms)
        else:
            answer_parsed = self._idiom_map.get(answer_word)
            if not answer_parsed:
                answer_parsed = random.choice(self.idioms)

        self._current_answer = answer_parsed
        # 首局等待浏览器源加载（OBS 刷新后需要几秒加载 HTML + 连接 WS）
        if not self._ws_connected.is_set():
            self._ws_connected.wait(timeout=8)
        ws_ok = self._ws_connected.is_set()
        if ws_ok:
            self._rpc_fire('newGameWithAnswer', answer_parsed.word, self._parsed_to_js(answer_parsed))
        print(f"  {C_CYAN}[汉兜] _new_game: answer={answer_parsed.word}, ws_connected={ws_ok}{C_RESET}")
        self._log_event(f"[汉兜] _new_game: ws_connected={ws_ok}")
        return answer_parsed

    def _submit_guess_visual(self, guess_parsed: ParsedIdiom):
        """在浏览器上显示猜测动画（fire-and-forget）"""
        if self._ws_connected.is_set():
            self._rpc_fire('submitGuess', guess_parsed.word, self._parsed_to_js(guess_parsed))

    def get_pending_decision(self) -> GameDecisionRequest | None:
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
                self._log_event(f"[汉兜] clear_pending_decision: {reason}")

    def _build_state_text(
        self, turn: int, tracker: ConstraintTracker,
        candidates_left: int, top_guesses: list[tuple[str, float]],
    ) -> str:
        """构建给 LLM 的局面描述 — LLM 自由选词，算法推荐仅供参考"""
        if turn == 1:
            return (
                f"第 1/{MAX_TURNS} 轮，开局！\n"
                f"你可以自由选择任何四字成语作为开局词。\n"
                f"小提示：选字符多样、声母韵母覆盖广的成语会更有效哦~\n\n"
                f"选一个成语，给观众解说一下你的开局策略。"
            )
        if candidates_left <= 5:
            candidates_str = "、".join(w for w, _ in top_guesses)
            return (
                f"第 {turn}/{MAX_TURNS} 轮，快猜出来了，没剩几个可能！\n"
                f"已知线索:\n{tracker.describe()}\n\n"
                f"心里可以参考这些方向（别对观众提）：{candidates_str}\n"
                f"挑一个调用 guess_idiom 猜，说说你的感受。"
            )
        # 正常轮：给参考词但不限制
        hints_desc = "、".join(w for w, _ in top_guesses[:5])
        return (
            f"第 {turn}/{MAX_TURNS} 轮，还有 {candidates_left} 个可能。\n\n"
            f"已知线索:\n{tracker.describe()}\n\n"
            f"心里可以参考这些方向（别对观众提）：{hints_desc}\n"
            f"挑一个调用 guess_idiom 猜，也可以自己想，说说你的感受。"
        )

    def _build_spectator_text(self, turn: int) -> str:
        """给旁观者看的局面——只描述已出现的结果，绝不剧透操作者本手要选的成语。"""
        if turn <= 1 or not self.current_guesses:
            return (
                "对局刚开始，棋盘还是空的，操作者马上要选开局成语了。\n"
                "你是旁观者，先说一句期待或起哄的话——但**还不知道**操作者会选什么成语，"
                "不要替他报成语、不要编造具体成语。"
            )
        board = "\n".join(
            f"  第{g['turn']}手 {g['guess']} → {g['feedback']}"
            for g in self.current_guesses
        )
        left = self.current_guesses[-1].get("candidates_left", "?")
        return (
            f"目前已经猜过的成语和反馈：\n{board}\n\n"
            f"还剩 {left} 个可能的成语，轮到操作者挑下一手了。\n"
            f"你是旁观者，针对**已经出现**的结果聊两句即可——还不知道操作者下一手选什么，"
            f"不要替他报成语、不要编造具体成语。"
        )

    def _request_decision(
        self, turn: int, algo_top: list[tuple[str, float]],
        state_text: str,
    ) -> tuple[str, str]:
        """
        创建 GameDecisionRequest 并等待快脑返回。

        LLM 自由选词，不设 enum 限制。
        如果 LLM 选的词不在词库中或超时，用算法首选兜底。
        """
        tools = build_handle_tools()
        request = GameDecisionRequest(
            state_text=state_text,
            intel_text="说1句话就好(保持简短),话题随你发散——吐槽、闲聊、联想、回应弹幕都行,不必只说这一手;别重复之前说过的话。必须调用guess_idiom工具。",
            tools=tools,
            spectator_text=self._build_spectator_text(turn),
        )

        with self._pending_lock:
            self.pending_decision = request

        self.event_callback("need_decision", {
            "state_text": state_text[:200],
            "turn": turn,
        })

        algo_hint = [w for w, _ in algo_top[:3]] if algo_top else []
        print(f"  {C_CYAN}[汉兜] 第{turn}轮: 等待快脑自由选词... (算法推荐: {algo_hint}){C_RESET}")

        # 等待快脑返回（最长 DECISION_TIMEOUT_S 秒，每秒检查 running）
        _waited = 0.0
        while _waited < DECISION_TIMEOUT_S and not request.result_event.is_set() and self.running:
            request.result_event.wait(timeout=1.0)
            _waited += 1.0
        if not self.running:
            with self._pending_lock:
                self.pending_decision = None
            fallback = algo_top[0][0] if algo_top else self._openers[0]
            return fallback, _sanitize_commentary("", "游戏结束啦！")
        if request.cancelled:
            return "", ""
        if request.result_event.is_set():
            result = request.result
            idiom = result.get("idiom", "").strip()
            fallback_reasoning = f"这轮我对「{idiom}」有感觉，先拿它试试！" if idiom else "这轮我先凭感觉试一下！"
            reasoning = _sanitize_commentary(result.get("reasoning", ""), fallback_reasoning)

            if idiom in self._idiom_map:
                print(f"  {C_GREEN}[汉兜] 快脑选择: {idiom} — {reasoning[:60]}{C_RESET}")
                # 等"这手操作者这次解说"播完再提交到浏览器，避免画面先于语音、且不被别的
                # 角色/别次说话的 tts_done 串扰（output_id 由 conversation 写入 request）
                self._wait_tts_done(request.output_id, timeout=12)
                return idiom, reasoning
            else:
                msg = f"[汉兜] 第{turn}轮: 快脑选了词库外的 '{idiom}'，用算法兜底"
                print(f"  {C_YELLOW}{msg}{C_RESET}")
                self._log_event(msg)
        else:
            msg = f"[汉兜] 第{turn}轮: 快脑超时({DECISION_TIMEOUT_S}s)，用算法兜底"
            print(f"  {C_RED}{msg}{C_RESET}")
            self._log_event(msg)

            self._interrupt_lumi_reply()
        with self._pending_lock:
            self.pending_decision = None

        fallback = algo_top[0][0] if algo_top else self._openers[0]
        return fallback, _sanitize_commentary("", f"这轮我先猜「{fallback}」，看看感觉对不对！")

    def _play_one(self, answer_word: str | None = None) -> dict:
        """玩一局汉兜"""
        answer_parsed = self._new_game(answer_word)
        self.game_active = True
        self.current_turn = 0
        self.current_guesses = []

        self.event_callback("game_event", {
            "text": "成语猜猜新一局开始！",
            "event": "game_start",
        })
        self.in_round = True

        # 提前发一条「就绪」game_state：导演的就绪握手原本要等第一手出招才完成，而第一手
        # 搭载主播解说常超过 10s 就绪超时 → 回滚。这里在棋盘搭好后立即发就绪，真实局面随
        # 第一手覆盖。turn=0 仅作就绪标记。
        self.event_callback("game_state", {
            "turn": 0, "guess": "", "feedback": "",
            "candidates_left": len(self.idioms), "solved": False, "guesses": [],
        })

        print(f"\n  {C_CYAN}{'='*50}")
        print(f"  成语猜猜 — 新一局")
        print(f"  {'='*50}{C_RESET}\n")

        tracker = ConstraintTracker()
        candidates = list(self.idioms)
        guesses_log = []
        solved = False

        for turn in range(1, MAX_TURNS + 1):
            if not self.running:
                print(f"  {C_YELLOW}[汉兜] 游戏被中断（running=False）{C_RESET}")
                self.in_round = False
                self.game_active = False
                return {"answer": answer_parsed.word, "guesses": guesses_log, "turns": turn - 1, "solved": False, "interrupted": True, "timestamp": datetime.now().isoformat()}
            self.current_turn = turn

            # 计算算法推荐（供参考 + 兜底）
            if turn == 1:
                # 首轮：算法推荐开局词池，但 LLM 自由选
                algo_top = [(w, 0.0) for w in self._openers[:5]]
            else:
                algo_top = solver_client.best_guesses_or_fallback("handle", candidates, 5, get_best_guesses)

            state_text = self._build_state_text(
                turn, tracker, len(candidates), algo_top,
            )
            guess_word, reasoning = self._request_decision(
                turn, algo_top, state_text,
            )

            # 清除 pending
            with self._pending_lock:
                self.pending_decision = None

            if not guess_word:
                msg = f"[汉兜] 第{turn}轮: 决策已取消，跳过本轮提交"
                print(f"  {C_YELLOW}{msg}{C_RESET}")
                self._log_event(msg)
                if not self.running:
                    self.in_round = False
                    self.game_active = False
                    return {
                        "answer": answer_parsed.word,
                        "guesses": guesses_log,
                        "turns": turn - 1,
                        "solved": False,
                        "interrupted": True,
                        "timestamp": datetime.now().isoformat(),
                    }
                continue

            guess_parsed = self._idiom_map.get(guess_word)
            if not guess_parsed:
                print(f"  {C_RED}[汉兜] 词库中找不到 '{guess_word}'，跳过{C_RESET}")
                continue

            # 用引擎计算 pattern（用于过滤）
            pattern = get_pattern(guess_parsed, answer_parsed)
            tracker.update(guess_parsed, pattern)
            candidates = filter_candidates(candidates, guess_parsed, pattern)

            # 浏览器显示动画（fire-and-forget）
            self._submit_guess_visual(guess_parsed)

            # 记录
            decoded = decode_pattern(pattern)
            feedback_str = pattern_to_text(pattern, guess_parsed)
            guess_info = {
                "turn": turn,
                "guess": guess_word,
                "feedback": feedback_str,
                "reasoning": reasoning,
                "candidates_left": len(candidates),
            }
            guesses_log.append(guess_info)
            self.current_guesses.append(guess_info)

            # 推送事件
            self.event_callback("game_event", {
                "text": f"第{turn}轮: {guess_word} → {feedback_str} (剩{len(candidates)}词)",
                "event": "guess",
            })
            self.event_callback("game_state", {
                "turn": turn,
                "guess": guess_word,
                "feedback": feedback_str,
                "candidates_left": len(candidates),
                "solved": pattern == PATTERN_ALL_GREEN,
                "guesses": [g["guess"] for g in guesses_log],
            })

            solved = pattern == PATTERN_ALL_GREEN
            if solved:
                print(f"  {C_GREEN}{C_BOLD}  {turn}轮猜出！答案: {answer_parsed.word}{C_RESET}")
                self.games_won += 1
                self.total_turns += turn
                self.in_round = False
                self._wait_tts_done(timeout=12)
                self.event_callback("game_event", {
                    "text": f"成语猜猜胜利！{turn}轮猜出「{answer_parsed.word}」！",
                    "event": "victory",
                })
                break

            time.sleep(0.5)

        else:
            print(f"  {C_RED}  失败！答案: {answer_parsed.word}{C_RESET}")
            self.total_turns += MAX_TURNS
            self.in_round = False
            self._wait_tts_done(timeout=12)
            self.event_callback("game_event", {
                "text": f"成语猜猜失败了...答案是「{answer_parsed.word}」",
                "event": "defeat",
            })

        self.games_played += 1
        self.game_active = False

        result = {
            "answer": answer_parsed.word,
            "guesses": guesses_log,
            "turns": turn if solved else MAX_TURNS,
            "solved": solved,
            "timestamp": datetime.now().isoformat(),
        }
        self.game_logs.append(result)
        return result

    def run(self):
        """主循环 — 在独立线程中运行，连续玩"""
        print(f"{C_CYAN}{'='*50}")
        print(f"  汉兜 Bridge 已启动")
        print(f"{'='*50}{C_RESET}")

        self._start_browser()
        self.running = True

        try:
            while self.running:
                if not self._wait_until_active():
                    break
                self._play_one()

                if self.running:
                    time.sleep(5)

        except KeyboardInterrupt:
            print(f"\n  {C_YELLOW}[汉兜] 用户中断{C_RESET}")
        except Exception as e:
            print(f"  {C_RED}[汉兜] 异常: {e}{C_RESET}")
            import traceback
            traceback.print_exc()
        finally:
            self._stop_browser()
            self.running = False
            self.save_logs()

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
            if self.game_active and self._current_answer:
                print(f"  {C_CYAN}[汉兜] 同步浏览器状态: 答案={self._current_answer.word}, 已猜{len(self.current_guesses)}轮{C_RESET}")
                self._rpc_fire('newGameWithAnswer', self._current_answer.word, self._parsed_to_js(self._current_answer))
                for guess_info in self.current_guesses:
                    guess_word = guess_info.get('guess')
                    guess_parsed = self._idiom_map.get(guess_word)
                    if guess_parsed:
                        self._rpc_fire('submitGuess', guess_parsed.word, self._parsed_to_js(guess_parsed))
            else:
                self._hide_overlay()
        except Exception:
            pass

    def _start_browser(self):
        self._start_server()
        print(f"  {C_CYAN}[汉兜] OBS 浏览器源请加载: http://127.0.0.1:8770/handle.html{C_RESET}")
        print(f"  {C_CYAN}[汉兜] 词库 {len(self.idioms)} 条成语{C_RESET}")

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
        logs_dir = Path(__file__).parent / "logs"
        logs_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filepath = logs_dir / f"handle_{ts}.json"
        filepath.write_text(
            json.dumps(self.game_logs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  {C_CYAN}[汉兜] 日志已保存: {filepath}{C_RESET}")


# ==================== 独立运行 ====================

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")

    print("=== 汉兜 Bridge 独立测试 ===\n")

    bridge = HandleBridge(headless=False)
    bridge._start_browser()

    try:
        result = bridge._play_one()
        print(f"\n结果: {json.dumps(result, ensure_ascii=False, indent=2)}")
    finally:
        bridge._stop_browser()
        bridge.save_logs()
