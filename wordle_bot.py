"""
Wordle Bot — 混合决策引擎（信息熵算法 + LLM 选词解说）

架构：
  算法层（wordle_engine.py）：约束追踪 + 信息熵 Top-N 候选
  LLM 层：从 Top-N 中选词 + 生成推理解说
  交互层：Playwright 控制浏览器 / 本地终端模式

运行方式：
  # 终端模式（自带 Wordle，不需要浏览器）
  python wordle_bot.py

  # 终端模式，指定答案
  python wordle_bot.py --answer crane

  # 批量测试（纯算法，不调用 LLM）
  python wordle_bot.py --batch 100

  # 浏览器模式（Playwright 控制网页 Wordle）
  python wordle_bot.py --browser

  # 纯算法模式（不调用 LLM）
  python wordle_bot.py --no-llm
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

# Windows GBK 终端兼容：强制 UTF-8 输出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import solver_client  # 求解丢独立子进程，避免重算占CPU把皮套动作挤卡（毛病三）
from wordle_engine import (
    OPTIMAL_FIRST_GUESS,
    PATTERN_ALL_GREEN,
    ConstraintTracker,
    compute_entropy,
    decode_pattern,
    filter_candidates,
    get_best_guesses,
    get_pattern,
    load_words,
    pattern_to_emoji,
)

# ─── 颜色 ───

C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_GRAY = "\033[90m"
C_CYAN = "\033[96m"
C_RED = "\033[91m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"

MAX_TURNS = 6


def colorize_guess(guess: str, pattern: int) -> str:
    """给猜测词着色显示"""
    decoded = decode_pattern(pattern)
    color_map = {0: C_GRAY, 1: C_YELLOW, 2: C_GREEN}
    return "".join(f"{color_map[d]}{C_BOLD}{c.upper()}{C_RESET}" for c, d in zip(guess, decoded))


# ─── LLM 决策层 ───

WORDLE_SYSTEM_PROMPT = """你是Lumi，一个正在直播玩Wordle的AI主播。你需要在6次内猜出一个5字母英文单词。

每轮你会收到：
- 当前已知线索（绿色/黄色/灰色字母）
- 按信息熵排序的候选词（熵越高，能排除越多可能）

