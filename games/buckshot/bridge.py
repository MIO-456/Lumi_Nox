"""
恶魔轮盘游戏桥接模块 - 作为 Lumi 子模块运行
TCP 连接 Godot BridgeMod，确定性决策就地执行，不确定局面投递给 Lumi 快脑

可独立运行（调试用）：python -m games.buckshot.bridge
"""

import socket
import json
import time
import sys
import os
import subprocess
import threading
from datetime import datetime
from dataclasses import dataclass, field

from .prompt_context import (
    DUEL_COMMON_STRATEGY_TEXT,
    DUEL_STEALABLE_ITEMS,
    build_available_actions,
    build_duel_action_descriptions,
    build_duel_adrenaline_targets,
    build_duel_available_actions,
    command_from_chinese_action,
    item_to_cn,
    items_to_cn_text,
    shell_to_cn,
)
from .state_semantics import (
    BuckshotEventCausality,
    BuckshotStateLifecycle,
)

# ==================== 常量 ====================

HOST = "127.0.0.1"
PORT = 9876
RECONNECT_INTERVAL = 3


def _maximize_window_for_pid(pid: int) -> bool:
    """Maximize the visible top-level Windows window owned by ``pid``."""
    if os.name != "nt" or not pid:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_proc_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )
        found_hwnds: list[int] = []

        @enum_proc_type
        def find_process_window(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value == pid:
                found_hwnds.append(hwnd)
                return False
            return True

        user32.EnumWindows(find_process_window, 0)
        if not found_hwnds:
            return False
        user32.ShowWindow(found_hwnds[0], 3)  # SW_MAXIMIZE
        return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False

ITEM_NAMES_CN = {
    "handsaw": "手锯", "magnifying glass": "放大镜", "beer": "啤酒",
    "cigarettes": "香烟", "handcuffs": "手铐", "expired medicine": "过期药",
    "burner phone": "一次性手机", "adrenaline": "肾上腺素", "inverter": "逆转器",
    "jammer": "干扰器",
}

ITEM_DESC_CN = {
    "handsaw": "手锯（锯短枪管，本次射击伤害翻倍）",
    "magnifying glass": "放大镜（查看当前膛内子弹类型）",
    "beer": "啤酒（退掉当前膛内的子弹，不发射）",
    "cigarettes": "香烟（回复1点HP）",
    "handcuffs": "手铐（铐住庄家，庄家下回合跳过）",
    "expired medicine": "过期药（50%回复2HP / 50%扣1HP）",
    "burner phone": "一次性手机（随机揭示弹匣中某发子弹类型）",
    "adrenaline": "肾上腺素（偷庄家一个道具并使用）",
    "inverter": "逆转器（将膛内子弹反转：实弹↔空弹）",
    "jammer": "干扰器（让对手跳过下一次行动）",
}

def items_to_cn(items: list) -> list:
    return [ITEM_NAMES_CN.get(i, i) for i in items]

# Godot 路径配置：默认相对本文件（主目录）；worktree 测试可用
# BUCKSHOT_GODOT_DIR 环境变量指到含 Godot 与 buckshot_decompiled 的目录
_BUCKSHOT_BASE_DIR = os.environ.get("BUCKSHOT_GODOT_DIR") or os.path.dirname(__file__)
GODOT_EXE = os.path.join(_BUCKSHOT_BASE_DIR,
    "Godot_v4.1.1-stable_win64.exe", "Godot_v4.1.1-stable_win64_console.exe")
GODOT_PROJECT = os.path.join(_BUCKSHOT_BASE_DIR, "buckshot_decompiled")

# ANSI 颜色
C_RESET = "\033[0m"
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_MAGENTA = "\033[95m"
C_BOLD = "\033[1m"


# ==================== LLM 工具定义（给 Lumi 快脑用）====================

def build_game_tools(
    player_items: list,
    state=None,
    *,
    opponent_name: str = "",
    duel_mode: bool = False,
) -> list:
    """根据玩家当前道具+游戏状态动态构建工具列表"""
    unique_items = list(dict.fromkeys(player_items or []))
    if state:
        if state.dealer_cuffed and "handcuffs" in unique_items:
            unique_items.remove("handcuffs")
        if state.opponent_jammed and "jammer" in unique_items:
            unique_items.remove("jammer")
        if state.barrel_sawed_off and "handsaw" in unique_items:
            unique_items.remove("handsaw")
        if state.player_wire_cut:
            unique_items = [
                item for item in unique_items
                if item not in ("cigarettes", "expired medicine")
            ]
    if duel_mode:
        available_actions = build_duel_available_actions(
            unique_items,
            list(getattr(state, "dealer_items", []) or []),
            state=state,
            opponent_name=opponent_name,
        )
        adrenaline_targets = build_duel_adrenaline_targets(
            unique_items,
            list(getattr(state, "dealer_items", []) or []),
            state=state,
        )
        ordinary_actions = [
            action
            for action in available_actions
            if not action.startswith("使用肾上腺素：")
        ]
    else:
        available_actions = build_available_actions(
            unique_items,
            opponent_name=opponent_name,
        )
        adrenaline_targets = []
        ordinary_actions = available_actions
    tools = [{
        "type": "function",
        "function": {
            "name": "choose_buckshot_action",
            "description": "选择射击或普通道具动作。肾上腺素必须使用专用工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "动作": {
                        "type": "string",
                        "enum": ordinary_actions,
                        "description": "只能选择列表中的中文动作",
                    },
                },
                "required": ["动作"],
            },
        },
    }]
    if adrenaline_targets:
        tools.append({
            "type": "function",
            "function": {
                "name": "use_buckshot_adrenaline",
                "description": (
                    "一次调用完成使用肾上腺素、偷取指定的对手道具并立即使用。"
                    "不要再调用普通动作工具。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "目标道具": {
                            "type": "string",
                            "enum": [item_to_cn(item) for item in adrenaline_targets],
                            "description": "要从对手处偷走并立即使用的道具",
                        },
                    },
                    "required": ["目标道具"],
                },
            },
        })
    return tools


BUCKSHOT_GAME_PROMPT = """## 恶魔轮盘决策
根据当前局面和可选动作做一个决定。
说话要短，必须和最终选择的动作一致。
不要说工具名、函数名、英文参数。
不要说概率数字。
不要编造当前局面里没有写的事件。"""


# ==================== 游戏状态 ====================

@dataclass
class GameState:
    phase: str = "unknown"
    health_player: int = 0
    health_opponent: int = 0
    max_health: int = 0
    shells_sequence: list = field(default_factory=list)
    shells_remaining: int = 0
    shells_live_total: int = 0
    shells_blank_total: int = 0
    current_shell: str = "unknown"
    shotgun_damage: int = 1
    barrel_sawed_off: bool = False
    dealer_cuffed: bool = False
    player_cuffed: bool = False
    player_jammed: bool = False
    opponent_jammed: bool = False
    player_wire_cut: bool = False
    dealer_wire_cut: bool = False
    player_items: list = field(default_factory=list)
    dealer_items: list = field(default_factory=list)
    round: int = 0
    batch: int = 0
    endless: bool = False
    shells_ready: bool = False
    raw_shells_remaining: int = 0

    @property
    def live_remaining(self) -> int:
        return self.shells_sequence.count("live")

    @property
    def blank_remaining(self) -> int:
        return self.shells_sequence.count("blank")

    @property
    def live_probability(self) -> float:
        if self.shells_remaining == 0:
            return 0.0
        return self.live_remaining / self.shells_remaining

    def format_for_llm(self, controller_name: str = "操作者", known_intel_text: str = "当前膛内未知") -> str:
        """格式化局面描述给 LLM 看（无透视信息）"""
        live = self.live_remaining
        blank = self.blank_remaining
        total = live + blank
        items_cn = items_to_cn_text(self.player_items)
        dealer_items_cn = items_to_cn_text(self.dealer_items)

        return (
            f"当前阶段：{controller_name} 的回合\n"
            "游戏状态：进行中\n\n"
            "当前局面：\n"
            f"{controller_name} 的血量：{self.health_player}/{self.max_health}\n"
            f"庄家的血量：{self.health_opponent}/{self.max_health}\n"
            f"弹匣剩余：共{total}发（{live}发实弹，{blank}发空弹）\n"
            f"已知情报：{known_intel_text or '当前膛内未知'}\n"
            f"枪管已锯短：{'是，本次射击伤害翻倍' if self.barrel_sawed_off else '否'}\n"
            f"庄家被铐住：{'是，庄家下回合跳过' if self.dealer_cuffed else '否'}\n"
            f"断线状态：{'是，当前无法通过香烟或过期药回血' if self.player_wire_cut else '否'}\n"
            f"{controller_name} 的道具：{items_cn}\n"
            f"庄家的道具：{dealer_items_cn}"
        )

    @classmethod
    def from_mp_snapshot(
        cls,
        snap: dict,
        slot_provider,
        *,
        perspective_slot: int | None = None,
    ) -> "GameState":
        """Hotseat duel: build a GameState from a parse_mp_state snapshot.

        Single-player field names are reused with actor-relative semantics:
        "player" is the requested perspective and "opponent" is the other slot.
        """
        players = snap.get("players", {})
        shells = snap.get("shells", {})
        live = int(shells.get("live") or 0)
        blank = int(shells.get("blank") or 0)
        remaining = int(shells.get("remaining") or 0)
        current_slot = snap.get("current_slot")
        if perspective_slot not in (1, 3):
            perspective_slot = current_slot if current_slot in (1, 3) else 1
        opponent_slot = 3 if perspective_slot == 1 else 1
        actor = players.get(perspective_slot, {})
        opponent = players.get(opponent_slot, {})
        ready = bool(snap.get("ready", True))

        def safe_health(value) -> int:
            try:
                health = int(value)
            except (TypeError, ValueError):
                return 0
            return health if ready and 0 <= health <= 4 else 0

        state = cls(
            # 多人对打使用行动者视角：无论槽位 1 还是槽位 3，只要当前槽位
            # 等于这份视角，就都是“自己的回合”。另一个玩家的视角才是对手回合。
            # 槽位 5 是官方“无人行动”占位值（未开局）。
            phase=(
                "player_turn" if current_slot == perspective_slot
                else ("dealer_turn" if current_slot in (1, 3) else "waiting")
            ),
            health_player=safe_health(actor.get("health")),
            health_opponent=safe_health(opponent.get("health")),
            max_health=4,  # 多人模式血量按回合配置，Python 侧以快照为准即可
            shells_sequence=["live"] * live + ["blank"] * blank,
            shells_remaining=remaining,
            shells_live_total=live,
            shells_blank_total=blank,
            player_items=list(actor.get("items", [])),
            dealer_items=list(opponent.get("items", [])),
            player_jammed=bool(actor.get("jammed", False)),
            opponent_jammed=bool(opponent.get("jammed", False)),
            barrel_sawed_off=bool(snap.get("barrel_sawed_off", False)),
            shotgun_damage=int(snap.get("shotgun_damage") or 1),
            round=int(snap.get("round_index") or 0),
            shells_ready=ready,
            raw_shells_remaining=remaining,
        )
        return state

    def summary(self, *, player_label: str = "玩家", opponent_label: str = "庄家") -> str:
        """终端打印用的彩色摘要"""
        hp_bar_p = "♥" * self.health_player + "♡" * (self.max_health - self.health_player)
        hp_bar_d = "♥" * self.health_opponent + "♡" * (self.max_health - self.health_opponent)
        shells_vis = ""
        for s in self.shells_sequence:
            shells_vis += f"{C_RED}●{C_RESET}" if s == "live" else f"{C_GREEN}○{C_RESET}"
        shell_lines = (
            [
                f"  弹壳序列: [{shells_vis}]  ({self.live_remaining}实弹/{self.blank_remaining}空弹)",
                f"  当前弹: {C_BOLD}{self.current_shell}{C_RESET}  |  伤害: {self.shotgun_damage}",
            ]
            if self.shells_ready
            else [
                "  弹匣: 准备中（旧弹匣/清空瞬态已隐藏）",
                f"  伤害: {self.shotgun_damage}",
            ]
        )
        lines = [
            f"\n{'='*50}",
            f"  阶段: {C_BOLD}{self.phase}{C_RESET}  |  回合 {self.round}  |  Batch {self.batch}",
            f"  {player_label} HP: {C_GREEN}{hp_bar_p}{C_RESET}  ({self.health_player}/{self.max_health})",
            f"  {opponent_label} HP: {C_RED}{hp_bar_d}{C_RESET}  ({self.health_opponent}/{self.max_health})",
            *shell_lines,
            f"  {player_label}道具: {items_to_cn(self.player_items)}",
            f"  {opponent_label}道具: {items_to_cn(self.dealer_items)}",
            f"  手铐: {opponent_label}={self.dealer_cuffed} {player_label}={self.player_cuffed}",
            f"  干扰: {opponent_label}={self.opponent_jammed} {player_label}={self.player_jammed}",
            f"{'='*50}",
        ]
        return "\n".join(lines)


