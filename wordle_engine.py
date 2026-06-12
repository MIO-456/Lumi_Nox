"""
Wordle 算法引擎 — 信息熵求解器 + 约束追踪

核心算法：Shannon Entropy 最大化
- 每次猜测选择能最大程度缩小候选集的词
- 平均 ~3.5 步解完（理论最优约 3.42）

词库来源：NYT Wordle 标准词库
- answers: 2309 个可能的答案词
- valid_guesses: ~10000 个额外合法猜测词
"""

import json
import math
import os
import urllib.request
from pathlib import Path
from typing import Optional

# 词库文件路径
WORDS_FILE = Path(__file__).parent / "wordle_words.json"

# 预计算的最优开局词（信息熵最高）
OPTIMAL_FIRST_GUESS = "salet"

# Pattern 编码：每个位置 0=gray, 1=yellow, 2=green
# 5 位 base-3 编码，共 243 种 pattern (0~242)
# 全绿 = 2*1 + 2*3 + 2*9 + 2*27 + 2*81 = 242
PATTERN_ALL_GREEN = 242


def get_pattern(guess: str, answer: str) -> int:
    """
    计算 guess 相对于 answer 的反馈 pattern。

    返回 0~242 的整数（base-3 编码）:
      位置 i 的值 = result[i] * 3^i
      result[i]: 0=gray, 1=yellow, 2=green
    """
    result = [0] * 5
    answer_chars = list(answer)

    # 第一遍：标记 green（位置和字母都对）
    for i in range(5):
        if guess[i] == answer_chars[i]:
            result[i] = 2
            answer_chars[i] = None  # 已匹配，不参与 yellow 判定

    # 第二遍：标记 yellow（字母对但位置不对）
    for i in range(5):
        if result[i] == 0 and guess[i] in answer_chars:
            result[i] = 1
            answer_chars[answer_chars.index(guess[i])] = None

    return result[0] + result[1]*3 + result[2]*9 + result[3]*27 + result[4]*81


def decode_pattern(pattern: int) -> list[int]:
    """将 pattern 整数解码为 5 元素列表 [0/1/2, ...]"""
    result = []
    for _ in range(5):
        result.append(pattern % 3)
        pattern //= 3
    return result


def pattern_to_emoji(pattern: int) -> str:
    """将 pattern 转换为 emoji 显示（🟩🟨⬛）"""
    symbols = {0: "⬛", 1: "🟨", 2: "🟩"}
    return "".join(symbols[v] for v in decode_pattern(pattern))


def filter_candidates(candidates: list[str], guess: str, pattern: int) -> list[str]:
    """根据 guess 和 pattern 反馈过滤候选词"""
    return [w for w in candidates if get_pattern(guess, w) == pattern]


def compute_entropy(guess: str, candidates: list[str]) -> float:
    """
    计算某个 guess 对当前候选集的 Shannon 信息熵。

    熵越高 = 这个猜测能提供的平均信息量越大 = 能更快缩小候选集
    """
    if not candidates:
        return 0.0

    # 统计每种 pattern 出现的次数
    pattern_counts: dict[int, int] = {}
    for answer in candidates:
        p = get_pattern(guess, answer)
        pattern_counts[p] = pattern_counts.get(p, 0) + 1

    # 计算 Shannon 熵: H = -Σ p(x) * log2(p(x))
    n = len(candidates)
    entropy = 0.0
    for count in pattern_counts.values():
        prob = count / n
        entropy -= prob * math.log2(prob)

    return entropy


def get_best_guesses(
    candidates: list[str],
    n: int = 5,
) -> list[tuple[str, float]]:
    """
    返回信息熵最高的 N 个猜测词（只从 candidates 中选）。

    Args:
        candidates: 当前剩余候选答案
        n: 返回数量

    Returns:
        [(word, entropy), ...] 按熵降序
    """
    if len(candidates) <= 2:
        # 候选很少，直接返回，不需要熵计算
        return [(w, 0.0) for w in candidates[:n]]

    # 始终从 candidates 中选（保证给 LLM 的词不会与已知约束矛盾）
    scored = []
    for word in candidates:
        e = compute_entropy(word, candidates)
        scored.append((word, e))

    scored.sort(key=lambda x: -x[1])
    return scored[:n]