你必须调用guess_word函数提交猜测，从给出的候选词中选一个。
用中文给观众解说你的思路，1-2句话，语气活泼可爱。"""

WORDLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "guess_word",
            "description": "Submit a Wordle guess",
            "parameters": {
                "type": "object",
                "properties": {
                    "word": {
                        "type": "string",
                        "description": "The 5-letter word to guess (lowercase)",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Brief reasoning for this guess (for stream commentary)",
                    },
                },
                "required": ["word", "reasoning"],
            },
        },
    }
]


LLM_MODELS = {
    "doubao": {
        "name": "Doubao 2.0 Mini",
        "model": "doubao-seed-2-0-mini-260215",
        "api_key_env": "ARK_API_KEY",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "extra_body": {"reasoning_effort": "low"},
        "tool_choice": "required",
    },
    "qwen": {
        "name": "Qwen 3.5 Flash",
        "model": "qwen3.5-flash",
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "extra_body": {"enable_thinking": False},  # 关闭思考，稳定速度
        "tool_choice": {"type": "function", "function": {"name": "guess_word"}},  # 强制调用指定工具
    },
}


class LLMPlayer:
    """LLM 决策层：从算法候选中选词 + 生成解说"""

    def __init__(self, model_key: str = "doubao"):
        from dotenv import load_dotenv
        from openai import OpenAI

        load_dotenv()
        cfg = LLM_MODELS[model_key]
        self.client = OpenAI(
            api_key=os.getenv(cfg["api_key_env"]),
            base_url=cfg["base_url"],
        )
        self.model = cfg["model"]
        self.model_name = cfg["name"]
        self.extra_body = cfg["extra_body"]
        self.tool_choice = cfg["tool_choice"]
        self.last_llm_log: dict | None = None
        print(f"  LLM: {self.model_name} ({self.model})")

    def choose(
        self,
        turn: int,
        tracker: ConstraintTracker,
        candidates_left: int,
        top_guesses: list[tuple[str, float]],
    ) -> tuple[str, str]:
        """
        让 LLM 从候选中选词。

        Returns: (word, reasoning)
        """
        self.last_llm_log = None

        # 构建候选列表描述
        candidates_desc = "\n".join(
            f"  {i+1}. {w.upper()} (entropy: {e:.2f})" for i, (w, e) in enumerate(top_guesses)
        )

        user_msg = (
            f"Turn {turn}/{MAX_TURNS}. {candidates_left} possible words remain.\n\n"
            f"Current knowledge:\n{tracker.describe()}\n\n"
            f"Top candidates (by information entropy):\n{candidates_desc}\n\n"
            f"Pick one word from the candidates above."
        )

        messages = [
            {"role": "system", "content": WORDLE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        # 限制 LLM 只能选候选列表中的词
        valid_words = [w for w, _ in top_guesses]
        tools = _build_tools_with_enum(valid_words)

        # 打印请求信息
        print(
            f"\n  {C_CYAN}┌─ LLM 请求 {'─'*38}\n"
            f"  │ model: {self.model}\n"
            f"  │ enum: {valid_words}\n"
            f"  │ user_msg:\n"
        )
        for line in user_msg.split("\n"):
            print(f"  │   {line}")
        print(f"  └{'─'*50}{C_RESET}")

        LLM_TIMEOUT = 10  # 秒

        try:
            t0 = time.time()
            kwargs = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "stream": True,
            }
            if self.tool_choice:
                kwargs["tool_choice"] = self.tool_choice
            if self.extra_body:
                kwargs["extra_body"] = self.extra_body

            # 流式调用，边收边检查超时
            stream = self.client.chat.completions.create(**kwargs)
            thinking = ""           # 思考过程
            content = ""            # 普通文本回复
            tool_call_args = ""     # 工具调用参数 JSON
            tool_call_name = ""     # 工具名
            finish_reason = None
            timed_out = False

            for chunk in stream:
                # 超时检查
                if time.time() - t0 > LLM_TIMEOUT:
                    timed_out = True
                    stream.close()
                    break

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason or finish_reason

                # 思考过程（豆包 reasoning_content）
                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    thinking += delta.reasoning_content

                # 普通文本
                if delta.content:
                    content += delta.content

                # 工具调用
                if delta.tool_calls:
                    tc = delta.tool_calls[0]
                    if tc.function.name:
                        tool_call_name = tc.function.name
                    if tc.function.arguments:
                        tool_call_args += tc.function.arguments

            elapsed = time.time() - t0

            # 超时处理：保存已收到的思考过程，回退算法
            if timed_out:
                thinking_preview = thinking[:150].replace("\n", " ") if thinking else "(无)"
                print(
                    f"\n  {C_RED}┌─ LLM 超时 ({elapsed:.1f}s > {LLM_TIMEOUT}s) {'─'*25}\n"
                    f"  │ 已收到思考: \"{thinking_preview}\"\n"
                    f"  │ 已收到参数: \"{tool_call_args[:100]}\"\n"
                    f"  │ 回退到算法首选\n"
                    f"  └{'─'*50}{C_RESET}\n"
                )
                self.last_llm_log = {
                    "model": self.model, "elapsed": round(elapsed, 2),
                    "user_msg": user_msg, "status": "timeout",
                }
                if thinking:
                    self.last_llm_log["thinking"] = thinking[:2000]
                if tool_call_args:
                    self.last_llm_log["partial_args"] = tool_call_args[:500]
                fallback = top_guesses[0][0]
                self.last_llm_log["fallback"] = fallback
                return fallback, "(timeout fallback)"

            # 正常完成：解析工具调用
            if tool_call_args:
                try:
                    args = json.loads(tool_call_args)
                except json.JSONDecodeError:
                    args = _extract_word_from_raw(tool_call_args, valid_words)
                word = args.get("word", "").lower().strip()
                reasoning = args.get("reasoning", "")

                # 打印响应
                thinking_line = ""
                if thinking:
                    thinking_preview = thinking[:150].replace("\n", " ")
                    thinking_line = f"  │ thinking: \"{thinking_preview}...\"\n"
                print(
                    f"\n  {C_CYAN}┌─ LLM 响应 ({elapsed:.1f}s) {'─'*33}\n"
                    f"{thinking_line}"
                    f"  │ tool_call: guess_word({json.dumps(args, ensure_ascii=False)[:200]})\n"
                    f"  │ finish_reason: {finish_reason}\n"
                    f"  │ word: {word.upper()}, reasoning: \"{reasoning[:100]}\"\n"
                    f"  └{'─'*50}{C_RESET}\n"
                )

                # 记录完整日志
                self.last_llm_log = {
                    "model": self.model,
                    "elapsed": round(elapsed, 2),
                    "user_msg": user_msg,
                    "enum": valid_words,
                    "tool_call": f"guess_word({json.dumps(args, ensure_ascii=False)})",
                    "raw_arguments": tool_call_args[:500],
                    "finish_reason": finish_reason,
                    "word": word,
                    "reasoning": reasoning,
                    "status": "ok",
                }
                if thinking:
                    self.last_llm_log["thinking"] = thinking[:2000]

                # 校验：必须在候选列表中
                if word in valid_words:
                    return word, reasoning

                print(f"  {C_RED}[LLM] 选了不在候选中的词 '{word}'，回退到算法首选{C_RESET}")
                self.last_llm_log["status"] = "invalid_word"

            else:
                raw_content = content[:300] if content else "(empty)"
                print(
                    f"\n  {C_RED}┌─ LLM 异常 ({elapsed:.1f}s) {'─'*33}\n"
                    f"  │ 未返回工具调用\n"
                    f"  │ finish_reason: {finish_reason}\n"
                    f"  │ raw_content: {raw_content}\n"
                    f"  └{'─'*50}{C_RESET}\n"
                )
                self.last_llm_log = {
                    "model": self.model, "elapsed": round(elapsed, 2),
                    "user_msg": user_msg, "finish_reason": finish_reason,
                    "raw_content": raw_content, "status": "no_tool_call",
                }

        except Exception as e:
            error_str = str(e)
            elapsed = time.time() - t0
            print(
                f"\n  {C_RED}┌─ LLM 错误 ({elapsed:.1f}s) {'─'*33}\n"
                f"  │ {type(e).__name__}: {error_str[:200]}\n"
                f"  └{'─'*50}{C_RESET}\n"
            )
            self.last_llm_log = {
                "model": self.model, "elapsed": round(elapsed, 2),
                "user_msg": user_msg,
                "error": f"{type(e).__name__}: {error_str[:500]}",
                "status": "exception",
            }

        # 兜底：用算法首选
        fallback = top_guesses[0][0]
        if self.last_llm_log:
            self.last_llm_log["fallback"] = fallback
        return fallback, "(algorithm fallback)"

    def commentate(
        self,
        turn: int,
        tracker: ConstraintTracker,
        candidates_left: int,
        guess: str,
        context: str = "",
    ) -> str:
        """
        让 LLM 为已确定的猜测生成解说（不选词，只解说）。

        用于 Turn 1（固定开局）和候选很少（算法直选）的情况。
        Returns: reasoning 字符串
        """
        self.last_llm_log = None

        if turn == 1:
            user_msg = (
                f"Turn 1/6 开局。你选择了 {guess.upper()} 作为第一个猜测。\n"
                f"这是信息熵最高的开局词，能最大程度排除候选。\n\n"
                f"用中文给观众简短解说一下你的开局策略，1-2句话。"
            )
        else:
            user_msg = (
                f"Turn {turn}/{MAX_TURNS}. {candidates_left} possible words remain.\n\n"
                f"Current knowledge:\n{tracker.describe()}\n\n"
                f"{context}\n"
                f"你选择了 {guess.upper()}。用中文给观众简短解说，1-2句话。"
            )

        messages = [
            {"role": "system", "content": WORDLE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        # 强制选这个词，enum 只有一个
        tools = _build_tools_with_enum([guess])

        print(f"\n  {C_CYAN}[LLM 解说] {guess.upper()}, turn {turn}{C_RESET}")

        try:
            t0 = time.time()
            kwargs = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "stream": True,
            }
            if self.tool_choice:
                kwargs["tool_choice"] = self.tool_choice
            if self.extra_body:
                kwargs["extra_body"] = self.extra_body

            stream = self.client.chat.completions.create(**kwargs)
            thinking = ""
            tool_call_args = ""
            finish_reason = None

            for chunk in stream:
                if time.time() - t0 > 10:
                    stream.close()
                    break
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason or finish_reason
                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    thinking += delta.reasoning_content
                if delta.tool_calls:
                    tc = delta.tool_calls[0]
                    if tc.function.arguments:
                        tool_call_args += tc.function.arguments

            elapsed = time.time() - t0

            # 解析 reasoning
            reasoning = ""
            if tool_call_args:
                try:
                    args = json.loads(tool_call_args)
                    reasoning = args.get("reasoning", "")
                except json.JSONDecodeError:
                    pass

            print(f"  {C_CYAN}[LLM 解说] ({elapsed:.1f}s) \"{reasoning[:100]}\"{C_RESET}")

            self.last_llm_log = {
                "model": self.model,
                "elapsed": round(elapsed, 2),
                "user_msg": user_msg,
                "reasoning": reasoning,
                "status": "ok" if reasoning else "no_reasoning",
                "mode": "commentate",
            }
            if thinking:
                self.last_llm_log["thinking"] = thinking[:2000]

            return reasoning if reasoning else f"选 {guess.upper()} 试试看！"

        except Exception as e:
            print(f"  {C_RED}[LLM 解说] 失败: {e}{C_RESET}")
            self.last_llm_log = {
                "model": self.model,
                "error": str(e)[:200],
                "status": "exception",
                "mode": "commentate",
            }
            return f"选 {guess.upper()} 试试看！"


def _extract_word_from_raw(raw: str, valid_words: list[str]) -> dict:
    """JSON 解析失败时，用正则从 raw arguments 中提取 word"""
    import re
    raw_lower = raw.lower()
    for w in valid_words:
        # 匹配 "word": "xxxxx" 或 "word":"xxxxx"
        if re.search(rf'"word"\s*:\s*"{re.escape(w)}"', raw_lower):
            print(f"  {C_YELLOW}[LLM] JSON 损坏，正则提取到: {w.upper()}{C_RESET}")
            return {"word": w, "reasoning": "(extracted from malformed JSON)"}
    # 没找到 → 返回空，后面会走兜底
    print(f"  {C_RED}[LLM] JSON 损坏且正则提取失败，raw_len={len(raw)}{C_RESET}")
    return {}


def _build_tools_with_enum(valid_words: list[str]) -> list[dict]:
    """构建带 enum 约束的工具定义，限制 LLM 只能选指定词"""
    return [
        {
            "type": "function",
            "function": {
                "name": "guess_word",
                "description": "提交Wordle猜测，从候选词中选一个。reasoning用中文简短解说。",
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


# ─── 游戏循环 ───

class WordleGame:
    """Wordle 游戏主循环"""

    def __init__(self, use_llm: bool = True, model_key: str = "doubao"):
        self.answers, self.all_words = load_words()
        self.answer_set = set(self.answers)
        self.all_word_set = set(self.all_words)
        self.llm = LLMPlayer(model_key) if use_llm else None

    def play(self, answer: str | None = None) -> dict:
        """
        玩一局 Wordle。

        Args:
            answer: 指定答案词，None 则随机选

        Returns:
            {"answer": str, "guesses": [...], "turns": int, "solved": bool}
        """
        if answer is None:
            answer = random.choice(self.answers)
        answer = answer.lower()

        print(f"\n{'='*50}")
        if "--answer" in sys.argv:
            print(f"  WORDLE — 答案: {answer.upper()}")
        else:
            print(f"  WORDLE — 新一局 (答案已隐藏)")
        print(f"{'='*50}\n")

        tracker = ConstraintTracker()
        candidates = list(self.answers)
        guesses_log = []

        for turn in range(1, MAX_TURNS + 1):
            # 选词
            top = None
            if turn == 1:
                # 第一轮固定最优开局
                guess = OPTIMAL_FIRST_GUESS
                print(f"  Turn {turn}: {guess.upper()} (optimal opener)")
                if self.llm:
                    reasoning = self.llm.commentate(turn, tracker, len(candidates), guess)
                else:
                    reasoning = "Optimal first guess by information entropy."
            elif len(candidates) <= 2:
                # 候选很少，算法直选
                guess = candidates[0]
                context = f"只剩 {len(candidates)} 个候选了：{', '.join(c.upper() for c in candidates)}"
                print(f"  Turn {turn}: {guess.upper()} (direct pick, {len(candidates)} left)")
                if self.llm:
                    reasoning = self.llm.commentate(turn, tracker, len(candidates), guess, context)
                else:
                    reasoning = f"Only {len(candidates)} candidate(s) left, picking directly."
            else:
                # 算法 Top-N + LLM 选
                top = solver_client.best_guesses_or_fallback("wordle", candidates, 5, get_best_guesses)
                print(f"  Turn {turn}: {len(candidates)} candidates, Top 5:")
                for i, (w, e) in enumerate(top):
                    marker = " *" if w in self.answer_set else ""
                    print(f"    {i+1}. {w.upper()} (entropy: {e:.2f}){marker}")

                if self.llm:
                    guess, reasoning = self.llm.choose(turn, tracker, len(candidates), top)
                else:
                    guess = top[0][0]
                    reasoning = f"Highest entropy: {top[0][1]:.2f}"

            # 获取反馈
            pattern = get_pattern(guess, answer)
            tracker.update(guess, pattern)
            candidates = filter_candidates(candidates, guess, pattern)

            # 显示
            colored = colorize_guess(guess, pattern)
            print(f"  >> {colored}  ({len(candidates)} remaining)\n")

            turn_log = {
                "turn": turn,
                "guess": guess,
                "pattern": decode_pattern(pattern),
                "reasoning": reasoning,
                "candidates_left": len(candidates),
            }
            # 附加算法候选和 LLM 调用详情
            if top:
                turn_log["top_guesses"] = [{"word": w, "entropy": round(e, 3)} for w, e in top]
            if self.llm and self.llm.last_llm_log:
                turn_log["llm"] = self.llm.last_llm_log
                self.llm.last_llm_log = None
            guesses_log.append(turn_log)

            if pattern == PATTERN_ALL_GREEN:
                print(f"  {C_GREEN}{C_BOLD}Solved in {turn} turn(s)!{C_RESET}\n")
                return {"answer": answer, "guesses": guesses_log, "turns": turn, "solved": True}

        print(f"  {C_RED}Failed! Answer was: {answer.upper()}{C_RESET}\n")
        return {"answer": answer, "guesses": guesses_log, "turns": MAX_TURNS, "solved": False}


def batch_test(n: int):
    """批量测试纯算法性能"""
    game = WordleGame(use_llm=False)
    answers = random.sample(game.answers, min(n, len(game.answers)))

    results = []
    turn_counts = {i: 0 for i in range(1, MAX_TURNS + 2)}  # 1~6 + 7(fail)

    for i, answer in enumerate(answers):
        # 静默模式
        tracker = ConstraintTracker()
        candidates = list(game.answers)
        solved = False

        for turn in range(1, MAX_TURNS + 1):
            if turn == 1:
                guess = OPTIMAL_FIRST_GUESS
            elif len(candidates) <= 2:
                guess = candidates[0]
            else:
                top = solver_client.best_guesses_or_fallback("wordle", candidates, 1, get_best_guesses)
                guess = top[0][0]

            pattern = get_pattern(guess, answer)
            tracker.update(guess, pattern)
            candidates = filter_candidates(candidates, guess, pattern)

            if pattern == PATTERN_ALL_GREEN:
                turn_counts[turn] += 1
                results.append(turn)
                solved = True
                break

        if not solved:
            turn_counts[MAX_TURNS + 1] += 1
            results.append(MAX_TURNS + 1)

        if (i + 1) % 50 == 0:
            avg = sum(results) / len(results)
            print(f"  [{i+1}/{n}] avg={avg:.2f} turns")

    # 统计
    total = len(results)
    solved_count = sum(1 for r in results if r <= MAX_TURNS)
    avg_turns = sum(r for r in results if r <= MAX_TURNS) / max(solved_count, 1)

    print(f"\n{'='*50}")
    print(f"  Batch Test: {n} games")
    print(f"{'='*50}")
    print(f"  Solved:   {solved_count}/{total} ({solved_count/total*100:.1f}%)")
    print(f"  Avg turns (solved): {avg_turns:.2f}")
    print(f"  Distribution:")
    for t in range(1, MAX_TURNS + 2):
        label = str(t) if t <= MAX_TURNS else "X"
        bar = "#" * turn_counts[t]
        pct = turn_counts[t] / total * 100 if total else 0
        print(f"    {label}: {bar} {turn_counts[t]} ({pct:.1f}%)")
    print()


def save_log(result: dict):
    """保存游戏日志"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = log_dir / f"wordle_{ts}.json"
    log_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  日志已保存: {log_file}")