# ==================== 游戏决策请求（投递给 Lumi 快脑）====================

def _duel_state_text(state: GameState, slot: int, slot_provider, *, known_intel_text: str) -> str:
    """hotseat duel 决策局面文本：双玩家视角（槽位1=Lumi 位、槽位3=Nox 位）。"""
    my_name = slot_provider(slot)
    opp_slot = 3 if int(slot) == 1 else 1
    opp_name = slot_provider(opp_slot)
    live = state.live_remaining
    blank = state.blank_remaining
    total = live + blank
    items_cn = items_to_cn_text(state.player_items)
    opp_items_cn = items_to_cn_text(state.dealer_items)
    return (
        f"当前阶段：{my_name} 的回合（{my_name} 与 {opp_name} 正在对打恶魔轮盘）\n"
        "游戏状态：进行中\n\n"
        "当前局面：\n"
        f"{my_name} 的血量：{state.health_player}/{state.max_health}\n"
        f"{opp_name} 的血量：{state.health_opponent}/{state.max_health}\n"
        f"弹匣剩余：共{total}发（{live}发实弹，{blank}发空弹）\n"
        f"已知情报：{known_intel_text or '当前膛内未知'}\n"
        f"{opp_name} 已被干扰：{'是，下一次行动会跳过' if state.opponent_jammed else '否'}\n"
        f"{my_name} 的道具：{items_cn}\n"
        f"{opp_name} 的道具：{opp_items_cn}"
    )


@dataclass
class GameDecisionRequest:
    """由 bridge 线程创建，投递给 Lumi 快脑，等待结果"""
    state: GameState                    # 游戏状态快照
    state_text: str                     # format_for_llm() 输出
    intel_text: str                     # 已知情报描述
    tools: list                         # Function Calling 工具定义
    controller_name: str = "操作者"
    game_status: str = "进行中"
    last_action_result: str = ""
    result_event: threading.Event = field(default_factory=threading.Event)
    result: dict = field(default_factory=dict)  # Lumi 快脑写入决策结果
    cancelled: bool = False
    slot: int = 1                       # hotseat duel: 当前行动槽位
    role_name: str = ""                 # hotseat duel: 槽位对应的角色名（由 slot_provider 翻译）
    opponent_name: str = ""             # hotseat duel: 对手名（同样来自 slot_provider）
    available_actions: list = field(default_factory=list)
    action_descriptions: dict = field(default_factory=dict)
    strategy_text: str = ""
    output_id: str | None = None         # 本次决策话术对应的语音输出
    speech_done_event: threading.Event = field(default_factory=threading.Event)
    speech_had_text: bool = False        # 工具调用可以没有台词；完成信号仍必须发出
    action_id: str = ""                 # 本次动作的唯一编号


# ==================== 确定性决策引擎 ====================

class DeterministicEngine:
    """确定性策略 + 情报追踪 + 兜底概率"""

    def __init__(self):
        self.known_shell = None
        self.known_positions = {}
        self._last_shells_remaining = -1

    def on_action_executed(self, msg: dict):
        """处理道具使用结果，更新情报"""
        item = msg.get("item", "")
        revealed = msg.get("revealed_shell")
        position = msg.get("revealed_position")
        if item == "magnifying glass" and revealed:
            self.known_shell = revealed
            print(f"{C_CYAN}[情报] 放大镜 → 当前弹壳是【{'实弹' if revealed == 'live' else '空弹'}】{C_RESET}")
        elif item == "burner phone" and revealed and position is not None:
            self.known_positions[position] = revealed
            print(f"{C_CYAN}[情报] 手机 → 第{position+1}发是【{'实弹' if revealed == 'live' else '空弹'}】{C_RESET}")
        elif item == "inverter":
            # 逆转器只改变当前弹，不会向玩家展示结果。放大镜知道的当前弹，
            # 或手机情报随前置子弹消耗后已经移动到位置 0 的当前弹，可以据此翻转知识；
            # 手机仍记录在后续位置的情报不受逆转器影响。
            if self.known_shell in {"live", "blank"}:
                self.known_shell = "blank" if self.known_shell == "live" else "live"
            if self.known_positions.get(0) in {"live", "blank"}:
                current = self.known_positions[0]
                self.known_positions[0] = "blank" if current == "live" else "live"
            known = self.effective_known
            if known:
                print(
                    f"{C_CYAN}[情报] 逆转器 → 根据此前情报，反转后当前弹壳是"
                    f"【{'实弹' if known == 'live' else '空弹'}】{C_RESET}"
                )

    def on_state_update(self, state: GameState):
        """弹匣变化时重置情报"""
        if state.shells_remaining != self._last_shells_remaining:
            if state.shells_remaining > self._last_shells_remaining:
                # 装了新弹匣：旧情报全部作废
                self.known_shell = None
                self.known_positions.clear()
            else:
                # 打掉一发（或啤酒退弹）：当前膛内已知作废，手机看到的后续位置整体前移一位。
                # 不能再依赖 known_shell 是否存在——只有手机情报、没放大镜情报时位置也必须前移。
                self.known_shell = None
                self.known_positions = {
                    pos - 1: shell_type
                    for pos, shell_type in self.known_positions.items()
                    if pos > 0
                }
            self._last_shells_remaining = state.shells_remaining

    @property
    def effective_known(self):
        """综合放大镜+手机情报，返回当前膛内已知弹壳类型"""
        if self.known_shell is not None:
            return self.known_shell
        if 0 in self.known_positions:
            return self.known_positions[0]
        return None

    def intel_text(self) -> str:
        """格式化已知情报供 LLM 参考"""
        lines = []
        known = self.effective_known
        if known:
            lines.append(f"当前膛内是【{'实弹' if known == 'live' else '空弹'}】")
        for pos, stype in sorted(self.known_positions.items()):
            if pos > 0:
                lines.append(f"第{pos+1}发是【{'实弹' if stype == 'live' else '空弹'}】")
        return "★ 已知情报：" + "；".join(lines) if lines else ""

    def decide(self, state: GameState) -> dict | None:
        """尝试确定性决策，返回 None 表示需要 LLM"""
        items = state.player_items
        known = self.effective_known

        guaranteed_live = (
            known == "live"
            or (state.live_remaining > 0 and state.blank_remaining == 0)
            or (state.shells_remaining == 1 and state.live_remaining > 0)
        )
        immediate_win = (
            guaranteed_live
            and state.health_opponent > 0
            and state.health_opponent <= max(1, state.shotgun_damage)
        )
        if (
            "jammer" in items
            and not state.opponent_jammed
            and state.live_remaining > 0
            and known != "blank"
            and not immediate_win
        ):
            return {
                "action": "use_item",
                "item": "jammer",
                "reason": "先干扰对手，让对方跳过下一次行动",
            }

        # 只剩1发：100%已知
        if state.shells_remaining == 1:
            if state.live_remaining > 0:
                if "handcuffs" in items and not state.dealer_cuffed:
                    return {"action": "use_item", "item": "handcuffs", "reason": "最后一发实弹，先铐住再打"}
                if "handsaw" in items and not state.barrel_sawed_off:
                    return {"action": "use_item", "item": "handsaw", "reason": "最后一发实弹，锯短枪管翻倍伤害"}
                return {"action": "shoot", "target": "dealer", "reason": "最后一发必是实弹，射庄家！"}
            else:
                if "inverter" in items:
                    return {"action": "use_item", "item": "inverter", "reason": "最后一发空弹，逆转成实弹射庄家"}
                if "cigarettes" in items and state.health_player < state.max_health and not state.player_wire_cut:
                    return {"action": "use_item", "item": "cigarettes", "reason": "最后一发空弹，先抽烟回血"}
                return {"action": "shoot", "target": "self", "reason": "最后一发空弹，射自己白嫖~"}

        # 全是空弹
        if state.live_remaining == 0 and state.blank_remaining > 0:
            if "cigarettes" in items and state.health_player < state.max_health and not state.player_wire_cut:
                return {"action": "use_item", "item": "cigarettes", "reason": "全是空弹，先抽烟回血再射自己"}
            return {"action": "shoot", "target": "self", "reason": "全是空弹，射自己白嫖回合~"}

        # 全是实弹
        if state.blank_remaining == 0 and state.live_remaining > 0:
            if "handcuffs" in items and not state.dealer_cuffed and state.shells_remaining > 1:
                return {"action": "use_item", "item": "handcuffs", "reason": "全是实弹，先铐住再打"}
            if "handsaw" in items and not state.barrel_sawed_off:
                return {"action": "use_item", "item": "handsaw", "reason": "全是实弹，锯短枪管翻倍伤害"}
            return {"action": "shoot", "target": "dealer", "reason": "全是实弹，射庄家！"}

        # 有放大镜且不知道当前弹壳
        if "magnifying glass" in items and known is None:
            return {"action": "use_item", "item": "magnifying glass", "reason": "先看看这发是什么弹"}

        # 确定实弹 → 连招
        if known == "live":
            if "handcuffs" in items and not state.dealer_cuffed and state.shells_remaining > 1:
                return {"action": "use_item", "item": "handcuffs", "reason": "实弹在膛，先铐住你再说"}
            if "handsaw" in items and not state.barrel_sawed_off:
                return {"action": "use_item", "item": "handsaw", "reason": "实弹在膛，锯短枪管伤害翻倍"}
            return {"action": "shoot", "target": "dealer", "reason": "确定是实弹，吃我一枪吧！"}

        # 确定空弹
        if known == "blank":
            if "inverter" in items:
                return {"action": "use_item", "item": "inverter", "reason": "空弹？逆转一下变实弹射你"}
            if "cigarettes" in items and state.health_player < state.max_health and not state.player_wire_cut:
                return {"action": "use_item", "item": "cigarettes", "reason": "反正是空弹，先抽根烟回血"}
            return {"action": "shoot", "target": "self", "reason": "确定是空弹，射自己白嫖一回合~"}

        return None  # 不确定，需要 LLM

    def fallback(self, state: GameState) -> dict:
        """LLM 超时兜底策略"""
        items = state.player_items
        p_live = state.live_probability
        if (
            "cigarettes" in items
            and state.health_player < state.max_health
            and state.health_player <= 2
            and not state.player_wire_cut
        ):
            return {"action": "use_item", "item": "cigarettes", "reason": "兜底：血量危险，先抽烟回血"}
        if "jammer" in items and not state.opponent_jammed and state.live_remaining > 0:
            return {"action": "use_item", "item": "jammer", "reason": "兜底：先干扰对手，跳过对方下一次行动"}
        if "handcuffs" in items and not state.dealer_cuffed and p_live >= 0.5:
            return {"action": "use_item", "item": "handcuffs", "reason": "兜底：先铐住庄家"}
        if "handsaw" in items and not state.barrel_sawed_off and p_live >= 0.5:
            return {"action": "use_item", "item": "handsaw", "reason": "兜底：锯短枪管翻倍伤害"}
        if "beer" in items and p_live >= 0.6:
            return {"action": "use_item", "item": "beer", "reason": "兜底：实弹概率高，啤酒退掉碰运气"}
        if p_live >= 0.5:
            return {"action": "shoot", "target": "dealer", "reason": f"兜底：实弹概率{p_live:.0%}，射庄家"}
        return {"action": "shoot", "target": "self", "reason": f"兜底：空弹概率{1-p_live:.0%}，射自己"}