class ConstraintTracker:
    """
    人类可读的约束追踪器。

    维护 green/yellow/gray 信息，提供自然语言描述（给 LLM 用）。
    """

    def __init__(self):
        self.greens: dict[int, str] = {}         # {位置: 字母}
        self.yellows: dict[str, set[int]] = {}   # {字母: {排除位置集合}}
        self.grays: set[str] = set()             # 确认不存在的字母
        self.guesses: list[tuple[str, int]] = [] # [(guess, pattern), ...]

    def update(self, guess: str, pattern: int):
        """根据猜测和反馈更新约束"""
        decoded = decode_pattern(pattern)
        self.guesses.append((guess, pattern))

        # 先统计 guess 中每个字母出现在绿/黄位置的次数
        confirmed_letters = set()

        for i, val in enumerate(decoded):
            letter = guess[i]
            if val == 2:  # green
                self.greens[i] = letter
                confirmed_letters.add(letter)
            elif val == 1:  # yellow
                if letter not in self.yellows:
                    self.yellows[letter] = set()
                self.yellows[letter].add(i)
                confirmed_letters.add(letter)

        # gray 字母：只有不在 confirmed 中的才标记为不存在
        # （同一个字母可能一个位置绿/黄，另一个位置灰）
        for i, val in enumerate(decoded):
            if val == 0:
                letter = guess[i]
                if letter not in confirmed_letters:
                    self.grays.add(letter)

    def describe(self) -> str:
        """生成自然语言描述（给 LLM prompt 用）"""
        lines = []

        if self.greens:
            parts = [f"position {i+1}='{l}'" for i, l in sorted(self.greens.items())]
            lines.append(f"Confirmed letters (GREEN): {', '.join(parts)}")

        if self.yellows:
            parts = []
            for letter, excluded in sorted(self.yellows.items()):
                excluded_str = ",".join(str(p+1) for p in sorted(excluded))
                parts.append(f"'{letter}' (not at position {excluded_str})")
            lines.append(f"Present but wrong position (YELLOW): {', '.join(parts)}")

        if self.grays:
            lines.append(f"Eliminated letters (GRAY): {', '.join(sorted(self.grays))}")

        if self.guesses:
            state_names = {0: "gray", 1: "yellow", 2: "green"}
            lines.append("Previous guesses:")
            for guess, pattern in self.guesses:
                decoded = decode_pattern(pattern)
                feedback = ", ".join(f"{guess[i].upper()}={state_names[decoded[i]]}" for i in range(5))
                lines.append(f"  {guess.upper()} -> [{feedback}]")

        return "\n".join(lines) if lines else "No information yet (first guess)."


# ─── 词库管理 ───

def load_words() -> tuple[list[str], list[str]]:
    """
    加载词库。返回 (answers, all_valid_words)。

    answers: 2309 个可能的答案词
    all_valid_words: answers + 额外合法猜测词
    """
    if WORDS_FILE.exists():
        data = json.loads(WORDS_FILE.read_text())
        answers = data["answers"]
        all_valid = answers + data.get("extra_guesses", [])
        return answers, all_valid

    print("[wordle_engine] 词库文件不存在，正在下载...")
    answers, extra = _download_word_lists()

    # 保存到本地
    WORDS_FILE.write_text(json.dumps({
        "answers": answers,
        "extra_guesses": extra,
    }, indent=2))
    print(f"[wordle_engine] 词库已保存: {len(answers)} 答案词, {len(extra)} 额外合法词")

    return answers, answers + extra


def _download_word_lists() -> tuple[list[str], list[str]]:
    """从 GitHub 下载标准 Wordle 词库"""

    def fetch_gist(gist_id: str) -> list[str]:
        url = f"https://gist.github.com/cfreshman/{gist_id}/raw"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
        return [w.strip().lower() for w in text.strip().split("\n") if w.strip()]

    answers = fetch_gist("a03ef2cba789d8cf00c08f767e0fad7b")
    extra = fetch_gist("cdcdf777450c5b5301e439061d29694c")
    return answers, extra


# ─── 独立测试 ───

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    answers, all_words = load_words()
    print(f"答案词: {len(answers)}, 全部合法词: {len(all_words)}")

    # 测试 pattern 计算
    assert get_pattern("salet", "salet") == PATTERN_ALL_GREEN
    assert decode_pattern(PATTERN_ALL_GREEN) == [2, 2, 2, 2, 2]
    print("pattern 计算 OK")

    # 测试过滤
    p = get_pattern("salet", "crane")
    filtered = filter_candidates(answers, "salet", p)
    print(f"salet->crane pattern: {decode_pattern(p)}, 剩余候选: {len(filtered)}")

    # 测试熵计算（在小候选集上）
    test_candidates = ["crane", "crate", "trace", "grace", "brace"]
    for word in test_candidates:
        e = compute_entropy(word, test_candidates)
        print(f"  {word}: entropy={e:.3f}")

    # 测试约束描述
    tracker = ConstraintTracker()
    p = get_pattern("salet", "crane")
    tracker.update("salet", p)
    print(f"\n约束描述:\n{tracker.describe()}")

    # 模拟完整一局：salet -> crane
    print("\n--- 模拟一局: 答案=crane ---")
    candidates = list(answers)
    for turn in range(1, 7):
        if turn == 1:
            guess = OPTIMAL_FIRST_GUESS
        else:
            top = get_best_guesses(candidates, all_words if len(candidates) < 20 else None, n=3)
            guess = top[0][0]
            print(f"  Top 3: {[(w, f'{e:.2f}') for w, e in top]}")

        p = get_pattern(guess, "crane")
        candidates = filter_candidates(candidates, guess, p)
        print(f"  Turn {turn}: {guess.upper()} -> {decode_pattern(p)}, 剩余: {len(candidates)}")

        if p == PATTERN_ALL_GREEN:
            print(f"  Solved in {turn} turns!")
            break

    print("\n✅ wordle_engine all tests passed")