# ─── Playwright 浏览器模式 ───

class BrowserGame:
    """Playwright 控制本地 Wordle 网页"""

    def __init__(self, use_llm: bool = True, headless: bool = False, model_key: str = "doubao"):
        self.answers, self.all_words = load_words()
        self.answer_set = set(self.answers)
        self.llm = LLMPlayer(model_key) if use_llm else None
        self.headless = headless
        self.page = None
        self.browser = None
        self.playwright = None
        self.html_path = Path(__file__).parent / "wordle.html"

    def _start_browser(self):
        """启动浏览器并打开 Wordle 页面"""
        from playwright.sync_api import sync_playwright

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=["--window-size=540,720"],
        )
        self.page = self.browser.new_page(viewport={"width": 520, "height": 700})

        # 打开本地 HTML
        file_url = self.html_path.resolve().as_uri()
        self.page.goto(file_url)
        self.page.wait_for_load_state("domcontentloaded")

        # 注入词库
        self.page.evaluate(
            """([answers, allValid]) => {
                window.WORDLE_API.setWordLists(answers, allValid);
            }""",
            [self.answers, self.all_words],
        )
        print(f"  浏览器已启动，词库已注入 ({len(self.answers)} answers)")

    def _stop_browser(self):
        """关闭浏览器"""
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def _new_game(self, answer: str | None = None):
        """在浏览器中开始新一局"""
        if answer is None:
            answer = random.choice(self.answers)
        self.page.evaluate(
            "answer => window.WORDLE_API.newGameWithAnswer(answer)",
            answer,
        )
        return answer

    def _submit_guess(self, word: str) -> list[int]:
        """
        在浏览器中提交猜测，等待动画，返回 pattern。

        Returns: [0/1/2, 0/1/2, 0/1/2, 0/1/2, 0/1/2]
        """
        state = self.page.evaluate(
            "word => window.WORDLE_API.submitGuess(word)",
            word,
        )
        # 从返回的 state 中读取最近一行的反馈
        row_idx = state["currentRow"] - 1
        if row_idx < 0:
            row_idx = 0
        row = state["rows"][row_idx]

        state_map = {"correct": 2, "present": 1, "absent": 0}
        pattern = [state_map.get(t["state"], 0) for t in row]
        return pattern

    def _pattern_to_int(self, pattern_list: list[int]) -> int:
        """将 [0,1,2,...] 列表转为 base-3 整数编码"""
        return sum(v * (3 ** i) for i, v in enumerate(pattern_list))

    def play(self, answer: str | None = None, num_games: int = 1) -> list[dict]:
        """
        在浏览器中玩 Wordle。

        Args:
            answer: 指定第一局答案，None 随机
            num_games: 连续玩几局

        Returns:
            [result_dict, ...]
        """
        self._start_browser()
        results = []

        try:
            for game_idx in range(num_games):
                ans = answer if game_idx == 0 and answer else None
                result = self._play_one(ans)
                results.append(result)

                if game_idx < num_games - 1:
                    # 等一下再开新一局
                    time.sleep(2)
                    # 点掉结果弹窗
                    self.page.evaluate("document.getElementById('overlay').classList.remove('show')")

            # 最后一局保持画面
            if not self.headless:
                try:
                    print(f"\n  浏览器保持打开，按 Enter 退出...")
                    input()
                except EOFError:
                    time.sleep(5)

        except KeyboardInterrupt:
            print("\n  用户中断")
        finally:
            self._stop_browser()

        return results

    def _play_one(self, answer: str | None = None) -> dict:
        """玩一局"""
        answer = self._new_game(answer)

        print(f"\n{'='*50}")
        print(f"  WORDLE [Browser] — 新一局")
        print(f"{'='*50}\n")

        tracker = ConstraintTracker()
        candidates = list(self.answers)
        guesses_log = []

        for turn in range(1, MAX_TURNS + 1):
            # 选词逻辑（跟终端模式一样）
            top = None
            if turn == 1:
                guess = OPTIMAL_FIRST_GUESS
                print(f"  Turn {turn}: {guess.upper()} (optimal opener)")
                if self.llm:
                    reasoning = self.llm.commentate(turn, tracker, len(candidates), guess)
                else:
                    reasoning = "Optimal first guess by information entropy."
            elif len(candidates) <= 2:
                guess = candidates[0]
                context = f"只剩 {len(candidates)} 个候选了：{', '.join(c.upper() for c in candidates)}"
                print(f"  Turn {turn}: {guess.upper()} (direct pick, {len(candidates)} left)")
                if self.llm:
                    reasoning = self.llm.commentate(turn, tracker, len(candidates), guess, context)
                else:
                    reasoning = f"Only {len(candidates)} candidate(s) left."
            else:
                top = solver_client.best_guesses_or_fallback("wordle", candidates, 5, get_best_guesses)
                print(f"  Turn {turn}: {len(candidates)} candidates, Top 5:")
                for i, (w, e) in enumerate(top):
                    marker = " *" if w in self.answer_set else ""
                    print(f"    {i+1}. {w.upper()} (entropy: {e:.2f}){marker}")

                if self.llm:
                    guess, reasoning = self.llm.choose(turn, tracker, len(candidates), top)
                else:
                    guess = top[0][0]
                    reasoning = f"Highest entropy: {top[0][1]:.2f}"

            # 在浏览器中提交并读取反馈
            pattern_list = self._submit_guess(guess)
            pattern = self._pattern_to_int(pattern_list)

            tracker.update(guess, pattern)
            candidates = filter_candidates(candidates, guess, pattern)

            colored = colorize_guess(guess, pattern)
            print(f"  >> {colored}  ({len(candidates)} remaining)\n")

            turn_log = {
                "turn": turn,
                "guess": guess,
                "pattern": pattern_list,
                "reasoning": reasoning,
                "candidates_left": len(candidates),
            }
            if top:
                turn_log["top_guesses"] = [{"word": w, "entropy": round(e, 3)} for w, e in top]
            if self.llm and self.llm.last_llm_log:
                turn_log["llm"] = self.llm.last_llm_log
                self.llm.last_llm_log = None
            guesses_log.append(turn_log)

            if pattern == PATTERN_ALL_GREEN:
                print(f"  {C_GREEN}{C_BOLD}Solved in {turn} turn(s)!{C_RESET}\n")
                return {"answer": answer, "guesses": guesses_log, "turns": turn, "solved": True}

        print(f"  {C_RED}Failed! Answer was: {answer.upper()}{C_RESET}\n")
        return {"answer": answer, "guesses": guesses_log, "turns": MAX_TURNS, "solved": False}


