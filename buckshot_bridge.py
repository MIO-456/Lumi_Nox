"""
恶魔轮盘游戏桥接模块 - 作为 Lumi 子模块运行
TCP 连接 Godot BridgeMod，确定性决策就地执行，不确定局面投递给 Lumi 快脑

可独立运行（调试用）：python buckshot_bridge.py
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

from buckshot_prompt_context import (
    build_available_actions,
    command_from_chinese_action,
    item_to_cn,
    items_to_cn_text,
    shell_to_cn,
)

# ==================== 常量 ====================

HOST = "127.0.0.1"
PORT = 9876
RECONNECT_INTERVAL = 3

ITEM_NAMES_CN = {
    "handsaw": "手锯", "magnifying glass": "放大镜", "beer": "啤酒",
    "cigarettes": "香烟", "handcuffs": "手铐", "expired medicine": "过期药",
    "burner phone": "一次性手机", "adrenaline": "肾上腺素", "inverter": "逆转器",
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
}

def items_to_cn(items: list) -> list:
    return [ITEM_NAMES_CN.get(i, i) for i in items]

# Godot 路径配置
GODOT_EXE = os.path.join(os.path.dirname(__file__),
    "Godot_v4.1.1-stable_win64.exe", "Godot_v4.1.1-stable_win64_console.exe")
GODOT_PROJECT = os.path.join(os.path.dirname(__file__), "buckshot_decompiled")

# ANSI 颜色
C_RESET = "\033[0m"
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_MAGENTA = "\033[95m"
C_BOLD = "\033[1m"


# ==================== LLM 工具定义（给 Lumi 快脑用）====================

def build_game_tools(player_items: list, state=None) -> list:
    """根据玩家当前道具+游戏状态动态构建工具列表"""
    unique_items = list(dict.fromkeys(player_items or []))
    if state:
        if state.dealer_cuffed and "handcuffs" in unique_items:
            unique_items.remove("handcuffs")
        if state.barrel_sawed_off and "handsaw" in unique_items:
            unique_items.remove("handsaw")
    available_actions = build_available_actions(unique_items)
    return [{
        "type": "function",
        "function": {
            "name": "choose_buckshot_action",
            "description": "从可选动作中选择一个当前要执行的动作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "动作": {
                        "type": "string",
                        "enum": available_actions,
                        "description": "只能选择列表中的中文动作",
                    },
                },
                "required": ["动作"],
            },
        },
    }]


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
    player_items: list = field(default_factory=list)
    dealer_items: list = field(default_factory=list)
    round: int = 0
    batch: int = 0
    endless: bool = False

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
            f"弹匣剩余：{total}发（{live}发实弹，{blank}发空弹）\n"
            f"已知情报：{known_intel_text or '当前膛内未知'}\n"
            f"枪管已锯短：{'是，本次射击伤害翻倍' if self.barrel_sawed_off else '否'}\n"
            f"庄家被铐住：{'是，庄家下回合跳过' if self.dealer_cuffed else '否'}\n"
            f"{controller_name} 的道具：{items_cn}\n"
            f"庄家的道具：{dealer_items_cn}"
        )

    def summary(self) -> str:
        """终端打印用的彩色摘要"""
        hp_bar_p = "♥" * self.health_player + "♡" * (self.max_health - self.health_player)
        hp_bar_d = "♥" * self.health_opponent + "♡" * (self.max_health - self.health_opponent)
        shells_vis = ""
        for s in self.shells_sequence:
            shells_vis += f"{C_RED}●{C_RESET}" if s == "live" else f"{C_GREEN}○{C_RESET}"
        lines = [
            f"\n{'='*50}",
            f"  阶段: {C_BOLD}{self.phase}{C_RESET}  |  回合 {self.round}  |  Batch {self.batch}",
            f"  玩家 HP: {C_GREEN}{hp_bar_p}{C_RESET}  ({self.health_player}/{self.max_health})",
            f"  庄家 HP: {C_RED}{hp_bar_d}{C_RESET}  ({self.health_opponent}/{self.max_health})",
            f"  弹壳序列: [{shells_vis}]  ({self.live_remaining}实弹/{self.blank_remaining}空弹)",
            f"  当前弹: {C_BOLD}{self.current_shell}{C_RESET}  |  伤害: {self.shotgun_damage}",
            f"  玩家道具: {items_to_cn(self.player_items)}",
            f"  庄家道具: {items_to_cn(self.dealer_items)}",
            f"  手铐: 庄家={self.dealer_cuffed} 玩家={self.player_cuffed}",
            f"{'='*50}",
        ]
        return "\n".join(lines)


# ==================== 游戏决策请求（投递给 Lumi 快脑）====================

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
        elif item == "inverter" and revealed:
            self.known_shell = revealed
            print(f"{C_CYAN}[情报] 逆转器 → 反转后当前弹壳是【{'实弹' if revealed == 'live' else '空弹'}】{C_RESET}")

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
                if "cigarettes" in items and state.health_player < state.max_health:
                    return {"action": "use_item", "item": "cigarettes", "reason": "最后一发空弹，先抽烟回血"}
                return {"action": "shoot", "target": "self", "reason": "最后一发空弹，射自己白嫖~"}

        # 全是空弹
        if state.live_remaining == 0 and state.blank_remaining > 0:
            if "cigarettes" in items and state.health_player < state.max_health:
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
            if "cigarettes" in items and state.health_player < state.max_health:
                return {"action": "use_item", "item": "cigarettes", "reason": "反正是空弹，先抽根烟回血"}
            return {"action": "shoot", "target": "self", "reason": "确定是空弹，射自己白嫖一回合~"}

        return None  # 不确定，需要 LLM

    def fallback(self, state: GameState) -> dict:
        """LLM 超时兜底策略"""
        items = state.player_items
        p_live = state.live_probability
        if "cigarettes" in items and state.health_player < state.max_health and state.health_player <= 2:
            return {"action": "use_item", "item": "cigarettes", "reason": "兜底：血量危险，先抽烟回血"}
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
    mapping = {
        # 场景过渡事件（加载阶段）
        "auto_intro_start": lambda m: "游戏开始加载，准备进入地下室……",
        "kicking_door_1": lambda m: "踹开了第一扇门！",
        "kicking_door_2": lambda m: "踹开第二扇门，进入地下室！",
        "waiver_signed": lambda m: "签了免责书，正式开局！",
        # 战斗事件
        "dealer_shot_player": lambda m: f"庄家射击了{controller_name}，扣{m.get('damage',1)}点血量（剩余{m.get('hp_remaining',0)}点血量）",
        "player_shot_dealer": lambda m: f"{controller_name} 射击庄家，是实弹，庄家扣{m.get('damage',1)}点血量。",
        "player_shot_dealer_blank": lambda m: f"{controller_name} 射击庄家，是空弹，没有造成伤害。",
        "dealer_shot_self": lambda m: f"庄家自射实弹翻车，扣{m.get('damage',1)}点血量",
        "dealer_shot_self_blank": lambda m: "庄家自射空弹，白嫖一回合",
        "dealer_shot_player_blank": lambda m: f"庄家射击{controller_name}，是空弹，没有造成伤害。",
        "player_shot_self_blank": lambda m: f"{controller_name} 射击自己，是空弹，没有扣血，并继续行动。",
        "player_took_damage": lambda m: f"{controller_name} 受伤{m.get('damage',1)}点血量。",
        "player_healed": lambda m: f"{controller_name} 回复{m.get('amount',1)}点血量（当前{m.get('hp_now',0)}点血量）",
        "dealer_healed": lambda m: f"庄家回复{m.get('amount',1)}点血量",
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
    """恶魔轮盘桥接器 —— 可作为 Lumi 子线程或独立运行"""

    def __init__(self, event_callback=None, bus=None, controller_provider=None):
        """
        event_callback: 可选回调函数 (event_type: str, data: dict) -> None
            event_type: "game_event" | "game_state" | "need_decision" | "decision_executed"
        bus: 可选事件总线实例，传入后优先用总线通信
        controller_provider: 可选 () -> str 回调，每次 bridge 需要 controller 名字时即时拉真值。
            **不在 bridge 缓存** —— 避免缓存值与导演真值（schedule + state machine）漂移
            导致整局 role 错乱（2026-05-17 调试结论）。
            为空时退化成 "操作者" 占位，下游用占位再走自己的兜底链。
        """
        self._bus = bus
        self.sock = None
        self.state = GameState()
        self.engine = DeterministicEngine()
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
        self._game_status = "进行中"

        self._activation_event = threading.Event()
        self._last_global_state = ""
        if self._bus:
            self._bus.subscribe("state_changed", self._on_state_changed)
            self._bus.subscribe("game_role_context_changed", self._on_game_role_context_changed)
        else:
            self._activation_event.set()

    def _controller_name(self) -> str:
        """即时拉当前操作者名字。永远走 provider，不缓存。"""
        try:
            name = (self._controller_provider() or "").strip()
        except Exception:
            name = ""
        return name or "操作者"

    def _publish_to_bus(self, event_type: str, data: dict):
        """把桥接器事件转发到总线"""
        self._bus.publish(event_type, data, source="buckshot_roulette")

    def _on_state_changed(self, event):
        new_state = str(event.data.get("new") or "")
        self._last_global_state = new_state
        if new_state == self.EXPECTED_GLOBAL_STATE:
            self._activation_event.set()
            self.log({"type": "state_gate_open", "state": new_state})
            print(f"{C_CYAN}[恶魔轮盘][状态门控] 已进入 {new_state}，允许决策{C_RESET}")
        else:
            self._activation_event.clear()

    def _on_game_role_context_changed(self, event):
        """收到角色上下文变更通知时，重新把当前操作者名字下发给 Godot。
        不在本地缓存 controller —— bridge 用到时都从 provider 拉真值。
        """
        data = event.data or {}
        if data.get("game_id") == "buckshot_roulette":
            self._send_player_name()

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

    def connect(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((HOST, PORT))
            self.sock.settimeout(0.1)
            print(f"{C_GREEN}[恶魔轮盘] 已连接到游戏 {HOST}:{PORT}{C_RESET}")
            self.send_command({"action": "set_auto_mode", "enabled": True})
            self._send_player_name()
            return True
        except Exception as e:
            print(f"{C_RED}[恶魔轮盘] 连接失败: {e}{C_RESET}")
            self.sock = None
            return False

    def send_command(self, cmd: dict):
        if not self.sock:
            return
        try:
            data = json.dumps(cmd) + "\n"
            self.sock.sendall(data.encode("utf-8"))
            reason = cmd.get("reason", "")
            item_cn = ITEM_NAMES_CN.get(cmd.get("item", ""), cmd.get("item", "")) if "item" in cmd else ""
            target_cn = {"dealer": "庄家", "self": "自己"}.get(cmd.get("target", ""), cmd.get("target", "")) if "target" in cmd else ""
            print(f"{C_MAGENTA}[恶魔轮盘] >>> {cmd['action']}"
                  f"{' → ' + target_cn if target_cn else ''}"
                  f"{' → ' + item_cn if item_cn else ''}"
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
            print(f"{C_RED}[恶魔轮盘] 接收错误: {e}{C_RESET}")
            self.sock = None
        return messages

    def update_state(self, data: dict):
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
        s.player_items = data.get("player_items", [])
        s.dealer_items = data.get("dealer_items", [])
        s.round = data.get("round", 0)
        s.batch = data.get("batch", 0)
        s.endless = data.get("endless", False)

    def _handle_player_turn(self):
        """玩家回合：确定性决策 → 成功则直接执行，失败则投递给 Lumi 快脑"""
        state = self.state
        if not self._is_active_for_decision():
            return
        # 防重复：同一状态只决策一次
        phase_key = f"player_turn_{state.shells_remaining}_{state.health_player}_{state.health_opponent}_{len(state.player_items)}"
        if self.last_acted_phase == phase_key:
            return
        self.last_acted_phase = phase_key

        # 第一层：确定性策略
        decision = self.engine.decide(state)
        if decision:
            decision["_layer"] = "deterministic"
            self._log_decision("确定性策略", decision)
            self.log({"type": "decision", **decision})
            # 把确定性动作写进上一动作（射击事件会被后续 game_event 覆盖；道具使用没 game_event，靠这里）
            self._last_action_result = _format_deterministic_action(decision, self._controller_name())
            # 通知 Lumi 确定性决策（可用于 TTS 说理由）
            self.event_callback("decision_made", decision)
            time.sleep(1.5)
            self.send_command(decision)
            return

        # 第二层：投递给 Lumi 快脑
        available_items = list(state.player_items)
        if state.health_player >= state.max_health and "cigarettes" in available_items:
            available_items.remove("cigarettes")
        tools = build_game_tools(available_items, state)

        # 决策硬指令放进 intel_text（操控者可见，围观者 staged 解说时不可见）
        intel_text = self.engine.intel_text() or ""
        controller_now = self._controller_name()
        # 构建状态文本。已知情报只能来自道具结果，不能直接读 Godot 的 current_shell。
        llm_state_text = state.format_for_llm(
            controller_name=controller_now,
            known_intel_text=public_known_intel_text(intel_text),
        )
        if intel_text:
            intel_text += "\n"
        intel_text += "必须从可选中文动作中选择一个，并调用对应工具；只说话不调工具是无效的。"

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
            last_action_result=self._last_action_result,
        )

        with self._pending_lock:
            self.pending_decision = request

        self.event_callback("need_decision", {
            "state_text": request.state_text,
            "intel_text": request.intel_text,
        })

        print(f"{C_CYAN}[恶魔轮盘] 等待 Lumi 快脑决策...{C_RESET}")

        # 等待 Lumi 快脑返回结果（最长 20 秒）
        if request.result_event.wait(timeout=35.0):
            if request.cancelled:
                return
            decision = request.result
            decision["_layer"] = "lumi_fast_brain"
            # 校验+模糊匹配：LLM 经常写错道具名
            decision = self._validate_decision(decision, state)
            self._log_decision("Lumi快脑", decision)
        else:
            # 超时兜底
            print(f"{C_RED}[恶魔轮盘] Lumi 快脑超时(20s)，使用兜底策略{C_RESET}")
            decision = self.engine.fallback(state)
            decision["_layer"] = "fallback_timeout"
            self._log_decision("超时兜底", decision)

        with self._pending_lock:
            self.pending_decision = None

        self.log({"type": "decision", **decision})
        time.sleep(1.0)
        self.send_command(decision)
        self.event_callback("decision_executed", decision)

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
    }

    def _validate_decision(self, decision: dict, state) -> dict:
        """校验 LLM 决策：模糊匹配道具名，无效则回退兜底"""
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
                else:
                    print(f"{C_RED}[恶魔轮盘] 快脑选了不存在的道具 [{item}]，"
                          f"玩家道具: {state.player_items}，回退兜底{C_RESET}")
                    decision = self.engine.fallback(state)
                    decision["_layer"] = "fallback_invalid_item"
        elif action == "shoot":
            target = decision.get("target", "")
            if target not in ("self", "dealer"):
                print(f"{C_RED}[恶魔轮盘] 快脑射击目标无效 [{target}]，回退兜底{C_RESET}")
                decision = self.engine.fallback(state)
                decision["_layer"] = "fallback_invalid_target"
        else:
            print(f"{C_RED}[恶魔轮盘] 快脑返回未知动作 [{action}]，回退兜底{C_RESET}")
            decision = self.engine.fallback(state)
            decision["_layer"] = "fallback_invalid_action"
        return decision

    def _log_decision(self, layer: str, decision: dict):
        state = self.state
        known = self.engine.effective_known
        known_str = {"live": "实弹", "blank": "空弹"}.get(known, "未知")
        print(
            f"\n{C_YELLOW}┌─ 决策 [{layer}] {'─'*30}\n"
            f"│ 情报: 膛内={known_str}\n"
            f"│ 弹匣: {state.live_remaining}实/{state.blank_remaining}空\n"
            f"│ HP: 玩家{state.health_player}/{state.max_health} vs 庄家{state.health_opponent}/{state.max_health}\n"
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
                    self.engine.on_state_update(self.state)
                    if (
                        self._game_status == "失败"
                        and self.state.health_player > 0
                        and self.state.health_opponent > 0
                        and self.state.phase in ("round_loading", "item_grabbing", "player_turn", "dealer_turn", "waiting")
                    ):
                        self._game_status = "进行中"
                    state_log = {
                        "type": "state", "phase": self.state.phase,
                        "hp": self.state.health_player, "hp_opp": self.state.health_opponent,
                        "hp_max": self.state.max_health,
                        "shells": self.state.shells_remaining,
                        "live": self.state.live_remaining, "blank": self.state.blank_remaining,
                        "player_items": list(self.state.player_items),
                        "dealer_items": list(self.state.dealer_items),
                        "game_status": self._game_status,
                        # controller 不进 payload —— 让 lumi.py 端的 game_state 订阅自己向 director 取真值
                        "last_action_result": self._last_action_result,
                        # 放大镜 / 手机 / 逆转器看到的真实膛内情报（脱敏后的中文），供解说提示词使用。
                        # round_start/victory/defeat 时引擎已清空，这里读到的就是"未知"。
                        "known_intel": public_known_intel_text(self.engine.intel_text()),
                        "player_cuffed": self.state.player_cuffed,
                        "dealer_cuffed": self.state.dealer_cuffed,
                    }
                    self.log(state_log)
                    print(self.state.summary())
                    self.event_callback("game_state", state_log)

                    # 收到有效血量说明对局已开始
                    if not self.in_round and self.state.health_player > 0:
                        self.in_round = True

                    if self.state.phase == "player_turn":
                        self._handle_player_turn()

                elif msg_type == "game_event":
                    event = msg.get("event", "")
                    self.log(msg)
                    # 格式化并回调
                    event_text = format_game_event(msg, controller_name=self._controller_name())
                    if event_text:
                        self.event_callback("game_event", {"event": event, "text": event_text, "raw": msg})
                        # 积累战斗事件（新回合清空，避免无限增长）
                        if event == "round_start":
                            self._recent_events.clear()
                            self._last_action_result = ""
                            self._game_status = "进行中"
                        self._recent_events.append(event_text)
                        if event.startswith(("player_", "dealer_")) or event in ("victory", "defeat", "batch_won"):
                            self._last_action_result = event_text
                        # 保留最近 10 条
                        if len(self._recent_events) > 10:
                            self._recent_events = self._recent_events[-10:]

                    if event in ("round_start", "player_healed", "dealer_healed") and self.state.health_player > 0:
                        self._game_status = "进行中"

                    # 大回合切换 / 整局结束后，把放大镜/手机看到的膛内情报清掉，
                    # 避免新弹匣装填好之后角色还在念叨上一弹匣的子弹信息。
                    # 注意：player_turn_start / dealer_turn_start 是同一弹匣内双方轮流开枪的小回合，
                    # 那种切换情报仍然合法（看了下一发的人接着拿放大镜结果做决策），不能在那里清。
                    # round_start 才是弹匣打空 → 庄家血量重置 → 摸道具 → 重新装填好的新一大轮起点。
                    if event in ("round_start", "victory", "defeat"):
                        self.engine.known_shell = None
                        self.engine.known_positions.clear()

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
                    elif event in ("scene_changed", "death_recovery_main", "batch_won",
                                   "waiver_signed", "briefcase_opened"):
                        print(f"{C_CYAN}[事件] {event}{C_RESET}")
                    elif event.startswith("dealer_") or event.startswith("player_"):
                        print(f"{C_YELLOW}[战斗] {event_text or event}{C_RESET}")

                elif msg_type == "action_executed":
                    print(f"{C_GREEN}[游戏] 动作已执行: {msg.get('action')}{C_RESET}")
                    if msg.get("action") == "use_item":
                        self.engine.on_action_executed(msg)

                elif msg_type == "connected":
                    print(f"{C_GREEN}[游戏] {msg.get('message')}{C_RESET}")
                    self.event_callback("game_event", {
                        "event": "connected",
                        "text": msg.get("message", "BridgeMod ready"),
                        "raw": msg,
                    })

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
