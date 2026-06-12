"""
汉兜（成语 Wordle）算法引擎 — 四维信息熵求解器

核心机制：每次猜测在 4 个维度上独立反馈
  1. 汉字 — 字本身是否正确、位置是否正确
  2. 声母 — 拼音声母是否匹配
  3. 韵母 — 拼音韵母是否匹配
  4. 声调 — 声调（1-4）是否匹配

词库来源：Handle 开源项目 (https://github.com/antfu/handle)
  - 8000 条四字成语 + 精确拼音标注（含多音字处理）
"""

import json
import math
import os
import random
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# ─── 常量 ───

WORDS_FILE = Path(__file__).parent / "handle_words.json"
WORD_LENGTH = 4
MAX_TURNS = 6

# 声母表（21 个 + 空声母）
INITIALS = [
    "zh", "ch", "sh",  # 双字母声母放前面（贪心匹配）
    "b", "p", "m", "f", "d", "t", "n", "l",
    "g", "k", "h", "j", "q", "x",
    "r", "z", "c", "s", "y", "w",
]

# 三维反馈：字、声母、韵母（不含声调）
# 单位全 exact = 2*9 + 2*3 + 2 = 26
# 每位 3^3 = 27 种组合
# 全局全 exact = 26 + 26*27 + 26*27^2 + 26*27^3
DIMS_PER_POS = 27   # 3^3
UNIT_ALL_EXACT = 2 * 9 + 2 * 3 + 2  # = 26
PATTERN_ALL_GREEN = sum(UNIT_ALL_EXACT * (DIMS_PER_POS ** i) for i in range(WORD_LENGTH))


# ─── 拼音解析 ───

@dataclass
class ParsedChar:
    """解析后的单个汉字"""
    char: str       # 汉字
    initial: str    # 声母（零声母为 ""）
    final: str      # 韵母
    tone: int       # 声调 1-4（轻声为 0）


@dataclass
class ParsedIdiom:
    """解析后的四字成语"""
    word: str                   # 原始成语文本
    chars: list[ParsedChar]     # 4 个解析后的字


def parse_pinyin(pinyin: str) -> tuple[str, str, int]:
    """
    解析单个拼音音节为 (声母, 韵母, 声调)。

    例:
        "zhuang1" → ("zh", "uang", 1)
        "ai4"     → ("", "ai", 4)
        "yi1"     → ("y", "i", 1)
    """
    # 提取声调（末位数字）
    tone = 0
    if pinyin and pinyin[-1].isdigit():
        tone = int(pinyin[-1])
        base = pinyin[:-1]
    else:
        base = pinyin

    # 匹配声母（贪心：先试长的 zh/ch/sh）
    initial = ""
    for ini in INITIALS:
        if base.startswith(ini):
            initial = ini
            break

    final = base[len(initial):]

    # 特殊处理：ü → v（统一编码）
    final = final.replace("ü", "v")

    return initial, final, tone


def parse_idiom(word: str, pinyins: list[str]) -> ParsedIdiom:
    """将成语 + 拼音列表解析为 ParsedIdiom"""
    chars = []
    for i, (ch, py) in enumerate(zip(word, pinyins)):
        initial, final, tone = parse_pinyin(py)
        chars.append(ParsedChar(char=ch, initial=initial, final=final, tone=tone))
    return ParsedIdiom(word=word, chars=chars)


# ─── 四维 Pattern 匹配 ───

def _match_dimension(guess_vals: list[str], answer_vals: list[str]) -> list[int]:
    """
    对单个维度做 Wordle 式匹配（两遍扫描）。

    返回 [0/1/2, ...] 长度 4：
      2 = exact（值和位置都对）
      1 = misplaced（值存在但位置不对）
      0 = none（值不存在）
    """
    n = len(guess_vals)
    result = [0] * n
    answer_pool = list(answer_vals)  # 可消耗的副本

    # 第一遍：标记 exact
    for i in range(n):
        if guess_vals[i] == answer_pool[i]:
            result[i] = 2
            answer_pool[i] = None  # 已匹配

    # 第二遍：标记 misplaced
    for i in range(n):
        if result[i] == 0 and guess_vals[i] and guess_vals[i] in answer_pool:
            result[i] = 1
            answer_pool[answer_pool.index(guess_vals[i])] = None

    return result