# ─── 入口 ───

def main():
    parser = argparse.ArgumentParser(description="Wordle Bot")
    parser.add_argument("--answer", type=str, help="指定答案词")
    parser.add_argument("--batch", type=int, help="批量测试 N 局（纯算法）")
    parser.add_argument("--no-llm", action="store_true", help="不调用 LLM")
    parser.add_argument("--browser", action="store_true", help="浏览器模式（Playwright）")
    parser.add_argument("--headless", action="store_true", help="无头浏览器（不显示窗口）")
    parser.add_argument("--games", type=int, default=1, help="浏览器模式连续玩几局")
    parser.add_argument("--model", type=str, choices=list(LLM_MODELS.keys()), default="doubao",
                        help="LLM 模型选择 (default: doubao)")
    args = parser.parse_args()

    # 批量模式
    if args.batch:
        batch_test(args.batch)
        return

    # 浏览器模式
    if args.browser:
        game = BrowserGame(
            use_llm=not args.no_llm,
            headless=args.headless,
            model_key=args.model,
        )
        results = game.play(answer=args.answer, num_games=args.games)
        for r in results:
            save_log(r)
        return

    # 终端模式
    game = WordleGame(use_llm=not args.no_llm, model_key=args.model)
    result = game.play(answer=args.answer)
    save_log(result)


if __name__ == "__main__":
    main()