# ==================== 游戏事件格式化（给 Lumi 看）====================

def _format_deterministic_action(decision: dict, controller_name: str = "操作者") -> str:
    """把确定性策略的动作映射成中文 last_action_result。
    射击的最终结果会被随后的 game_event（含实弹/空弹/伤害）覆盖；
    道具使用没有 game_event，靠这里把动作描述写进快照。
    """
    action = decision.get("action", "")
    name = (controller_name or "操作者").strip() or "操作者"
    if action == "use_item":
        item_cn = ITEM_NAMES_CN.get(decision.get("item", ""), decision.get("item", ""))
        return f"{name} 使用了{item_cn}。"
    if action == "shoot":
        target = decision.get("target", "")
        target_cn = {"dealer": "庄家", "self": "自己"}.get(target, target)
        return f"{name} 射击{target_cn}。"
    return ""


def format_game_event(msg: dict, controller_name: str = "操作者") -> str | None:
    """将游戏事件格式化为中文描述，返回 None 表示不需要记录"""
    event = msg.get("event", "")

    def format_player_item(m: dict) -> str:
        item = str(m.get("item") or "")
        item_cn = item_to_cn(item)
        revealed_shell = shell_to_cn(str(m.get("revealed_shell") or "unknown"))
        if item == "magnifying glass" and revealed_shell != "未知":
            return f"{controller_name} 使用放大镜确认当前膛内是{revealed_shell}。"
        if item == "burner phone" and revealed_shell != "未知":
            try:
                position = int(m.get("revealed_position", -1))
            except (TypeError, ValueError):
                position = -1
            if position >= 0:
                return f"{controller_name} 使用一次性手机确认第{position + 1}发是{revealed_shell}。"
            return f"{controller_name} 使用一次性手机确认了一发{revealed_shell}。"
        if item == "inverter":
            return f"{controller_name} 使用逆转器翻转了当前子弹，具体弹种仍未知。"
        if item == "handsaw":
            return f"{controller_name} 使用手锯锯短了枪管，当前枪伤害翻倍。"
        if item == "handcuffs":
            return f"{controller_name} 使用手铐铐住了庄家。"
        return f"{controller_name} 使用了{item_cn}。"

    def format_dealer_item(m: dict) -> str:
        item = str(m.get("item") or "")
        if item == "magnifying glass":
            return "庄家使用放大镜检查了当前子弹。"
        if item == "handsaw":
            return "庄家使用手锯锯短了枪管。"
        if item == "handcuffs":
            return f"庄家使用手铐铐住了{controller_name}。"
        if item == "inverter":
            return "庄家使用逆转器反转了当前子弹。"
        if item == "burner phone":
            return "庄家使用一次性手机查看了一发子弹。"
        if item == "adrenaline":
            return "庄家使用了肾上腺素。"
        return f"庄家使用了{item_to_cn(item)}。"

    mapping = {
        # 场景过渡事件（加载阶段）
        "auto_intro_start": lambda m: "游戏开始加载，准备进入地下室……",
        "kicking_door_1": lambda m: "踹开了第一扇门！",
        "kicking_door_2": lambda m: "踹开第二扇门，进入地下室！",
        "waiver_signed": lambda m: "签了免责书，正式开局！",
        # 战斗事件
        "dealer_shot_player": lambda m: f"庄家射击了{controller_name}，扣{m.get('damage',1)}点血量（剩余{m.get('hp_remaining',0)}点血量）",
        "player_shot_dealer": lambda m: f"{controller_name} 射击庄家，是实弹，庄家扣{m.get('damage',1)}点血量（剩余{m.get('hp_remaining',0)}点血量）。",
        "player_shot_dealer_blank": lambda m: f"{controller_name} 射击庄家，是空弹，没有造成伤害。",
        "dealer_shot_self": lambda m: f"庄家自射实弹翻车，扣{m.get('damage',1)}点血量",
        "dealer_shot_self_blank": lambda m: "庄家自射空弹，白嫖一回合",
        "dealer_shot_player_blank": lambda m: f"庄家射击{controller_name}，是空弹，没有造成伤害。",
        "player_shot_self_blank": lambda m: f"{controller_name} 射击自己，是空弹，没有扣血，并继续行动。",
        "player_shot_self": lambda m: f"{controller_name} 射击自己，是实弹，扣{m.get('damage',1)}点血量（剩余{m.get('hp_remaining',0)}点血量）。",
        "player_used_item": format_player_item,
        "dealer_used_item": format_dealer_item,
        "player_used_beer": lambda m: f"{controller_name} 使用啤酒退掉了当前子弹（{shell_to_cn(m.get('ejected_shell', 'unknown'))}），没有开枪。",
        "dealer_used_beer": lambda m: f"庄家使用啤酒退掉了当前子弹（{shell_to_cn(m.get('ejected_shell', 'unknown'))}），没有开枪。",
        "player_took_damage": lambda m: f"{controller_name} 受伤{m.get('damage',1)}点血量。",
        "dealer_took_item_damage": lambda m: f"庄家因道具副作用受伤{m.get('damage',1)}点血量。",
        "player_healed": lambda m: f"{controller_name} 回复{m.get('amount',1)}点血量（当前{m.get('hp_now',0)}点血量）",
        "dealer_healed": lambda m: f"庄家回复{m.get('amount',1)}点血量",
        "player_wire_cut": lambda m: f"{controller_name} 的除颤器电线被切断，血量锁到{m.get('hp_now',1)}点，无法再用回血道具。",
        "dealer_wire_cut": lambda m: f"庄家的除颤器电线被切断，血量锁到{m.get('hp_now',1)}点，无法再用回血道具。",
        "batch_won": lambda m: "庄家被击败！",
        "victory": lambda m: "游戏胜利！",
        "defeat": lambda m: f"{controller_name} 被击败了（{'天堂' if m.get('death_path')=='heaven' else '复活'}路径）",
        "round_start": lambda m: f"新回合开始（回合{m.get('round',0)}·阶段{m.get('batch',0)}）",
    }
    formatter = mapping.get(event)
    if formatter:
        return formatter(msg)
    return None


def public_known_intel_text(intel_text: str = "") -> str:
    """只返回道具实际揭示过的情报；不读取游戏原始 current_shell。"""
    lines = []
    for line in (intel_text or "").splitlines():
        line = line.strip()
        if not line or "必须" in line or "工具" in line:
            continue
        for _prefix in ("★ 已知情报：", "☑ 已知情报："):
            if line.startswith(_prefix):
                line = line.removeprefix(_prefix).strip()
                break
        lines.append(line)
    return "；".join(lines) if lines else "当前膛内未知"


# ==================== 桥接主类 ====================