def get_pattern(guess: ParsedIdiom, answer: ParsedIdiom) -> int:
    """
    计算猜测相对于答案的四维反馈 pattern。

    返回整数编码：
      每个位置编码 = char_match*27 + initial_match*9 + final_match*3 + tone_match
      全局编码 = pos0 + pos1*81 + pos2*81^2 + pos3*81^3
    """
    # 提取各维度值（三维：字、声母、韵母）
    g_chars = [c.char for c in guess.chars]
    a_chars = [c.char for c in answer.chars]
    g_initials = [c.initial for c in guess.chars]
    a_initials = [c.initial for c in answer.chars]
    g_finals = [c.final for c in guess.chars]
    a_finals = [c.final for c in answer.chars]

    # 三维独立匹配
    char_match = _match_dimension(g_chars, a_chars)
    init_match = _match_dimension(g_initials, a_initials)
    final_match = _match_dimension(g_finals, a_finals)

    # 编码: char*9 + initial*3 + final, 每位 27 种组合
    pattern = 0
    for i in range(WORD_LENGTH):
        unit = char_match[i] * 9 + init_match[i] * 3 + final_match[i]
        pattern += unit * (DIMS_PER_POS ** i)

    return pattern


def decode_pattern(pattern: int) -> list[dict]:
    """
    解码 pattern 为可读的四维反馈列表。

    返回 [{char: 0/1/2, initial: 0/1/2, final: 0/1/2, tone: 0/1/2}, ...]
    """
    result = []
    for _ in range(WORD_LENGTH):
        unit = pattern % DIMS_PER_POS
        pattern //= DIMS_PER_POS
        final = unit % 3
        unit //= 3
        initial = unit % 3
        char = unit // 3
        result.append({
            "char": char,
            "initial": initial,
            "final": final,
        })
    return result


def pattern_to_text(pattern: int, guess: ParsedIdiom) -> str:
    """将 pattern 转为人类可读的文本描述"""
    decoded = decode_pattern(pattern)
    state_names = {0: "灰", 1: "黄", 2: "绿"}
    parts = []
    for i, d in enumerate(decoded):
        ch = guess.chars[i]
        parts.append(
            f"{ch.char}(字{state_names[d['char']]} "
            f"声{state_names[d['initial']]} "
            f"韵{state_names[d['final']]})"
        )
    return " | ".join(parts)


# ─── 过滤 & 信息熵 ───

def filter_candidates(
    candidates: list[ParsedIdiom],
    guess: ParsedIdiom,
    pattern: int,
) -> list[ParsedIdiom]:
    """根据猜测和 pattern 反馈过滤候选成语"""
    return [c for c in candidates if get_pattern(guess, c) == pattern]


def compute_entropy(guess: ParsedIdiom, candidates: list[ParsedIdiom]) -> float:
    """计算某个猜测对当前候选集的 Shannon 信息熵"""
    if not candidates:
        return 0.0

    pattern_counts: dict[int, int] = {}
    for answer in candidates:
        p = get_pattern(guess, answer)
        pattern_counts[p] = pattern_counts.get(p, 0) + 1

    n = len(candidates)
    entropy = 0.0
    for count in pattern_counts.values():
        prob = count / n
        entropy -= prob * math.log2(prob)

    return entropy


def get_best_guesses(
    candidates: list[ParsedIdiom],
    n: int = 5,
    sample_limit: int = 500,
) -> list[tuple[str, float]]:
    """
    返回信息熵最高的 N 个猜测词。

    候选 > sample_limit 时抽样计算（避免 O(n²) 过慢）。
    """
    if len(candidates) <= 2:
        return [(c.word, 0.0) for c in candidates[:n]]

    # 大候选集：抽样计算熵
    eval_pool = candidates
    if len(candidates) > sample_limit:
        eval_pool = random.sample(candidates, sample_limit)

    scored = []
    for idiom in eval_pool:
        e = compute_entropy(idiom, candidates)
        scored.append((idiom.word, e))

    scored.sort(key=lambda x: -x[1])
    return scored[:n]


# ─── 约束追踪器 ───

class ConstraintTracker:
    """
    维护四维约束信息，生成中文描述（给 LLM prompt 用）。
    """

    def __init__(self):
        self.guesses: list[tuple[str, int, ParsedIdiom]] = []  # (word, pattern, parsed)
        # 字级别
        self.green_chars: dict[int, str] = {}       # {位置: 字}
        self.yellow_chars: dict[str, set[int]] = {}  # {字: {排除位置}}
        self.gray_chars: set[str] = set()
        # 声母
        self.green_initials: dict[int, str] = {}
        self.yellow_initials: dict[str, set[int]] = {}
        self.gray_initials: set[str] = set()
        # 韵母
        self.green_finals: dict[int, str] = {}
        self.yellow_finals: dict[str, set[int]] = {}
        self.gray_finals: set[str] = set()

    def update(self, guess: ParsedIdiom, pattern: int):
        """根据猜测和反馈更新约束"""
        decoded = decode_pattern(pattern)
        self.guesses.append((guess.word, pattern, guess))

        # 逐维度更新
        for i, d in enumerate(decoded):
            ch = guess.chars[i]

            # 字
            self._update_dim(
                d["char"], ch.char, i,
                self.green_chars, self.yellow_chars, self.gray_chars,
                [c.char for c in guess.chars], decoded, "char",
            )
            # 声母
            if ch.initial:
                self._update_dim(
                    d["initial"], ch.initial, i,
                    self.green_initials, self.yellow_initials, self.gray_initials,
                    [c.initial for c in guess.chars], decoded, "initial",
                )
            # 韵母
            self._update_dim(
                d["final"], ch.final, i,
                self.green_finals, self.yellow_finals, self.gray_finals,
                [c.final for c in guess.chars], decoded, "final",
            )

    @staticmethod
    def _update_dim(match_val, val, pos, greens, yellows, grays,
                    all_vals, all_decoded, dim_key):
        """更新单个维度的约束"""
        if match_val == 2:  # exact
            greens[pos] = val
        elif match_val == 1:  # misplaced
            if val not in yellows:
                yellows[val] = set()
            yellows[val].add(pos)
        else:  # none
            # 只有当该值在本次猜测中没有任何 green/yellow 时才标灰
            confirmed = any(
                all_vals[j] == val and all_decoded[j][dim_key] in (1, 2)
                for j in range(WORD_LENGTH)
            )
            if not confirmed:
                grays.add(val)

    def describe(self) -> str:
        """生成中文约束描述（给 LLM prompt 用）"""
        lines = []

        # 已确认的字
        if self.green_chars:
            parts = [f"第{i+1}字='{v}'" for i, v in sorted(self.green_chars.items())]
            lines.append(f"✅ 确认汉字: {', '.join(parts)}")
        if self.yellow_chars:
            parts = [f"'{k}'(不在第{'、'.join(str(p+1) for p in sorted(v))}位)"
                     for k, v in sorted(self.yellow_chars.items())]
            lines.append(f"🟡 存在但位置不对: {', '.join(parts)}")
        if self.gray_chars:
            lines.append(f"❌ 排除汉字: {'、'.join(sorted(self.gray_chars))}")

        # 声母
        if self.green_initials:
            parts = [f"第{i+1}位={v}" for i, v in sorted(self.green_initials.items())]
            lines.append(f"✅ 确认声母: {', '.join(parts)}")
        if self.yellow_initials:
            parts = [f"{k}(不在第{'、'.join(str(p+1) for p in sorted(v))}位)"
                     for k, v in sorted(self.yellow_initials.items())]
            lines.append(f"🟡 声母存在但位置不对: {', '.join(parts)}")

        # 韵母
        if self.green_finals:
            parts = [f"第{i+1}位={v}" for i, v in sorted(self.green_finals.items())]
            lines.append(f"✅ 确认韵母: {', '.join(parts)}")
        if self.yellow_finals:
            parts = [f"{k}(不在第{'、'.join(str(p+1) for p in sorted(v))}位)"
                     for k, v in sorted(self.yellow_finals.items())]
            lines.append(f"🟡 韵母存在但位置不对: {', '.join(parts)}")

        # 猜测历史
        if self.guesses:
            lines.append("\n已猜成语:")
            for word, pattern, parsed in self.guesses:
                lines.append(f"  {word} → {pattern_to_text(pattern, parsed)}")

        return "\n".join(lines) if lines else "暂无线索（第一次猜测）"


# ─── 词库管理 ───

_idiom_cache: list[ParsedIdiom] | None = None


def load_idioms() -> list[ParsedIdiom]:
    """
    加载成语词库。返回 ParsedIdiom 列表。

    首次运行从 GitHub 下载，之后使用本地缓存。
    """
    global _idiom_cache
    if _idiom_cache is not None:
        return _idiom_cache

    if WORDS_FILE.exists():
        data = json.loads(WORDS_FILE.read_text(encoding="utf-8"))
        idioms = []
        for entry in data:
            pinyins = entry["pinyin"].split()
            idioms.append(parse_idiom(entry["word"], pinyins))
        _idiom_cache = idioms
        return idioms

    print("[handle_engine] 词库不存在，正在下载...")
    idioms = _download_idioms()

    # 保存缓存
    data = [{"word": idm.word, "pinyin": " ".join(
        f"{c.initial}{c.final}{c.tone}" for c in idm.chars
    )} for idm in idioms]
    WORDS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[handle_engine] 词库已保存: {len(idioms)} 条成语")

    _idiom_cache = idioms
    return idioms