class BuckshotBridge:
    EXPECTED_GLOBAL_STATE = "PLAYING_BUCKSHOT"
    ACTION_ACK_TIMEOUT_SECONDS = 25.0
    DEFAULT_DUEL_TOTAL_ROUNDS = 3
    """恶魔轮盘桥接器 —— 可作为 Lumi 子线程或独立运行"""

    def __init__(self, event_callback=None, bus=None, controller_provider=None,
                 duel_mode: bool = False, slot_provider=None,
                 continue_unit_provider=None):
        """
        event_callback: 可选回调函数 (event_type: str, data: dict) -> None
            event_type: "game_event" | "game_state" | "need_decision" | "decision_executed"
        bus: 可选事件总线实例，传入后优先用总线通信
        controller_provider: 可选 () -> str 回调，每次 bridge 需要 controller 名字时即时拉真值。
            **不在 bridge 缓存** —— 避免缓存值与导演真值（schedule + state machine）漂移
            导致整局 role 错乱（2026-05-17 调试结论）。
            为空时退化成 "操作者" 占位，下游用占位再走自己的兜底链。
        duel_mode: hotseat 双人对打；状态来自 BridgeMod 的 mp_main 推送（parse_mp_state）
        slot_provider: 可选 (slot: int) -> str 回调，槽位→角色名；默认固定映射（阶段一）。
            同样只读不缓存 —— 将来由 director 下发的 players 覆盖。
        continue_unit_provider: 可选 () -> bool 回调；完整对局结束后由导演按节目单
            剩余时间和切换状态决定是否再开一场。默认不续场。
        """
        self._bus = bus
        self.sock = None
        self._has_connected_once = False
        self._startup_wait_logged = False
        self.state = GameState()
        self.engine = DeterministicEngine()
        self._duel_engines: dict[int, DeterministicEngine] = {}
        self._duel_snapshot: dict = {}
        self._duel_last_action_results: dict[int, str] = {1: "", 3: ""}
        self._state_lifecycle = BuckshotStateLifecycle()
        self._event_causality = BuckshotEventCausality()
        self.event_callback = self._publish_to_bus if bus else (event_callback or (lambda t, d: None))
        self.running = True
        self.recv_buffer = ""
        self.last_acted_phase = ""
        # 统计
        self.wins = 0
        self.losses = 0
        # Lumi 快脑决策机制
        self.pending_decision: GameDecisionRequest | None = None
        self._pending_lock = threading.Lock()
        # Godot 进程
        self.godot_process: subprocess.Popen | None = None
        # 日志
        self.log_entries: list = []
        self.start_time = datetime.now()
        # 当前是否在一局游戏中（供导演系统判断）
        self.in_round = False
        # 最近战斗事件只用于内部记录；快脑提示词只使用上一动作结果。
        self._recent_events: list[str] = []
        self._last_action_result = ""
        self._controller_provider = controller_provider or (lambda: "")
        self._continue_unit_provider = continue_unit_provider or (lambda: False)
        self.duel_mode = duel_mode
        if duel_mode:
            from .duel_protocol import make_default_slot_provider
            self._slot_provider = slot_provider or make_default_slot_provider()
            self._current_duel_slot = None
            self._duel_engines = {1: self.engine, 3: DeterministicEngine()}
        else:
            self._slot_provider = None
            self._current_duel_slot = None
        self._game_status = "进行中"
        self._action_generation = 0
        self._action_sequence = 0
        self._event_sequence = 0
        self._pending_commentary_event: dict | None = None
        self._duel_round_finished = False
        self._duel_restart_pending = False
        self._duel_restart_requested = False
        self._pending_execution: dict | None = None
        self._forced_fallback: dict | None = None

        self._activation_event = threading.Event()
        self._last_global_state = ""
        if self._bus:
            self._bus.subscribe("state_changed", self._on_state_changed)
            self._bus.subscribe("game_role_context_changed", self._on_game_role_context_changed)
            self._bus.subscribe("buckshot_commentary_done", self._on_commentary_done)
        else:
            self._activation_event.set()

    def _engine_for_slot(self, slot: int | None = None) -> DeterministicEngine:
        if not self.duel_mode:
            return self.engine
        resolved = slot if slot in self._duel_engines else self._current_duel_slot
        if resolved not in self._duel_engines:
            raise ValueError(f"unknown duel slot: {resolved}")
        return self._duel_engines[resolved]

    def _last_result_for_slot(self, slot: int | None = None) -> str:
        if not self.duel_mode:
            return self._last_action_result
        resolved = slot if slot in (1, 3) else self._current_duel_slot
        return self._duel_last_action_results.get(resolved, "")

    def _set_last_result(self, text: str, slot: int | None = None) -> None:
        if not self.duel_mode:
            self._last_action_result = text
            return
        if slot in (1, 3):
            self._duel_last_action_results[int(slot)] = text

    def _set_public_duel_result(self, text: str) -> None:
        if self.duel_mode:
            self._duel_last_action_results = {1: text, 3: text}
        else:
            self._last_action_result = text

    def _controller_name(self) -> str:
        """即时拉当前操作者名字。永远走 provider，不缓存。"""
        try:
            name = (self._controller_provider() or "").strip()
        except Exception:
            name = ""
        return name or "操作者"

    def _publish_to_bus(self, event_type: str, data: dict):
        """把桥接器事件转发到总线"""
        source = "buckshot_duel" if self.duel_mode else "buckshot_roulette"
        self._bus.publish(event_type, data, source=source)

    def _on_state_changed(self, event):
        new_state = str(event.data.get("new") or "")
        self._last_global_state = new_state
        if new_state == self.EXPECTED_GLOBAL_STATE:
            self._activation_event.set()
            self.log({"type": "state_gate_open", "state": new_state})
            print(f"{C_CYAN}[恶魔轮盘][状态门控] 已进入 {new_state}，允许决策{C_RESET}")
        else:
            self._activation_event.clear()
            self._action_generation += 1
            self.clear_pending_decision(f"state_changed:{new_state or 'unknown'}")

    def _on_game_role_context_changed(self, event):
        """收到角色上下文变更通知时，重新把当前操作者名字下发给 Godot。
        不在本地缓存 controller —— bridge 用到时都从 provider 拉真值。
        """
        data = event.data or {}
        if not self.duel_mode and data.get("game_id") == "buckshot_roulette":
            self._send_player_name()

    def _on_commentary_done(self, event) -> None:
        if getattr(event, "source", "") not in {"conversation", "lumi"}:
            return
        event_id = str((getattr(event, "data", None) or {}).get("event_id") or "")
        pending = self._pending_commentary_event or {}
        if event_id and event_id == pending.get("event_id"):
            self.log({"type": "commentary_done", "event_id": event_id})
            self._pending_commentary_event = None

    def _duel_names(self, slot: int | None = None) -> tuple[str, str]:
        resolved = slot if slot in (1, 3) else self._current_duel_slot
        if not self.duel_mode or resolved not in (1, 3):
            return self._controller_name(), "庄家"
        other = 3 if resolved == 1 else 1
        return self._slot_provider(resolved), self._slot_provider(other)

    def _apply_public_duel_identity(self, decision: dict, slot: int | None) -> tuple[str, str]:
        actor_name, opponent_name = self._duel_names(slot)
        if self.duel_mode:
            decision["reason"] = str(decision.get("reason") or "").replace(
                "庄家", opponent_name or "对手"
            )
        decision["actor_name"] = actor_name
        decision["opponent_name"] = opponent_name
        return actor_name, opponent_name

    def _action_still_valid(
        self,
        generation: int,
        slot: int | None,
        round_index: int | None = None,
    ) -> bool:
        if not self.running or generation != self._action_generation:
            return False
        if not self._is_active_for_decision():
            return False
        if self.duel_mode and self._current_duel_slot != slot:
            return False
        if self.duel_mode and round_index is not None and self.state.round != round_index:
            return False
        return bool(self.state.shells_ready and self.state.shells_remaining > 0)

    def _wait_tts_done(
        self,
        output_id: str | None,
        *,
        generation: int,
        slot: int | None,
        round_index: int | None = None,
        completion_event: threading.Event | None = None,
        timeout: float = 12.0,
    ) -> bool:
        if completion_event is not None:
            deadline = time.monotonic() + float(timeout)
            while time.monotonic() < deadline:
                if completion_event.wait(timeout=0.05):
                    return self._action_still_valid(generation, slot, round_index)
                if not self._action_still_valid(generation, slot, round_index):
                    return False
            self.log({"type": "speech_wait_timeout", "output_id": output_id})
            return self._action_still_valid(generation, slot, round_index)
        if not self._bus or not output_id:
            return self._action_still_valid(generation, slot, round_index)
        done = threading.Event()

        def on_tts_done(event):
            data = getattr(event, "data", None) or {}
            if data.get("output_id") == output_id:
                done.set()

        self._bus.subscribe("tts_done", on_tts_done)
        try:
            deadline = time.monotonic() + float(timeout)
            while time.monotonic() < deadline:
                if done.wait(timeout=0.2):
                    return self._action_still_valid(generation, slot, round_index)
                if not self._action_still_valid(generation, slot, round_index):
                    return False
            self.log({"type": "speech_wait_timeout", "output_id": output_id})
            return self._action_still_valid(generation, slot, round_index)
        finally:
            self._bus.unsubscribe("tts_done", on_tts_done)

    def _wait_result_commentary(
        self,
        *,
        generation: int,
        slot: int | None,
        round_index: int | None = None,
        timeout: float = 12.0,
    ) -> bool:
        deadline = time.monotonic() + float(timeout)
        while self._pending_commentary_event and time.monotonic() < deadline:
            if not self._action_still_valid(generation, slot, round_index):
                return False
            time.sleep(0.2)
        if self._pending_commentary_event:
            self.log({
                "type": "commentary_wait_timeout",
                **self._pending_commentary_event,
            })
            self._pending_commentary_event = None
        return self._action_still_valid(generation, slot, round_index)

    def is_in_round(self) -> bool:
        if not self.running:
            return False
        process = self.godot_process
        if process is not None and process.poll() is not None:
            return False
        pending = self._pending_commentary_event or {}
        if pending.get("event_kind") == "round_result":
            age = time.monotonic() - float(pending.get("created_at", 0.0))
            if age <= 15.0:
                return True
        return bool(self.in_round)

    def _duel_match_complete(self, msg: dict) -> bool:
        """Resolve the full hotseat boundary from the native round result."""
        if not self.duel_mode:
            return False
        if "match_complete" in msg:
            return bool(msg.get("match_complete"))
        try:
            round_index = int(msg.get(
                "round_index",
                self._duel_snapshot.get("round_index", self.state.round),
            ))
            total_rounds = int(msg.get(
                "total_rounds",
                self._duel_snapshot.get(
                    "total_rounds",
                    self.DEFAULT_DUEL_TOTAL_ROUNDS,
                ),
            ))
        except (TypeError, ValueError):
            return False
        return total_rounds > 0 and round_index >= total_rounds - 1

    def _mark_duel_round_finished(self, msg: dict) -> None:
        """Keep inter-round setup inside the duel; release only after the final round."""
        self._duel_round_finished = True
        if self._duel_match_complete(msg):
            self.in_round = False

    def _stage_duel_restart(self, msg: dict) -> bool:
        """Remember the official lobby boundary; the director decides whether to continue."""
        if (
            not self.duel_mode
            or not self._duel_round_finished
            or msg.get("event") != "scene_changed"
            or msg.get("scene") not in {"mp_lobby", "menu"}
        ):
            return False
        self._duel_restart_pending = True
        self._duel_restart_requested = False
        self.log({
            "type": "duel_restart_staged",
            "scene": msg.get("scene"),
        })
        return True

    def _try_restart_duel(self) -> bool:
        """Start exactly one new full match when commentary and schedule allow it."""
        if not self._duel_restart_pending or self._duel_restart_requested:
            return False
        pending_commentary = self._pending_commentary_event or {}
        if pending_commentary:
            age = time.monotonic() - float(pending_commentary.get("created_at", 0.0))
            if age <= 15.0:
                return False
            self.log({
                "type": "commentary_wait_timeout",
                **pending_commentary,
            })
            self._pending_commentary_event = None
        if not self._is_active_for_decision():
            self._duel_restart_pending = False
            return False
        try:
            should_continue = bool(self._continue_unit_provider())
        except Exception as exc:
            self.log({
                "type": "duel_restart_rejected",
                "reason": "director_check_failed",
                "error": str(exc),
            })
            print(
                f"{C_RED}[恶魔轮盘·续场] 导演时间检查失败，不启动新一场: "
                f"{exc}{C_RESET}"
            )
            should_continue = False
        if not should_continue:
            self._duel_restart_pending = False
            self.log({
                "type": "duel_restart_rejected",
                "reason": "segment_boundary_reached",
            })
            print(
                f"{C_CYAN}[恶魔轮盘·续场] 环节已到边界，不再开新一场{C_RESET}"
            )
            return False
        self._duel_restart_requested = True
        self.send_command({
            "action": "start_duel",
            "reason": "segment_time_remaining",
        })
        self.log({
            "type": "duel_restart_requested",
            "reason": "segment_time_remaining",
        })
        print(
            f"{C_CYAN}[恶魔轮盘·续场] 环节仍有时间，从联机大厅再开一场{C_RESET}"
        )
        return True

    def _is_active_for_decision(self) -> bool:
        if not self._bus:
            return True
        return self._activation_event.is_set()

    def log(self, entry: dict):
        entry["timestamp"] = datetime.now().isoformat()
        self.log_entries.append(entry)

    def save_logs(self):
        if not self.log_entries:
            return
        log_dir = os.path.join(os.path.dirname(__file__), "logs")
        os.makedirs(log_dir, exist_ok=True)
        filename = f"buckshot_{self.start_time.strftime('%Y-%m-%d_%H-%M-%S')}.jsonl"
        filepath = os.path.join(log_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            for entry in self.log_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"{C_GREEN}[恶魔轮盘] 日志已保存: {filepath} ({len(self.log_entries)} 条){C_RESET}")

    def launch_godot(self):
        if not os.path.exists(GODOT_EXE):
            raise RuntimeError(f"Godot 引擎可执行文件不存在: {GODOT_EXE}")
        if not os.path.exists(GODOT_PROJECT):
            raise RuntimeError(f"恶魔轮盘游戏项目目录不存在: {GODOT_PROJECT}")
        print(f"{C_CYAN}[恶魔轮盘] 启动 Godot 游戏...{C_RESET}")
        self.godot_process = subprocess.Popen(
            [GODOT_EXE, "--path", GODOT_PROJECT],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"{C_GREEN}[恶魔轮盘] Godot 已启动 (PID: {self.godot_process.pid}){C_RESET}")

    def _restore_window_for_game_event(self, msg: dict) -> bool:
        """Restore the same Godot process window when a following game returns to menu."""
        event = msg.get("event", "")
        returning_to_menu = (
            event == "auto_menu_start"
            or (
                event == "scene_changed"
                and msg.get("scene") in {"menu", "mp_lobby"}
            )
        )
        process = self.godot_process
        if (
            not returning_to_menu
            or process is None
            or process.poll() is not None
        ):
            return False
        restored = _maximize_window_for_pid(process.pid)
        if restored:
            self.log({
                "type": "window_maximized",
                "trigger": event,
                "pid": process.pid,
            })
            print(
                f"{C_CYAN}[恶魔轮盘·窗口] trigger={event} "
                f"PID={process.pid} 已恢复最大化{C_RESET}"
            )
        return restored

    def _reset_for_new_game(self, raw_event: dict) -> None:
        """Atomically clear all per-match state at the full-match boundary."""
        self.clear_pending_decision("starting_game")
        self.state = GameState()
        self.engine = DeterministicEngine()
        if self.duel_mode:
            self._duel_engines = {1: self.engine, 3: DeterministicEngine()}
            self._duel_snapshot = {}
            self._duel_last_action_results = {1: "", 3: ""}
        self._state_lifecycle.reset()
        self._event_causality.reset()
        self._recent_events.clear()
        self._last_action_result = ""
        self._game_status = "进行中"
        self.last_acted_phase = ""
        self.in_round = False
        self._duel_round_finished = False
        self._duel_restart_pending = False
        self._duel_restart_requested = False
        self._pending_commentary_event = None
        self._pending_execution = None
        self._forced_fallback = None
        self._last_shell_audit_key = None
        payload = {
            "event": "starting_game",
            "text": "",
            "raw": raw_event,
            "batch": 0,
            "endless": False,
        }
        self.log({"type": "match_reset", "trigger": "starting_game"})
        self.event_callback("game_event", payload)
        print(
            f"{C_CYAN}[恶魔轮盘·新局重置] trigger=starting_game "
            f"status=进行中 pending=cleared intel=cleared{C_RESET}"
        )

    def connect(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((HOST, PORT))
            self.sock.settimeout(0.1)
            self._has_connected_once = True
            print(f"{C_GREEN}[恶魔轮盘] 已连接到游戏 {HOST}:{PORT}{C_RESET}")
            if self.duel_mode:
                # hotseat: 直接触发本地双人对局（菜单场景走 StartHotseatDuel，
                # 不要 set_auto_mode——那是单机自动打庄家流程）
                self.send_command({"action": "start_duel"})
            else:
                self.send_command({"action": "set_auto_mode", "enabled": True})
                self._send_player_name()
            return True
        except Exception as e:
            failed_sock = self.sock
            self.sock = None
            if failed_sock is not None:
                try:
                    failed_sock.close()
                except Exception:
                    pass

            connection_refused = (
                isinstance(e, ConnectionRefusedError)
                or getattr(e, "winerror", None) == 10061
                or getattr(e, "errno", None) == 10061
            )
            if connection_refused and not self._has_connected_once:
                if not self._startup_wait_logged:
                    print(
                        f"{C_YELLOW}[恶魔轮盘] 等待游戏通信服务启动...{C_RESET}"
                    )
                    self._startup_wait_logged = True
            else:
                label = "重连失败" if self._has_connected_once else "连接失败"
                print(f"{C_RED}[恶魔轮盘] {label}: {e}{C_RESET}")
            return False

    def send_command(self, cmd: dict):
        if not self.sock:
            return
        try:
            if self.duel_mode:
                from .duel_engine import duel_command
                cmd = duel_command(cmd, self._current_duel_slot or 1)
            data = json.dumps(cmd) + "\n"
            self.sock.sendall(data.encode("utf-8"))
            self._event_causality.record_player_command(cmd)
            reason = cmd.get("reason", "")
            item_cn = ITEM_NAMES_CN.get(cmd.get("item", ""), cmd.get("item", "")) if "item" in cmd else ""
            steal_cn = ITEM_NAMES_CN.get(cmd.get("steal_item", ""), cmd.get("steal_item", "")) if "steal_item" in cmd else ""
            target_cn = {"dealer": "庄家", "self": "自己"}.get(cmd.get("target", ""), cmd.get("target", "")) if "target" in cmd else ""
            print(f"{C_MAGENTA}[恶魔轮盘] >>> {cmd['action']}"
                  f"{' → ' + target_cn if target_cn else ''}"
                  f"{' → ' + item_cn if item_cn else ''}"
                  f"{'（偷取并使用' + steal_cn + '）' if steal_cn else ''}"
                  f"  ({reason}){C_RESET}")
        except Exception as e:
            print(f"{C_RED}[恶魔轮盘] 发送失败: {e}{C_RESET}")

    def _send_player_name(self):
        if not self.sock:
            return
        name = self._controller_name()
        if not name or name == "操作者":
            # 早期场景下还没拿到真实操作者（bridge 连上 Godot 但状态机没切到 PLAYING_*）。
            # 仍然要发一个名字让游戏端别拿空字符串签字，"Lumi" 是 1 角色直播的合理默认。
            name = "Lumi"
        self.send_command({"action": "set_player_name", "name": name})

    def receive_messages(self) -> list:
        messages = []
        if not self.sock:
            return messages
        try:
            data = self.sock.recv(4096)
            if not data:
                self.sock = None
                return messages
            self.recv_buffer += data.decode("utf-8")
            while "\n" in self.recv_buffer:
                idx = self.recv_buffer.index("\n")
                line = self.recv_buffer[:idx].strip()
                self.recv_buffer = self.recv_buffer[idx + 1:]
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except socket.timeout:
            pass
        except Exception as e:
            if self.running:
                print(f"{C_RED}[恶魔轮盘] 接收错误: {e}{C_RESET}")
            self.sock = None
        return messages

    def update_state(self, data: dict):
        if self.duel_mode:
            # hotseat: mp_main payload, normalized via the duel protocol
            from .duel_protocol import parse_mp_state
            snap = parse_mp_state(data)
            self._current_duel_slot = snap.get("current_slot")
            self._duel_snapshot = snap
            perspective = self._current_duel_slot if self._current_duel_slot in (1, 3) else 1
            self.state = GameState.from_mp_snapshot(
                snap,
                self._slot_provider,
                perspective_slot=perspective,
            )
            if self._current_duel_slot in self._duel_engines:
                self.engine = self._duel_engines[self._current_duel_slot]
            # Both private ledgers must advance when a shell is consumed, but
            # neither receives the other player's revealed shell information.
            for slot, duel_engine in self._duel_engines.items():
                slot_state = GameState.from_mp_snapshot(
                    snap,
                    self._slot_provider,
                    perspective_slot=slot,
                )
                duel_engine.on_state_update(slot_state)
            return
        data = self._state_lifecycle.normalize(data)
        s = self.state
        s.phase = data.get("phase", "unknown")
        s.health_player = data.get("health_player", 0)
        s.health_opponent = data.get("health_opponent", 0)
        s.max_health = data.get("max_health", 0)
        s.shells_sequence = data.get("shells_sequence", [])
        s.shells_remaining = data.get("shells_remaining", 0)
        s.shells_live_total = data.get("shells_live_total", 0)
        s.shells_blank_total = data.get("shells_blank_total", 0)
        s.current_shell = data.get("current_shell", "unknown")
        s.shotgun_damage = data.get("shotgun_damage", 1)
        s.barrel_sawed_off = data.get("barrel_sawed_off", False)
        s.dealer_cuffed = data.get("dealer_cuffed", False)
        s.player_cuffed = data.get("player_cuffed", False)
        s.player_wire_cut = bool(data.get("player_wire_cut", False))
        s.dealer_wire_cut = bool(data.get("dealer_wire_cut", False))
        s.player_items = data.get("player_items", [])
        s.dealer_items = data.get("dealer_items", [])
        s.round = data.get("round", 0)
        s.batch = data.get("batch", 0)
        s.endless = data.get("endless", False)
        s.shells_ready = bool(data.get("shells_ready", False))
        s.raw_shells_remaining = int(data.get("raw_shells_remaining", 0))

    def _handle_player_turn(self):
        """玩家回合：确定性决策 → 成功则直接执行，失败则投递给 Lumi 快脑"""
        state = self.state
        action_generation = self._action_generation
        action_slot = self._current_duel_slot if self.duel_mode else None
        action_round = int(state.round) if self.duel_mode else None
        if (
            not self._is_active_for_decision()
            or not state.shells_ready
            or state.shells_remaining <= 0
            or self._pending_execution is not None
        ):
            return
        if self.duel_mode:
            actor_name, opponent_name = self._duel_names(action_slot)
            if not actor_name or not opponent_name:
                self.log({
                    "type": "decision_blocked",
                    "reason": "duel_roster_incomplete",
                    "current_slot": action_slot,
                })
                return
        if not self._wait_result_commentary(
            generation=action_generation,
            slot=action_slot,
            round_index=action_round,
        ):
            return
        forced = self._take_forced_fallback(action_slot, action_round)
        if forced is not None:
            active_engine = self._engine_for_slot(action_slot)
            decision = self._timeout_decision(active_engine, state)
            decision = self._validate_decision(decision, state, allow_fallback=True)
            if decision is None:
                self.log({
                    "type": "execution_fallback_blocked",
                    "reason": "no_safe_fallback",
                    **forced,
                })
                return
            self._action_sequence += 1
            decision["action_id"] = f"action-{self._action_sequence}"
            decision["round_index"] = action_round
            decision["_layer"] = "fallback_execution_error"
            decision["reason"] = decision.get("reason") or "上一动作执行失败，改走安全兜底"
            self._apply_public_duel_identity(decision, action_slot)
            self._log_decision("执行失败兜底", decision)
            self.log({"type": "decision", **decision})
            self._record_selected_action(decision)
            self._dispatch_action(decision)
            return
        # 防重复：同一状态只决策一次
        phase_key = (
            f"player_turn_{state.round}_{self._current_duel_slot}_{state.shells_remaining}_"
            f"{state.health_player}_{state.health_opponent}_{len(state.player_items)}"
        )
        if self.last_acted_phase == phase_key:
            return
        self.last_acted_phase = phase_key

        # 单机继续把确定性策略放在模型前面；双 AI 对打不追求某一方胜率，
        # 正常回合一律先交给当前角色模型，旧策略只在模型超时后兜底。
        active_engine = self._engine_for_slot(self._current_duel_slot) if self.duel_mode else self.engine
        self.engine = active_engine
        decision = None if self.duel_mode else active_engine.decide(state)
        if decision:
            actor_now, opponent_now = self._apply_public_duel_identity(
                decision, action_slot
            )
            self._action_sequence += 1
            decision["_layer"] = "deterministic"
            decision["action_id"] = f"action-{self._action_sequence}"
            if action_round is not None:
                decision["round_index"] = action_round
            self._log_decision("确定性策略", decision)
            self.log({"type": "decision", **decision})
            self._record_selected_action(decision)
            # 通知 Lumi 确定性决策（可用于 TTS 说理由）
            self.event_callback("decision_made", decision)
            time.sleep(1.5)
            if not self._action_still_valid(
                action_generation, action_slot, action_round
            ):
                self.log({"type": "action_cancelled", "reason": "state_changed_before_send", **decision})
                return
            self._dispatch_action(decision)
            return

        # 第二层：投递给 Lumi 快脑
        available_items = list(state.player_items)
        if state.health_player >= state.max_health and "cigarettes" in available_items:
            available_items.remove("cigarettes")
        if state.player_wire_cut:
            available_items = [
                item for item in available_items
                if item not in ("cigarettes", "expired medicine")
            ]
        # 决策硬指令放进 intel_text（操控者可见，围观者 staged 解说时不可见）
        intel_text = active_engine.intel_text() or ""
        controller_now = (
            self._slot_provider(self._current_duel_slot)
            if self.duel_mode and self._current_duel_slot in (1, 3)
            else self._controller_name()
        )
        opponent_now = ""
        if self.duel_mode and self._current_duel_slot in (1, 3):
            opponent_slot = 3 if self._current_duel_slot == 1 else 1
            opponent_now = self._slot_provider(opponent_slot)
        tools = build_game_tools(
            available_items,
            state,
            opponent_name=opponent_now,
            duel_mode=self.duel_mode,
        )
        # 构建状态文本。已知情报只能来自道具结果，不能直接读 Godot 的 current_shell。
        if self.duel_mode and self._current_duel_slot is not None:
            # hotseat duel: 双玩家视角局面（操作者/庄家语义不适用）
            llm_state_text = _duel_state_text(
                state, self._current_duel_slot, self._slot_provider,
                known_intel_text=public_known_intel_text(intel_text),
            )
        else:
            llm_state_text = state.format_for_llm(
                controller_name=controller_now,
                known_intel_text=public_known_intel_text(intel_text),
            )
        if intel_text:
            intel_text += "\n"
        intel_text += "必须从可选中文动作中选择一个，并调用对应工具；只说话不调工具是无效的。"

        duel_slot = self._current_duel_slot if self.duel_mode else None
        action_descriptions = {}
        strategy_text = ""
        if self.duel_mode:
            action_choices = build_duel_available_actions(
                available_items,
                list(state.dealer_items or []),
                state=state,
                opponent_name=opponent_now,
            )
            action_descriptions = build_duel_action_descriptions(
                state,
                list(action_choices),
                opponent_name=opponent_now,
            )
            strategy_text = DUEL_COMMON_STRATEGY_TEXT
        request = GameDecisionRequest(
            state=GameState(**{
                f.name: getattr(state, f.name)
                for f in state.__dataclass_fields__.values()
            }),
            state_text=llm_state_text,
            intel_text=intel_text,
            tools=tools,
            controller_name=controller_now,
            game_status=self._game_status,
            last_action_result=self._last_result_for_slot(duel_slot),
            slot=duel_slot or 1,
            role_name=self._slot_provider(duel_slot) if (self.duel_mode and duel_slot is not None) else "",
            opponent_name=opponent_now,
            available_actions=list(action_choices) if self.duel_mode else [],
            action_descriptions=action_descriptions,
            strategy_text=strategy_text,
        )
        self._action_sequence += 1
        request.action_id = f"action-{self._action_sequence}"

        with self._pending_lock:
            self.pending_decision = request

        self.event_callback("need_decision", {
            "state_text": request.state_text,
            "intel_text": request.intel_text,
            "action_id": request.action_id,
            "actor_name": controller_now,
            "opponent_name": opponent_now,
        })

        print(f"{C_CYAN}[恶魔轮盘] 等待 {controller_now} 快脑决策...{C_RESET}")

        # 等待当前行动者快脑返回；状态或环节变化会立即作废这次请求。
        deadline = time.monotonic() + 35.0
        while (
            not request.result_event.is_set()
            and time.monotonic() < deadline
            and self._action_still_valid(
                action_generation, action_slot, action_round
            )
        ):
            request.result_event.wait(timeout=0.25)
        if not self._action_still_valid(
            action_generation, action_slot, action_round
        ):
            request.cancelled = True
            with self._pending_lock:
                if self.pending_decision is request:
                    self.pending_decision = None
            self.log({
                "type": "action_cancelled",
                "reason": "state_or_activity_changed_while_deciding",
                "action_id": request.action_id,
                "actor_name": controller_now,
            })
            return
        if request.result_event.is_set():
            if request.cancelled:
                return
            decision = request.result
            decision["_layer"] = "lumi_fast_brain"
            # 校验+模糊匹配：LLM 经常写错道具名
            decision = self._validate_decision(
                decision,
                state,
                allow_fallback=True,
            )
            if decision is None:
                self.log({
                    "type": "decision_invalid",
                    "reason": "duel_model_choice_failed_validation",
                    "action_id": request.action_id,
                    "actor_name": controller_now,
                })
                with self._pending_lock:
                    if self.pending_decision is request:
                        self.pending_decision = None
                # 仅供显式关闭兜底的诊断调用。正式对打链会立即得到脚本
                # 兜底，不再依赖一个可能永远不会到来的状态更新。
                self.last_acted_phase = ""
                return
            self._apply_public_duel_identity(decision, action_slot)
            if str(decision.get("_layer") or "").startswith("fallback_invalid_"):
                self.log({
                    "type": "decision_invalid",
                    "reason": "duel_model_choice_failed_validation",
                    "fallback_layer": decision.get("_layer"),
                    "action_id": request.action_id,
                    "actor_name": controller_now,
                })
                self._log_decision("校验失败兜底", decision)
            else:
                self._log_decision(f"{controller_now}快脑", decision)
        else:
            # 超时兜底
            print(f"{C_RED}[恶魔轮盘] {controller_now} 快脑超时(35s)，使用兜底策略{C_RESET}")
            decision = self._timeout_decision(active_engine, state)
            decision["_layer"] = "fallback_timeout"
        self._apply_public_duel_identity(decision, action_slot)
        if action_round is not None:
            decision["round_index"] = action_round
        if decision.get("_layer") == "fallback_timeout":
            self._log_decision("超时兜底", decision)

        with self._pending_lock:
            self.pending_decision = None

        decision["action_id"] = request.action_id
        self.log({"type": "decision", **decision})
        self._record_selected_action(decision)
        if not self._wait_tts_done(
            request.output_id,
            generation=action_generation,
            slot=action_slot,
            round_index=action_round,
            completion_event=request.speech_done_event,
        ):
            self.log({"type": "action_cancelled", "reason": "speech_or_activity_invalidated", **decision})
            return
        time.sleep(0.2)
        if not self._action_still_valid(
            action_generation, action_slot, action_round
        ):
            self.log({"type": "action_cancelled", "reason": "state_changed_after_speech", **decision})
            return
        self._dispatch_action(decision)

    # 道具别名映射（LLM 经常写错名字或用中文）
    ITEM_ALIASES = {
        "magnifier": "magnifying glass",
        "magnifying_glass": "magnifying glass",
        "magnifyingglass": "magnifying glass",
        "glass": "magnifying glass",
        "cigs": "cigarettes",
        "cigarette": "cigarettes",
        "cuffs": "handcuffs",
        "handcuff": "handcuffs",
        "saw": "handsaw",
        "medicine": "expired medicine",
        # 中文映射
        "啤酒": "beer",
        "香烟": "cigarettes",
        "烟": "cigarettes",
        "手铐": "handcuffs",
        "手锯": "handsaw",
        "锯子": "handsaw",
        "放大镜": "magnifying glass",
        "过期药": "expired medicine",
        "药": "expired medicine",
        "药物": "expired medicine",
        "手机": "burner phone",
        "电话": "burner phone",
        "逆转器": "inverter",
        "肾上腺素": "adrenaline",
        "jam": "jammer",
        "干扰器": "jammer",
    }

    def _timeout_decision(self, active_engine: DeterministicEngine, state) -> dict:
        if self.duel_mode:
            return active_engine.decide(state) or active_engine.fallback(state)
        return active_engine.fallback(state)

    def _invalid_decision(
        self,
        state,
        *,
        layer: str,
        allow_fallback: bool,
    ) -> dict | None:
        if not allow_fallback:
            return None
        decision = (
            self._timeout_decision(self.engine, state)
            if self.duel_mode
            else self.engine.fallback(state)
        )
        decision["_layer"] = layer
        return decision

    def _validate_decision(
        self,
        decision: dict,
        state,
        *,
        allow_fallback: bool = True,
    ) -> dict | None:
        """校验模型选择；无效选择按调用方要求返回可执行脚本兜底。"""
        if decision.get("action") == "use_buckshot_adrenaline":
            raw_target = str(
                decision.get("目标道具")
                or decision.get("steal_item")
                or ""
            ).strip()
            target_item = raw_target if raw_target in DUEL_STEALABLE_ITEMS else ""
            if raw_target and not target_item:
                structured = command_from_chinese_action(f"肾上腺素：{raw_target}")
                target_item = str((structured or {}).get("steal_item") or "")
            decision = {
                **decision,
                "action": "use_item",
                "item": "adrenaline",
                "steal_item": target_item or raw_target,
            }
        cn_action = decision.get("动作") or decision.get("action_cn")
        mapped = command_from_chinese_action(cn_action) if cn_action else None
        if mapped:
            decision = {**decision, **mapped}

        action = decision.get("action", "")
        if action == "use_item":
            # LLM 有时用 "name" 而不是 "item"
            item = decision.get("item", "") or decision.get("name", "")
            decision["item"] = item  # 统一到 item 字段
            if item not in state.player_items:
                # 模糊匹配
                corrected = self.ITEM_ALIASES.get(item.lower().strip())
                if corrected and corrected in state.player_items:
                    print(f"{C_YELLOW}[恶魔轮盘] 道具名修正: {item} → {corrected}{C_RESET}")
                    decision["item"] = corrected
                    item = corrected
                else:
                    print(f"{C_RED}[恶魔轮盘] 快脑选了不存在的道具 [{item}]，"
                          f"玩家道具: {state.player_items}，回退兜底{C_RESET}")
                    return self._invalid_decision(
                        state,
                        layer="fallback_invalid_item",
                        allow_fallback=allow_fallback,
                    )
            if state.player_wire_cut and item in ("cigarettes", "expired medicine"):
                print(
                    f"{C_RED}[恶魔轮盘] 断线状态禁止无效回血道具 [{item}]，回退兜底{C_RESET}"
                )
                return self._invalid_decision(
                    state,
                    layer="fallback_wire_cut_heal",
                    allow_fallback=allow_fallback,
                )
            if item == "jammer" and state.opponent_jammed:
                print(
                    f"{C_RED}[恶魔轮盘] 对手已经被干扰，禁止重复使用干扰器，回退兜底{C_RESET}"
                )
                return self._invalid_decision(
                    state,
                    layer="fallback_opponent_already_jammed",
                    allow_fallback=allow_fallback,
                )
            if self.duel_mode and item == "adrenaline":
                stolen_item = str(decision.get("steal_item") or "")
                usable_targets = build_duel_adrenaline_targets(
                    state.player_items,
                    state.dealer_items,
                    state=state,
                )
                if (
                    not stolen_item
                    or stolen_item not in DUEL_STEALABLE_ITEMS
                    or stolen_item not in state.dealer_items
                    or stolen_item not in usable_targets
                ):
                    print(
                        f"{C_RED}[恶魔轮盘] 肾上腺素偷取目标无效 "
                        f"[{stolen_item}]，对手道具: {state.dealer_items}{C_RESET}"
                    )
                    return self._invalid_decision(
                        state,
                        layer="fallback_invalid_adrenaline_target",
                        allow_fallback=allow_fallback,
                    )
        elif action == "shoot":
            target = decision.get("target", "")
            if target not in ("self", "dealer"):
                print(f"{C_RED}[恶魔轮盘] 快脑射击目标无效 [{target}]，回退兜底{C_RESET}")
                return self._invalid_decision(
                    state,
                    layer="fallback_invalid_target",
                    allow_fallback=allow_fallback,
                )
        else:
            print(f"{C_RED}[恶魔轮盘] 快脑返回未知动作 [{action}]，回退兜底{C_RESET}")
            return self._invalid_decision(
                state,
                layer="fallback_invalid_action",
                allow_fallback=allow_fallback,
            )
        return decision

    def _record_dispatched_action(self, decision: dict, *, now: float | None = None) -> None:
        """Track a sent duel action until Godot confirms completion or failure."""
        if not self.duel_mode or decision.get("action") not in {"shoot", "use_item"}:
            return
        slot = decision.get("slot", self._current_duel_slot)
        round_index = decision.get("round_index", getattr(self.state, "round", None))
        self._pending_execution = {
            "action_id": str(decision.get("action_id") or ""),
            "action": str(decision.get("action") or ""),
            "item": str(decision.get("item") or ""),
            "slot": int(slot) if slot in (1, 3) else slot,
            "round_index": int(round_index) if round_index is not None else None,
            "decision": dict(decision),
            "sent_at": time.monotonic() if now is None else float(now),
            "cancel_requested": False,
        }

    def _dispatch_action(self, decision: dict) -> None:
        self._record_dispatched_action(decision)
        self.send_command(decision)
        self.event_callback("decision_dispatched", dict(decision))

    def _execution_message_matches(self, msg: dict) -> bool:
        pending = self._pending_execution
        if not pending:
            return False
        action_id = str(msg.get("action_id") or "")
        if action_id and pending.get("action_id"):
            return action_id == pending["action_id"]
        # Backward-compatible bridge payloads had no action_id. There can only
        # be one in-flight duel action, so require its action and slot to match.
        if msg.get("action") and msg.get("action") != pending.get("action"):
            return False
        msg_slot = msg.get("slot", msg.get("from_slot"))
        if msg_slot is not None:
            try:
                if int(msg_slot) != int(pending.get("slot")):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _complete_pending_execution(self, msg: dict) -> None:
        if not self._execution_message_matches(msg):
            return
        pending = self._pending_execution or {}
        self._pending_execution = None
        completed = dict(pending.get("decision") or {})
        completed["execution_result"] = dict(msg)
        self.event_callback("decision_executed", completed)

    def _handle_action_error(self, msg: dict) -> None:
        """Turn a native execution failure into an observable, retryable state."""
        self.log(dict(msg))
        message = str(msg.get("message") or msg.get("reason") or "未知执行错误")
        print(f"{C_RED}[恶魔轮盘·执行失败] {message}{C_RESET}")
        if not self._execution_message_matches(msg):
            return
        pending = self._pending_execution or {}
        self._pending_execution = None
        self.last_acted_phase = ""
        if self.duel_mode and bool(msg.get("retry_safe", False)):
            self._forced_fallback = {
                "slot": pending.get("slot"),
                "round_index": pending.get("round_index"),
                "failed_action_id": pending.get("action_id"),
                "reason": message,
            }
        self.event_callback("action_failed", {
            **dict(msg),
            "failed_decision": dict(pending.get("decision") or {}),
        })

    def _check_pending_execution_timeout(self, *, now: float | None = None) -> None:
        pending = self._pending_execution
        if not pending or pending.get("cancel_requested"):
            return
        current = time.monotonic() if now is None else float(now)
        if current - float(pending.get("sent_at") or current) < self.ACTION_ACK_TIMEOUT_SECONDS:
            return
        pending["cancel_requested"] = True
        self.send_command({
            "action": "cancel_action",
            "action_id": pending.get("action_id", ""),
            "slot": pending.get("slot"),
            "round_index": pending.get("round_index"),
            "reason": "execution_timeout",
        })
        self.log({
            "type": "execution_timeout",
            "action_id": pending.get("action_id", ""),
            "slot": pending.get("slot"),
            "round_index": pending.get("round_index"),
        })
        print(
            f"{C_RED}[恶魔轮盘·执行超时] action_id={pending.get('action_id') or '-'}，"
            f"已请求游戏恢复原生操作状态{C_RESET}"
        )

    def has_stalled_execution(self) -> bool:
        if self._forced_fallback is not None:
            return True
        pending = self._pending_execution
        if not pending:
            return False
        return bool(pending.get("cancel_requested")) or (
            time.monotonic() - float(pending.get("sent_at") or time.monotonic())
            >= self.ACTION_ACK_TIMEOUT_SECONDS
        )

    def _take_forced_fallback(self, slot: int | None, round_index: int | None) -> dict | None:
        forced = self._forced_fallback
        if not forced:
            return None
        if forced.get("slot") != slot or forced.get("round_index") != round_index:
            # The failed lease no longer owns the turn; never replay it into a
            # different player or round.
            self._forced_fallback = None
            return None
        self._forced_fallback = None
        return forced

    def _record_selected_action(self, decision: dict) -> None:
        """A selected command is not a completed game fact.

        Clear stale context now; item and shot results are written only after the
        game confirms their native interaction has completed.
        """
        if self.duel_mode:
            self._set_last_result("", self._current_duel_slot)
        else:
            self._set_last_result("")

    def _handle_action_executed(self, msg: dict) -> None:
        """Promote a native, completed item interaction into grounded context."""
        logged_action = dict(msg)
        self.log(logged_action)
        print(f"{C_GREEN}[游戏] 动作已执行: {msg.get('action')}{C_RESET}")
        self._complete_pending_execution(msg)
        if msg.get("action") != "use_item":
            return
        item = str(msg.get("item") or "")
        if not item:
            return
        if self.duel_mode:
            try:
                slot = int(msg.get("slot"))
            except (TypeError, ValueError):
                self.log({"type": "action_rejected", "reason": "missing_duel_slot", **msg})
                return
            if slot not in self._duel_engines:
                self.log({"type": "action_rejected", "reason": "unknown_duel_slot", **msg})
                return
            private_engine = self._engine_for_slot(slot)
            intel_message = dict(msg)
            stolen_item = str(msg.get("stolen_item") or "")
            if item == "adrenaline" and stolen_item:
                intel_message["item"] = stolen_item
            known_before = private_engine.effective_known
            private_engine.on_action_executed(intel_message)
            known_after = private_engine.effective_known
            role_name = self._slot_provider(slot)
            effective_item = str(intel_message.get("item") or item)
            private_fact = {
                "type": "game_event",
                "event": "player_used_item",
                "item": effective_item,
            }
            if effective_item in {"magnifying glass", "burner phone"}:
                for key in ("revealed_shell", "revealed_position"):
                    if key in msg:
                        private_fact[key] = msg[key]
            private_text = format_game_event(private_fact, controller_name=role_name) or ""
            if (
                effective_item == "inverter"
                and known_before in {"live", "blank"}
                and known_after in {"live", "blank"}
            ):
                private_text = (
                    f"{role_name} 根据此前情报确认，逆转后当前子弹是"
                    f"{'实弹' if known_after == 'live' else '空弹'}。"
                )
            self._set_last_result(private_text, slot)
            actor_private_text = ""
            if (
                effective_item in {"magnifying glass", "burner phone"}
                and shell_to_cn(str(msg.get("revealed_shell") or "unknown")) != "未知"
            ):
                actor_private_text = private_text
            elif effective_item == "inverter" and known_before in {"live", "blank"}:
                actor_private_text = private_text

            # Public result deliberately omits revealed shell details. The user of
            # the item keeps them in their private engine; the opponent sees only
            # that an item was used.
            public_fact = {
                "type": "game_event",
                "event": "item_used",
                "slot": slot,
                "item": item,
            }
            if item == "burner phone":
                public_fact["intel_revealed"] = bool(actor_private_text)
            for key in ("stolen_item", "health_before", "health_after"):
                if key in msg:
                    public_fact[key] = msg[key]
            from .duel_protocol import format_duel_game_event

            public_text = format_duel_game_event(public_fact, self._slot_provider)
            self.log(public_fact)
            if public_text:
                self.event_callback(
                    "game_event",
                    self._build_game_event_payload(
                        "item_used",
                        public_text,
                        public_fact,
                        actor_private_text=actor_private_text,
                    ),
                )
            return

        self.engine.on_action_executed(msg)
        if item == "beer":
            return
        if item in {"cigarettes", "expired medicine"}:
            before = msg.get("health_before")
            after = msg.get("health_after")
            # A changed HP already produced player_healed/player_took_damage.
            # Missing fields mean an older producer, where emitting here could
            # duplicate that later effect event.
            if before is None or after is None or before != after:
                return
        confirmed = {
            "type": "game_event",
            "event": "player_used_item",
            "item": item,
        }
        for key in ("revealed_shell", "revealed_position"):
            if key in msg:
                confirmed[key] = msg[key]
        self.log(confirmed)
        event_text = format_game_event(
            confirmed,
            controller_name=self._controller_name(),
        )
        if event_text:
            self._set_last_result(event_text)
            self.event_callback(
                "game_event",
                {
                    "event": "player_used_item",
                    "text": event_text,
                    "raw": confirmed,
                },
            )

    def _build_game_event_payload(
        self,
        event: str,
        event_text: str,
        raw: dict,
        *,
        actor_private_text: str = "",
    ) -> dict:
        """Attach the authoritative batch/mode active when an event arrived."""

        self._event_sequence += 1
        event_id = f"buckshot-{self._event_sequence}"
        payload = {
            "event": event,
            "event_id": event_id,
            "text": event_text,
            "raw": raw,
            "batch": self.state.batch,
            "endless": self.state.endless,
        }
        if self.duel_mode:
            from .duel_protocol import build_duel_event_metadata

            payload.update(build_duel_event_metadata(raw, self._slot_provider))
            payload.update({
                "game_id": "buckshot_duel",
                "role_mode": "duel",
                "round_index": self.state.round,
            })
            if actor_private_text:
                payload["actor_private_text"] = str(actor_private_text).strip()
            event_kind = {
                "item_used": "item_result",
                "shot_result": "shot_result",
                "player_won": "round_result",
            }.get(event, "")
            if event_kind:
                self._pending_commentary_event = {
                    "event_id": event_id,
                    "event_kind": event_kind,
                    "created_at": time.monotonic(),
                }
        return payload

    def _log_decision(self, layer: str, decision: dict):
        state = self.state
        known = self.engine.effective_known
        known_str = {"live": "实弹", "blank": "空弹"}.get(known, "未知")
        player_label, opponent_label = self._duel_names(self._current_duel_slot)
        print(
            f"\n{C_YELLOW}┌─ 决策 [{layer}] {'─'*30}\n"
            f"│ 情报: 膛内={known_str}\n"
            f"│ 弹匣: {state.live_remaining}实/{state.blank_remaining}空\n"
            f"│ HP: {player_label}{state.health_player}/{state.max_health} vs "
            f"{opponent_label}{state.health_opponent}/{state.max_health}\n"
            f"│ → {decision['action']}"
            f"{' ' + decision.get('target','') if 'target' in decision else ''}"
            f"{' ' + ITEM_NAMES_CN.get(decision.get('item',''), decision.get('item','')) if 'item' in decision else ''}\n"
            f"│ → {decision.get('reason','')}\n"
            f"└{'─'*50}{C_RESET}"
        )

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
                self.log({"type": "pending_cleared", "reason": reason})

    def run(self):
        """主循环 — 在独立线程或独立进程中运行"""
        print(f"{C_CYAN}{'='*50}")
        print(f"  恶魔轮盘 Bridge 已启动")
        print(f"{'='*50}{C_RESET}")

        while self.running:
            # 检查游戏进程是否退出（放在最前面，确保 sock 断开后也能检测到）
            if self.godot_process and self.godot_process.poll() is not None:
                print(f"{C_YELLOW}[恶魔轮盘] 游戏已关闭{C_RESET}")
                self.running = False
                break

            if not self.sock:
                if not self.connect():
                    time.sleep(RECONNECT_INTERVAL)
                    continue

            messages = self.receive_messages()
            if not self.sock:
                continue

            for msg in messages:
                msg_type = msg.get("type", "")

                if msg_type == "game_state":
                    self.update_state(msg)
                    if not self.duel_mode:
                        self.engine.on_state_update(self.state)
                    shell_audit_key = (
                        self.state.batch,
                        self.state.round,
                        self.state.shells_ready,
                        self.state.raw_shells_remaining,
                    )
                    if getattr(self, "_last_shell_audit_key", None) != shell_audit_key:
                        self._last_shell_audit_key = shell_audit_key
                        print(
                            f"{C_CYAN}[恶魔轮盘·弹匣门控] batch={self.state.batch} "
                            f"round={self.state.round} phase={self.state.phase} "
                            f"raw={self.state.raw_shells_remaining} "
                            f"authoritative={self.state.shells_ready}{C_RESET}"
                        )
                    if (
                        self._game_status == "失败"
                        and self.state.health_player > 0
                        and self.state.health_opponent > 0
                        and self.state.phase in ("round_loading", "item_grabbing", "player_turn", "dealer_turn", "waiting")
                    ):
                        self._game_status = "进行中"
                    state_log = {
                        "type": "state", "phase": self.state.phase,
                        "round": self.state.round,
                        "batch": self.state.batch,
                        "endless": self.state.endless,
                        "hp": self.state.health_player, "hp_opp": self.state.health_opponent,
                        "hp_max": self.state.max_health,
                        "shells": self.state.shells_remaining,
                        "live": self.state.live_remaining, "blank": self.state.blank_remaining,
                        "shells_ready": self.state.shells_ready,
                        "raw_shells_remaining": self.state.raw_shells_remaining,
                        "player_items": list(self.state.player_items),
                        "dealer_items": list(self.state.dealer_items),
                        "game_status": self._game_status,
                        # controller 不进 payload —— 让 lumi.py 端的 game_state 订阅自己向 director 取真值
                        "last_action_result": self._last_result_for_slot(self._current_duel_slot),
                        # 放大镜 / 手机获得的情报，以及基于既有情报推导出的逆转结果，供解说提示词使用。
                        # round_start/victory/defeat 时引擎已清空，这里读到的就是"未知"。
                        "known_intel": public_known_intel_text(
                            self._engine_for_slot(self._current_duel_slot).intel_text()
                            if self.duel_mode and self._current_duel_slot in (1, 3)
                            else self.engine.intel_text()
                        ),
                        "player_cuffed": self.state.player_cuffed,
                        "dealer_cuffed": self.state.dealer_cuffed,
                        "player_jammed": self.state.player_jammed,
                        "opponent_jammed": self.state.opponent_jammed,
                        "barrel_sawed_off": self.state.barrel_sawed_off,
                        "shotgun_damage": self.state.shotgun_damage,
                        "player_wire_cut": self.state.player_wire_cut,
                        "dealer_wire_cut": self.state.dealer_wire_cut,
                    }
                    if self.duel_mode:
                        actor_name, opponent_name = self._duel_names(self._current_duel_slot)
                        state_log.update({
                            "game_id": "buckshot_duel",
                            "role_mode": "duel",
                            "current_slot": self._current_duel_slot,
                            "actor_name": actor_name if self._current_duel_slot in (1, 3) else "",
                            "opponent_name": opponent_name if self._current_duel_slot in (1, 3) else "",
                            "players": [self._slot_provider(1), self._slot_provider(3)],
                        })
                    self.log(state_log)
                    if self.duel_mode:
                        actor_label = state_log.get("actor_name") or "等待行动者"
                        opponent_label = state_log.get("opponent_name") or "另一位玩家"
                        print(self.state.summary(
                            player_label=actor_label,
                            opponent_label=opponent_label,
                        ))
                    else:
                        print(self.state.summary())
                    self.event_callback("game_state", state_log)

                    # 收到有效血量说明对局已开始
                    if (
                        not self.in_round
                        and self.state.health_player > 0
                        and not (self.duel_mode and self._duel_round_finished)
                    ):
                        self.in_round = True

                    # hotseat duel: both slots are AI players, so dealer_turn (slot 3)
                    # must also trigger decision flow
                    if self.duel_mode and self.state.phase in ("player_turn", "dealer_turn"):
                        self._handle_player_turn()
                    elif self.state.phase == "player_turn":
                        self._handle_player_turn()

                elif msg_type == "game_event":
                    event = msg.get("event", "")
                    self.log(msg)
                    accepted, reject_reason = self._event_causality.validate_event(
                        msg,
                        phase=self.state.phase,
                    )
                    if not accepted:
                        rejected = {
                            "type": "event_rejected",
                            "event": event,
                            "reason": reject_reason,
                            "phase": self.state.phase,
                        }
                        self.log(rejected)
                        print(
                            f"{C_YELLOW}[恶魔轮盘·事件拒绝] event={event or '-'} "
                            f"phase={self.state.phase} reason={reject_reason}{C_RESET}"
                        )
                        continue
                    if event == "shot_result":
                        self._complete_pending_execution(msg)
                    if event == "starting_game":
                        self._reset_for_new_game(msg)
                    self._restore_window_for_game_event(msg)
                    self._stage_duel_restart(msg)
                    # 格式化并回调
                    if self.duel_mode:
                        from .duel_protocol import format_duel_game_event

                        event_text = format_duel_game_event(msg, self._slot_provider)
                    else:
                        event_text = format_game_event(msg, controller_name=self._controller_name())
                    if event_text:
                        # Freeze the authoritative mode/batch at event receipt.
                        # The final batch_won arrives before the native ending UI.
                        self.event_callback(
                            "game_event",
                            self._build_game_event_payload(event, event_text, msg),
                        )
                        # 积累战斗事件（新回合清空，避免无限增长）
                        if event in ("round_start", "round_started"):
                            self._recent_events.clear()
                            if self.duel_mode:
                                self._duel_last_action_results = {1: "", 3: ""}
                            else:
                                self._last_action_result = ""
                            self._game_status = "进行中"
                            if self.duel_mode:
                                self._duel_round_finished = False
                                self.in_round = True
                        self._recent_events.append(event_text)
                        if self.duel_mode and event in ("shot_result", "player_won", "round_complete"):
                            self._set_public_duel_result(event_text)
                        elif event.startswith(("player_", "dealer_")) or event in ("victory", "defeat", "batch_won"):
                            self._set_last_result(event_text)
                        # 保留最近 10 条
                        if len(self._recent_events) > 10:
                            self._recent_events = self._recent_events[-10:]

                    if event in ("round_start", "round_started", "player_healed", "dealer_healed") and self.state.health_player > 0:
                        self._game_status = "进行中"

                    # 大回合切换 / 整局结束后，把放大镜/手机看到的膛内情报清掉，
                    # 避免新弹匣装填好之后角色还在念叨上一弹匣的子弹信息。
                    # 注意：player_turn_start / dealer_turn_start 是同一弹匣内双方轮流开枪的小回合，
                    # 那种切换情报仍然合法（看了下一发的人接着拿放大镜结果做决策），不能在那里清。
                    # round_start 才是弹匣打空 → 庄家血量重置 → 摸道具 → 重新装填好的新一大轮起点。
                    if event in ("round_start", "round_started", "victory", "defeat"):
                        engines = self._duel_engines.values() if self.duel_mode else (self.engine,)
                        for engine in engines:
                            engine.known_shell = None
                            engine.known_positions.clear()

                    if event == "victory":
                        self._game_status = "胜利"
                        self.in_round = False
                        self.wins += 1
                        print(f"\n{C_GREEN}{C_BOLD}  胜利！{self.wins}胜/{self.losses}负{C_RESET}")
                    elif event == "defeat":
                        self._game_status = "失败"
                        self.in_round = False
                        self.losses += 1
                        print(f"\n{C_RED}{C_BOLD}  败北... {self.wins}胜/{self.losses}负{C_RESET}")
                    elif self.duel_mode and event == "player_won":
                        self._mark_duel_round_finished(msg)
                    elif event in ("scene_changed", "death_recovery_main", "batch_won",
                                   "waiver_signed", "briefcase_opened"):
                        print(f"{C_CYAN}[事件] {event}{C_RESET}")
                    elif event.startswith("dealer_") or event.startswith("player_"):
                        print(f"{C_YELLOW}[战斗] {event_text or event}{C_RESET}")

                elif msg_type == "action_executed":
                    self._handle_action_executed(msg)

                elif msg_type in ("action_error", "action_cancelled", "error"):
                    self._handle_action_error(msg)

                elif msg_type == "connected":
                    print(f"{C_GREEN}[游戏] {msg.get('message')}{C_RESET}")
                    self.event_callback("game_event", {
                        "event": "connected",
                        "text": msg.get("message", "BridgeMod ready"),
                        "raw": msg,
                    })

            self._check_pending_execution_timeout()
            self._try_restart_duel()
            time.sleep(0.05)

    def stop(self):
        self.clear_pending_decision("stop")
        self._activation_event.clear()
        self.running = False
        if self.sock:
            self.sock.close()
        if self.godot_process and self.godot_process.poll() is None:
            self.godot_process.terminate()
        self.save_logs()


# ==================== 独立运行入口（调试用）====================

def main():
    """独立运行：不连接 Lumi，LLM 决策用兜底策略替代"""
    bridge = BuckshotBridge()
    bridge.launch_godot()
    print("[Bridge] 等待游戏启动...")
    time.sleep(5)
    try:
        bridge.run()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
        print(f"\n{C_YELLOW}  恶魔轮盘 Bridge 已停止")
        print(f"  总计 {bridge.wins}胜/{bridge.losses}负{C_RESET}")


if __name__ == "__main__":
    main()