def _download_idioms() -> list[ParsedIdiom]:
    """从 Handle 项目下载成语 + 拼音数据"""
    import requests
    from pypinyin import pinyin, Style

    base = "https://raw.githubusercontent.com/antfu/handle/main/src/data"

    # 下载成语列表
    resp = requests.get(f"{base}/idioms.txt", timeout=30)
    resp.raise_for_status()
    idiom_lines = resp.text.strip().split("\n")
    idiom_list = [line.strip() for line in idiom_lines if len(line.strip()) == 4]

    # 下载多音字覆盖表（3400+ 条显式拼音，解决多音字歧义）
    resp = requests.get(f"{base}/polyphones.json", timeout=30)
    resp.raise_for_status()
    polyphones: dict = resp.json()

    # 合并：polyphones 覆盖多音字，其余用 pypinyin 生成
    idioms = []
    poly_used = 0
    for word in idiom_list:
        if word in polyphones:
            pinyins = polyphones[word].split()
            poly_used += 1
        else:
            # pypinyin 返回 [['yi1'], ['ding1'], ...] 格式
            pinyins = [p[0] for p in pinyin(word, style=Style.TONE3)]

        if len(pinyins) == 4:
            idioms.append(parse_idiom(word, pinyins))

    print(f"[handle_engine] 下载完成: {len(idiom_list)} 条成语, "
          f"{poly_used} 条多音字覆盖, {len(idioms)} 条可用")
    return idioms


def get_idiom_by_word(word: str, idioms: list[ParsedIdiom] | None = None) -> ParsedIdiom | None:
    """按成语文本查找 ParsedIdiom"""
    if idioms is None:
        idioms = load_idioms()
    for idm in idioms:
        if idm.word == word:
            return idm
    return None


# ─── 预设开局词 ───

# 高熵开局词池（字多样、声母韵母覆盖广的常用成语）
# 实际最优开局需要预计算，这里选择字符不重叠的常见成语
GOOD_OPENERS = [
    "风花雪月", "天长地久", "龙飞凤舞", "山清水秀",
    "光明正大", "心想事成", "万紫千红", "鸟语花香",
    "春暖花开", "海阔天空",
]


# ─── 独立测试 ───

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("=== 汉兜引擎测试 ===\n")

    # 1. 拼音解析测试
    print("拼音解析:")
    test_cases = [
        ("zhuang1", ("zh", "uang", 1)),
        ("ai4", ("", "ai", 4)),
        ("yi1", ("y", "i", 1)),
        ("shi4", ("sh", "i", 4)),
        ("chang2", ("ch", "ang", 2)),
        ("e4", ("", "e", 4)),
    ]
    for py, expected in test_cases:
        result = parse_pinyin(py)
        status = "✓" if result == expected else f"✗ got {result}"
        print(f"  {py:10s} → {result}  {status}")

    # 2. 加载词库
    idioms = load_idioms()
    print(f"\n词库: {len(idioms)} 条成语")
    for idm in idioms[:3]:
        chars_desc = " ".join(f"{c.char}({c.initial}/{c.final}/{c.tone})" for c in idm.chars)
        print(f"  {idm.word}: {chars_desc}")

    # 3. Pattern 匹配测试
    print("\nPattern 匹配:")
    # 自身匹配应该全绿
    test_idiom = idioms[0]
    p = get_pattern(test_idiom, test_idiom)
    assert p == PATTERN_ALL_GREEN, f"自身匹配应全绿，got {p} != {PATTERN_ALL_GREEN}"
    print(f"  {test_idiom.word} vs 自身: 全绿 ✓")

    # 两个不同成语
    if len(idioms) > 1:
        a, b = idioms[0], idioms[1]
        p = get_pattern(a, b)
        desc = pattern_to_text(p, a)
        print(f"  {a.word} vs {b.word}: {desc}")

    # 4. 过滤测试
    print("\n过滤测试:")
    if len(idioms) > 100:
        guess = idioms[0]
        answer = idioms[50]
        p = get_pattern(guess, answer)
        filtered = filter_candidates(idioms[:200], guess, p)
        print(f"  猜 {guess.word} (答案 {answer.word}): {len(idioms[:200])} → {len(filtered)} 候选")

    # 5. 熵计算测试
    print("\n熵计算 (小候选集):")
    small_pool = idioms[:50]
    for idm in small_pool[:3]:
        e = compute_entropy(idm, small_pool)
        print(f"  {idm.word}: entropy = {e:.3f}")

    # 6. 约束追踪
    print("\n约束追踪:")
    tracker = ConstraintTracker()
    if len(idioms) > 50:
        guess = idioms[0]
        answer = idioms[50]
        p = get_pattern(guess, answer)
        tracker.update(guess, p)
        print(tracker.describe())

    print("\n✅ handle_engine 测试完成")
