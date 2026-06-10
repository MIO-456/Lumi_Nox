"""
恶魔轮盘 AI 决策引擎 - Buckshot Roulette Bot
通过 TCP 连接游戏内的 BridgeMod，接收状态，发送动作指令

用法:
    python buckshot_bot.py                # 默认：混合引擎（无透视 + 代码策略 + LLM 兜底）
    python buckshot_bot.py --legacy       # 旧版：透视 + 确定性引擎（不调用 LLM）
    python buckshot_bot.py --watch        # 仅观察模式（不操作）
    python buckshot_bot.py --no-auto      # 禁用全自动化（需手动启动游戏，只保留战斗AI）
"""

import socket
import json
import time
import sys
import threading
import subprocess
import os
from datetime import datetime
from dataclasses import dataclass, field

# ==================== 配置 ====================

HOST = "127.0.0.1"
PORT = 9876
RECONNECT_INTERVAL = 3  # 重连间隔秒数

# 道具中英文对照
ITEM_NAMES_CN = {
    "handsaw": "手锯",
    "magnifying glass": "放大镜",
    "beer": "啤酒",
    "cigarettes": "香烟",
    "handcuffs": "手铐",
    "expired medicine": "过期药",
    "burner phone": "一次性手机",
    "adrenaline": "肾上腺素",
    "inverter": "逆转器",
}

def items_to_cn(items: list) -> list:
    """将道具列表翻译为中文"""
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


# ==================== 游戏状态 ====================

@dataclass
class GameState:
    phase: str = "unknown"
    health_player: int = 0
    health_opponent: int = 0
    max_health: int = 0
    shells_sequence: list = field(default_factory=list)  # 完整弹壳序列（作弊信息）
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

    def summary(self) -> str:
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


# ==================== 确定性决策引擎 ====================

class DecisionEngine:
    """基于概率和规则的确定性决策引擎"""

    def decide(self, state: GameState) -> dict:
        """返回动作指令 dict"""
        # 阶段 1：道具使用决策
        item_action = self._decide_item(state)
        if item_action:
            return item_action

        # 阶段 2：射击决策
        return self._decide_shoot(state)

    def _decide_item(self, state: GameState) -> dict | None:
        items = state.player_items.copy()
        # Bot 全知模式：当前弹壳类型已知（后续接入 Lumi 时改为不知道）
        known_shell = state.current_shell if state.current_shell in ("live", "blank") else None

        # 放大镜 —— Bot 全知模式下不需要，跳过
        # 电话 —— Bot 全知模式下不需要，跳过

        # 逆转器 —— 空弹反转为实弹，然后射庄家
        if "inverter" in items and known_shell == "blank":
            return {"action": "use_item", "item": "inverter", "reason": "空弹在膛，反转为实弹再射庄家"}

        # 手铐 —— 已知实弹且庄家没被铐，铐住再打
        if "handcuffs" in items and not state.dealer_cuffed and state.shells_remaining > 1:
            if known_shell == "live":
                return {"action": "use_item", "item": "handcuffs", "reason": "实弹在膛，铐住庄家再打"}

        # 手锯 —— 已知实弹时使用，伤害翻倍
        if "handsaw" in items and not state.barrel_sawed_off:
            if known_shell == "live":
                return {"action": "use_item", "item": "handsaw", "reason": "实弹在膛，锯短枪管伤害翻倍"}

        # 香烟 —— 血量不满且当前是空弹（空弹回合不浪费时间）或血量危急时回血
        if "cigarettes" in items and state.health_player < state.max_health:
            if known_shell == "blank" or state.health_player <= 1:
                return {"action": "use_item", "item": "cigarettes", "reason": "回复1点血量"}

        # 啤酒 —— 已知实弹时退掉（避免自己吃实弹或给庄家留实弹）
        if "beer" in items and state.shells_remaining > 1:
            if known_shell == "live" and state.live_remaining > 1:
                # 实弹太多，退一发减少风险
                pass  # 不退，直接射庄家更好
            elif known_shell == "blank" and state.shells_remaining > 1:
                # 空弹射自己白嫖更好，不浪费啤酒
                pass

        # 过期药 —— 血量 <= 2 且没有香烟时赌一把
        if "expired medicine" in items and state.health_player <= 2 and state.health_player > 1:
            if "cigarettes" not in items:
                return {"action": "use_item", "item": "expired medicine", "reason": "血量低，赌回血"}

        # 肾上腺素 —— 偷庄家的好道具
        if "adrenaline" in items:
            steal_priority = ["magnifying glass", "handsaw", "handcuffs", "inverter"]
            for target_item in steal_priority:
                if target_item in state.dealer_items:
                    cn_name = ITEM_NAMES_CN.get(target_item, target_item)
                    return {"action": "use_item", "item": "adrenaline",
                            "reason": f"偷庄家的{cn_name}"}

        return None

    def _decide_shoot(self, state: GameState) -> dict:
        # 已知弹壳类型
        if state.shells_remaining == 1:
            shell = state.shells_sequence[0]
            if shell == "live":
                return {"action": "shoot", "target": "dealer", "reason": "最后一发实弹，射庄家"}
            else:
                return {"action": "shoot", "target": "self", "reason": "最后一发空弹，射自己白嫖回合"}

        known = state.current_shell
        if known == "live":
            return {"action": "shoot", "target": "dealer", "reason": "已知实弹，射庄家"}
        if known == "blank":
            return {"action": "shoot", "target": "self", "reason": "已知空弹，射自己白嫖回合"}

        # 概率决策
        p_live = state.live_probability
        if p_live > 0.5:
            return {"action": "shoot", "target": "dealer",
                    "reason": f"实弹概率 {p_live:.0%}，射庄家"}
        elif p_live < 0.5:
            return {"action": "shoot", "target": "self",
                    "reason": f"空弹概率 {1-p_live:.0%}，射自己白嫖回合"}
        else:
            # 50/50 —— 射庄家更安全（空弹射自己虽然白嫖但实弹自伤）
            # 如果有手铐已生效，射庄家更值
            if state.dealer_cuffed:
                return {"action": "shoot", "target": "dealer",
                        "reason": "50/50 但庄家被铐，射庄家"}
            return {"action": "shoot", "target": "dealer",
                    "reason": "50/50，保守射庄家"}


# ==================== 混合决策引擎（代码策略 + LLM 兜底）====================

# LLM 工具定义 — shoot 是静态的，use_item 由 _build_tools() 动态生成
SHOOT_TOOL = {
    "type": "function",
    "function": {
        "name": "shoot",
        "description": "开枪射击目标。实弹造成伤害，空弹不造成伤害。射自己如果是空弹可以白嫖一个回合（不轮到庄家）。",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["dealer", "self"],
                    "description": "射击目标：dealer=射庄家，self=射自己"
                },
                "reason": {
                    "type": "string",
                    "description": "简短说明决策理由（1句话，用Lumi主播的语气）"
                }
            },
            "required": ["target", "reason"]
        }
    }
}

def _build_tools(player_items: list, state=None) -> list:
    """根据玩家当前道具+游戏状态动态构建工具列表。过滤掉当前无意义的道具。"""
    tools = [SHOOT_TOOL]
    if player_items:
        # 去重保留顺序
        unique_items = list(dict.fromkeys(player_items))
        # 状态感知过滤：移除当前使用无意义的道具，防止 LLM 做蠢事
        if state:
            if state.dealer_cuffed and "handcuffs" in unique_items:
                unique_items.remove("handcuffs")  # 已铐，再铐无效
            if state.barrel_sawed_off and "handsaw" in unique_items:
                unique_items.remove("handsaw")  # 已锯，再锯无效
        if not unique_items:
            return tools  # 过滤后没道具了，只返回 shoot
        tools.append({
            "type": "function",
            "function": {
                "name": "use_item",
                "description": "使用你拥有的一个道具。只能使用列表中存在的道具。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item": {
                            "type": "string",
                            "enum": unique_items,
                            "description": "要使用的道具英文名（只能选你当前拥有的）"
                        },
                        "reason": {
                            "type": "string",
                            "description": "简短说明使用理由（1句话，用Lumi主播的语气）"
                        }
                    },
                    "required": ["item", "reason"]
                }
            }
        })
    return tools

BUCKSHOT_LLM_PROMPT = """你是Lumi，AI主播，正在直播玩恶魔轮盘（Buckshot Roulette）。
重要：reason字段只写一句简短的决策理由，不要写分析过程。

## 游戏规则
- 你和庄家轮流用霰弹枪射击，弹匣里有实弹和空弹混装
- 你知道弹匣里一共有几发实弹和几发空弹，但不知道顺序
- 射自己如果是空弹：不扣血 + 白嫖一个回合（继续你的回合）
- 射自己如果是实弹：扣自己的血，回合结束
- 射庄家如果是实弹：扣庄家血；空弹：不扣血。无论结果回合都结束

## 核心策略（按优先级排序）

### 1. 先用道具收集信息，再射击
- 有【啤酒】时：用啤酒退掉当前子弹（不发射），这相当于免费跳过一发！退掉后弹匣概率会变化，可能变得对你更有利
- 有【一次性手机】：可以得知某发子弹信息，帮助后续判断
- 永远不要在有信息收集道具时直接赌射击

### 2. 道具连招（非常重要）
- 已知实弹在膛 → 【手铐】铐住庄家（让他跳过下回合）→ 【手锯】锯短枪管（伤害翻倍）→ 射庄家 = 一次造成2倍伤害+庄家跳过回合
- 已知空弹在膛 → 【逆转器】反转成实弹 → 走上面的连招
- 已知空弹 + 没有逆转器 → 射自己白嫖回合

### 3. 血量管理
- 你只剩1-2HP时非常危险！优先用【香烟】回血（回1HP），然后再做其他决策
- 不要在低血量时赌运气射自己

### 4. 概率射击（没有道具时）
- 实弹概率 > 50% → 射庄家
- 实弹概率 < 50% → 射自己赌白嫖（空弹不扣血还能继续）
- 实弹概率 = 50% → 射庄家更安全

### 5. 庄家威胁评估
- 庄家有手锯时，他打你可能造成2倍伤害，要更谨慎保血
- 庄家被铐住了 → 你可以大胆操作，反正下回合他跳过

## 你现在要做的
根据局面，先考虑能不能用道具获取优势，再决定射击。只能用你手上有的道具！
用你主播的风格说出理由（简短、有趣、自信）。"""

# 道具详细描述（给 LLM 看的）
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


class HybridDecisionEngine:
    """混合决策引擎：确定性策略 + LLM 兜底不确定局面"""

    def __init__(self):
        from openai import OpenAI
        from dotenv import load_dotenv
        load_dotenv()
        self.llm_client = OpenAI(
            api_key=os.getenv("ARK_API_KEY"),
            base_url="https://ark.cn-beijing.volces.com/api/v3",
        )
        self.llm_model = "doubao-seed-1-6-flash-250828"
        # 情报追踪（非透视，通过道具合法获取的信息）
        self.known_shell = None          # 当前膛内弹壳类型（放大镜/逆转器得知）
        self.known_positions = {}        # {position: "live"/"blank"} 手机得知
        self.pending_item = None         # 等待 action_executed 回传结果的道具名
        self._last_llm_info = None       # 最近一次 LLM 调用信息（写入 jsonl）
        self._last_shells_remaining = -1 # 追踪弹匣变化，重置情报

    def on_action_executed(self, msg: dict):
        """处理 action_executed 消息，更新情报"""
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

    def on_state_update(self, state: "GameState"):
        """状态更新时检查是否需要重置情报"""
        if state.shells_remaining != self._last_shells_remaining:
            if state.shells_remaining > self._last_shells_remaining:
                # 弹匣重新装填了，清空所有情报
                self.known_shell = None
                self.known_positions.clear()
            elif self.known_shell is not None:
                # 射击后弹匣减少，当前弹壳已消耗，清除当前情报
                self.known_shell = None
                # 手机情报的位置也要前移（第一发被消耗了）
                new_positions = {}
                for pos, shell_type in self.known_positions.items():
                    if pos > 0:
                        new_positions[pos - 1] = shell_type
                self.known_positions = new_positions
            self._last_shells_remaining = state.shells_remaining

    def _log_decision(self, layer: str, decision: dict, state: "GameState", effective_known):
        """打印决策日志"""
        items_cn = items_to_cn(state.player_items)
        known_str = "未知"
        if effective_known == "live":
            known_str = "实弹"
        elif effective_known == "blank":
            known_str = "空弹"
        phone_intel = ""
        if self.known_positions:
            parts = [f"第{p+1}发={'实弹' if t == 'live' else '空弹'}" for p, t in sorted(self.known_positions.items())]
            phone_intel = f"  手机情报: {', '.join(parts)}\n"

        print(
            f"\n{C_YELLOW}┌─ 决策日志 {'─'*38}\n"
            f"│ 决策层: {C_BOLD}{layer}{C_RESET}{C_YELLOW}\n"
            f"│ 当前情报: 膛内={known_str}\n"
            f"{f'│ {phone_intel}' if phone_intel else ''}"
            f"│ 玩家道具: {items_cn}\n"
            f"│ 弹匣: {state.live_remaining}实/{state.blank_remaining}空 (共{state.shells_remaining}发)\n"
            f"│ HP: 玩家{state.health_player}/{state.max_health} vs 庄家{state.health_opponent}/{state.max_health}\n"
            f"│ → {decision['action']}"
            f"{' ' + decision.get('target','') if 'target' in decision else ''}"
            f"{' ' + ITEM_NAMES_CN.get(decision.get('item',''), decision.get('item','')) if 'item' in decision else ''}\n"
            f"│ → 理由: {decision.get('reason','')}\n"
            f"└{'─'*50}{C_RESET}"
        )

    def decide(self, state: "GameState") -> dict:
        """三层决策：确定性规则 → LLM → 兜底"""
        items = state.player_items

        # 如果手机情报里有当前位置(0)的信息，也算已知
        effective_known = self.known_shell
        if effective_known is None and 0 in self.known_positions:
            effective_known = self.known_positions[0]

        # 决策上下文（附带到 decision dict 里，写入 jsonl）
        context = {
            "known_shell": effective_known,
            "phone_intel": dict(self.known_positions) if self.known_positions else None,
            "player_items": list(items),
            "dealer_items": list(state.dealer_items),
            "live": state.live_remaining,
            "blank": state.blank_remaining,
            "hp": state.health_player,
            "hp_max": state.max_health,
            "hp_opp": state.health_opponent,
            "barrel_sawed": state.barrel_sawed_off,
            "dealer_cuffed": state.dealer_cuffed,
        }

        # ===== 第一层：确定性策略 =====
        result = self._deterministic_decide(state, items, effective_known)
        if result:
            result["_layer"] = "deterministic"
            result["_context"] = context
            self._log_decision("第一层·确定性策略", result, state, effective_known)
            return result

        # ===== 第二层：不确定局面 → 交给 LLM =====
        self._last_llm_info = None
        llm_result = self._llm_decide(state, items, effective_known)
        if llm_result:
            llm_result["_layer"] = "llm"
            llm_result["_context"] = context
            if self._last_llm_info:
                llm_result["_llm"] = self._last_llm_info
            self._log_decision("第二层·LLM决策", llm_result, state, effective_known)
            return llm_result

        # ===== 第三层：LLM 失败兜底（也尝试用道具） =====
        p_live = state.live_probability
        # 兜底也用道具：低血量先抽烟回血
        if "cigarettes" in items and state.health_player < state.max_health and state.health_player <= 2:
            fallback = {"action": "use_item", "item": "cigarettes", "reason": f"兜底策略：血量危险({state.health_player}HP)，先抽烟回血"}
        # 兜底用手铐：实弹概率高时先铐庄家
        elif "handcuffs" in items and not state.dealer_cuffed and p_live >= 0.5:
            fallback = {"action": "use_item", "item": "handcuffs", "reason": f"兜底策略：先铐住庄家"}
        # 兜底用手锯：实弹概率高时锯枪管
        elif "handsaw" in items and not state.barrel_sawed_off and p_live >= 0.5:
            fallback = {"action": "use_item", "item": "handsaw", "reason": f"兜底策略：锯短枪管翻倍伤害"}
        # 兜底用啤酒：高实弹概率时退一发碰运气
        elif "beer" in items and p_live >= 0.6:
            fallback = {"action": "use_item", "item": "beer", "reason": f"兜底策略：实弹概率{p_live:.0%}高，啤酒退掉碰运气"}
        elif p_live >= 0.5:
            fallback = {"action": "shoot", "target": "dealer", "reason": f"兜底策略：实弹概率{p_live:.0%}，射庄家"}
        else:
            fallback = {"action": "shoot", "target": "self", "reason": f"兜底策略：空弹概率{1-p_live:.0%}，射自己"}
        fallback["_layer"] = "fallback"
        fallback["_context"] = context
        if self._last_llm_info:
            fallback["_llm_error"] = self._last_llm_info  # LLM 失败信息
        self._log_decision("第三层·兜底", fallback, state, effective_known)
        return fallback

    def _deterministic_decide(self, state, items, effective_known) -> dict | None:
        """第一层：确定性策略"""
        # 只剩1发：100%已知
        if state.shells_remaining == 1:
            remaining_live = state.live_remaining
            if remaining_live > 0:
                # 确定实弹，走连招（手铐→手锯→射）
                if "handcuffs" in items and not state.dealer_cuffed:
                    return {"action": "use_item", "item": "handcuffs", "reason": "最后一发实弹，先铐住再打"}
                if "handsaw" in items and not state.barrel_sawed_off:
                    return {"action": "use_item", "item": "handsaw", "reason": "最后一发实弹，锯短枪管翻倍伤害"}
                return {"action": "shoot", "target": "dealer", "reason": "最后一发必是实弹，射庄家！"}
            else:
                # 确定空弹，先利用道具
                if "inverter" in items:
                    return {"action": "use_item", "item": "inverter", "reason": "最后一发空弹，逆转成实弹射庄家"}
                if "cigarettes" in items and state.health_player < state.max_health:
                    return {"action": "use_item", "item": "cigarettes", "reason": "最后一发空弹，先抽烟回血"}
                return {"action": "shoot", "target": "self", "reason": "最后一发空弹，射自己白嫖~"}

        # 全是空弹（live=0）→ 直接射自己白嫖回合，不需要进 LLM
        if state.live_remaining == 0 and state.blank_remaining > 0:
            if "cigarettes" in items and state.health_player < state.max_health:
                return {"action": "use_item", "item": "cigarettes", "reason": "全是空弹，先抽烟回血再射自己"}
            return {"action": "shoot", "target": "self", "reason": "全是空弹，射自己白嫖回合~"}

        # 全是实弹（blank=0）→ 走连招打庄家
        if state.blank_remaining == 0 and state.live_remaining > 0:
            if "handcuffs" in items and not state.dealer_cuffed and state.shells_remaining > 1:
                return {"action": "use_item", "item": "handcuffs", "reason": "全是实弹，先铐住再打"}
            if "handsaw" in items and not state.barrel_sawed_off:
                return {"action": "use_item", "item": "handsaw", "reason": "全是实弹，锯短枪管翻倍伤害"}
            return {"action": "shoot", "target": "dealer", "reason": "全是实弹，射庄家！"}

        # 有放大镜且不知道当前弹壳 → 先用放大镜
        if "magnifying glass" in items and effective_known is None:
            return {"action": "use_item", "item": "magnifying glass", "reason": "先看看这发是什么弹"}

        # 确定实弹 → 连招：铐 → 锯 → 射庄家
        if effective_known == "live":
            if "handcuffs" in items and not state.dealer_cuffed and state.shells_remaining > 1:
                return {"action": "use_item", "item": "handcuffs", "reason": "实弹在膛，先铐住你再说"}
            if "handsaw" in items and not state.barrel_sawed_off:
                return {"action": "use_item", "item": "handsaw", "reason": "实弹在膛，锯短枪管伤害翻倍"}
            return {"action": "shoot", "target": "dealer", "reason": "确定是实弹，吃我一枪吧！"}

        # 确定空弹 → 先看看有没有逆转器可以变实弹射庄家
        if effective_known == "blank":
            if "inverter" in items:
                return {"action": "use_item", "item": "inverter", "reason": "空弹？逆转一下变实弹射你"}
            if "cigarettes" in items and state.health_player < state.max_health:
                return {"action": "use_item", "item": "cigarettes", "reason": "反正是空弹，先抽根烟回血"}
            return {"action": "shoot", "target": "self", "reason": "确定是空弹，射自己白嫖一回合~"}

        return None  # 不确定，交给 LLM

    def _llm_decide(self, state: "GameState", items: list, known_shell) -> dict | None:
        """调用 LLM 处理不确定局面"""
        # 构造局面描述
        items_cn = [ITEM_DESC_CN.get(i, i) for i in items]
        dealer_items_cn = [ITEM_DESC_CN.get(i, i) for i in state.dealer_items]
        live = state.live_remaining
        blank = state.blank_remaining
        total = live + blank
        prob = f"{live/total*100:.0f}%" if total > 0 else "?"

        state_text = (
            f"=== 当前局面 ===\n"
            f"你的HP: {state.health_player}/{state.max_health}\n"
            f"庄家HP: {state.health_opponent}/{state.max_health}\n"
            f"弹匣剩余: {total}发（{live}发实弹 + {blank}发空弹）\n"
            f"当前子弹是实弹的概率: {prob}\n"
            f"枪管已锯短: {'是（本次伤害翻倍）' if state.barrel_sawed_off else '否'}\n"
            f"庄家被铐住: {'是（庄家下回合跳过）' if state.dealer_cuffed else '否'}\n"
            f"你的道具: {items_cn if items_cn else '无'}\n"
            f"庄家的道具: {dealer_items_cn if dealer_items_cn else '无'}"
        )

        # 加上已知情报
        intel_lines = []
        if known_shell:
            intel_lines.append(f"当前膛内是【{'实弹' if known_shell == 'live' else '空弹'}】")
        for pos, stype in sorted(self.known_positions.items()):
            if pos > 0:  # 位置0已经在 known_shell 里了
                intel_lines.append(f"第{pos+1}发是【{'实弹' if stype == 'live' else '空弹'}】")
        if intel_lines:
            state_text += "\n★ 已知情报：" + "；".join(intel_lines)

        user_content = f"轮到你了，请决策。\n\n{state_text}"
        messages = [
            {"role": "system", "content": BUCKSHOT_LLM_PROMPT},
            {"role": "user", "content": user_content}
        ]

        # 动态构建工具（只包含玩家当前可用的道具）
        available_items = list(items)
        if state.health_player >= state.max_health and "cigarettes" in available_items:
            available_items.remove("cigarettes")  # 满血时不给烟选项
        tools = _build_tools(available_items, state)

        # 打印发给 LLM 的完整信息
        tools_summary = [t["function"]["name"] + "(" + ", ".join(t["function"]["parameters"]["properties"].keys()) + ")" for t in tools]
        item_enum = None
        for t in tools:
            if t["function"]["name"] == "use_item":
                item_enum = t["function"]["parameters"]["properties"]["item"]["enum"]
        print(
            f"\n{C_CYAN}┌─ LLM 请求 {'─'*39}\n"
            f"│ 模型: {self.llm_model}\n"
            f"│ 工具: {tools_summary}\n"
            f"│ use_item可选: {item_enum if item_enum else '(无道具，已移除use_item)'}\n"
            f"│ tool_choice: required\n"
            f"│ ── System Prompt ──\n"
            f"│ {BUCKSHOT_LLM_PROMPT[:100]}...\n"
            f"│ ── User Message ──\n"
        )
        for line in user_content.split("\n"):
            print(f"│ {line}")
        print(f"└{'─'*50}{C_RESET}")

        try:
            t0 = time.time()
            response = self.llm_client.chat.completions.create(
                model=self.llm_model,
                messages=messages,
                tools=tools,
                tool_choice="required",
            )
            elapsed = time.time() - t0
            msg = response.choices[0].message

            if not msg.tool_calls:
                print(f"{C_RED}[LLM] 模型未返回工具调用 (耗时 {elapsed:.1f}s)，原文: {msg.content[:200] if msg.content else '(空)'}{C_RESET}")
                self._last_llm_info = {"model": self.llm_model, "elapsed": round(elapsed, 2),
                                       "state_text": state_text, "error": "no_tool_calls",
                                       "raw": msg.content[:200] if msg.content else None}
                return None

            tc = msg.tool_calls[0]
            args = json.loads(tc.function.arguments)
            action = tc.function.name
            item = args.get("item", "")

            # 打印 LLM 返回结果
            print(
                f"\n{C_CYAN}┌─ LLM 响应 (耗时 {elapsed:.1f}s) {'─'*28}\n"
                f"│ tool_call: {action}({json.dumps(args, ensure_ascii=False)})\n"
                f"│ finish_reason: {response.choices[0].finish_reason}\n"
                f"└{'─'*50}{C_RESET}"
            )

            # 记录 LLM 信息（会被 decide() 附加到 decision dict）
            self._last_llm_info = {
                "model": self.llm_model,
                "elapsed": round(elapsed, 2),
                "state_text": state_text,
                "tool_call": f"{action}({json.dumps(args, ensure_ascii=False)})",
                "finish_reason": response.choices[0].finish_reason,
            }

            # 模糊匹配修正：LLM 经常把 "magnifying glass" 写成别的形式
            if action == "use_item" and item not in state.player_items:
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
                }
                corrected = ITEM_ALIASES.get(item.lower().strip(), None)
                if corrected and corrected in state.player_items:
                    print(f"{C_YELLOW}[LLM] 道具名修正: {item} → {corrected}{C_RESET}")
                    item = corrected
                    args["item"] = corrected
                else:
                    print(f"{C_RED}[LLM] 校验失败：模型选了玩家没有的道具 [{item}]，回退兜底{C_RESET}")
                    self._last_llm_info["error"] = f"invalid_item:{item}"
                    return None

            return {"action": action, **args}

        except Exception as e:
            print(f"{C_RED}[LLM] 调用异常: {e}{C_RESET}")
            self._last_llm_info = {"model": self.llm_model, "state_text": state_text,
                                   "error": str(e)}
            return None


# ==================== TCP 客户端 ====================

class BuckshotBot:
    def __init__(self, watch_only=False, auto_mode=True, hybrid=False):
        self.sock = None
        self.state = GameState()
        self.hybrid = hybrid
        if hybrid:
            print(f"{C_CYAN}[Bot] 初始化混合决策引擎（代码策略 + LLM）...{C_RESET}")
            self.engine = HybridDecisionEngine()
        else:
            self.engine = DecisionEngine()
        self.watch_only = watch_only
        self.auto_mode = auto_mode
        self.running = True
        self.recv_buffer = ""
        self.last_acted_phase = ""
        # 自动化统计
        self.wins = 0
        self.losses = 0
        self.game_events: list = []
        self.godot_process: subprocess.Popen | None = None
        # 日志
        self.log_entries: list = []
        self.start_time = datetime.now()

    def log(self, entry: dict):
        """记录一条日志（内存中，退出时写文件）"""
        entry["timestamp"] = datetime.now().isoformat()
        self.log_entries.append(entry)

    def save_logs(self):
        """保存日志到 logs/buckshot_YYYY-MM-DD_HH-MM-SS.jsonl"""
        if not self.log_entries:
            print(f"{C_YELLOW}[Bot] 无日志需要保存{C_RESET}")
            return
        log_dir = os.path.join(os.path.dirname(__file__), "logs")
        os.makedirs(log_dir, exist_ok=True)
        filename = f"buckshot_{self.start_time.strftime('%Y-%m-%d_%H-%M-%S')}.jsonl"
        filepath = os.path.join(log_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            for entry in self.log_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"{C_GREEN}[Bot] 日志已保存: {filepath} ({len(self.log_entries)} 条){C_RESET}")

    def launch_godot(self):
        """启动 Godot 运行游戏项目"""
        if not os.path.exists(GODOT_EXE):
            print(f"{C_RED}[Bot] Godot 不存在: {GODOT_EXE}{C_RESET}")
            return
        if not os.path.exists(GODOT_PROJECT):
            print(f"{C_RED}[Bot] 项目不存在: {GODOT_PROJECT}{C_RESET}")
            return
        print(f"{C_CYAN}[Bot] 启动 Godot 游戏...{C_RESET}")
        self.godot_process = subprocess.Popen(
            [GODOT_EXE, "--path", GODOT_PROJECT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"{C_GREEN}[Bot] Godot 已启动 (PID: {self.godot_process.pid}){C_RESET}")

    def connect(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((HOST, PORT))
            self.sock.settimeout(0.1)
            print(f"{C_GREEN}[Bot] 已连接到游戏 {HOST}:{PORT}{C_RESET}")
            # 连接后自动发送 auto_mode 命令
            if self.auto_mode:
                self.send_command({"action": "set_auto_mode", "enabled": True})
                print(f"{C_CYAN}[Bot] 已启用游戏内自动化模式{C_RESET}")
            return True
        except Exception as e:
            print(f"{C_RED}[Bot] 连接失败: {e}{C_RESET}")
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
            print(f"{C_MAGENTA}[Bot] >>> {cmd['action']}"
                  f"{' → ' + target_cn if target_cn else ''}"
                  f"{' → ' + item_cn if item_cn else ''}"
                  f"  ({reason}){C_RESET}")
        except Exception as e:
            print(f"{C_RED}[Bot] 发送失败: {e}{C_RESET}")

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
            print(f"{C_RED}[Bot] 接收错误: {e}{C_RESET}")
            self.sock = None
        return messages

    def update_state(self, data: dict):
        if data.get("type") != "game_state":
            return
        self.state.phase = data.get("phase", "unknown")
        self.state.health_player = data.get("health_player", 0)
        self.state.health_opponent = data.get("health_opponent", 0)
        self.state.max_health = data.get("max_health", 0)
        self.state.shells_sequence = data.get("shells_sequence", [])
        self.state.shells_remaining = data.get("shells_remaining", 0)
        self.state.shells_live_total = data.get("shells_live_total", 0)
        self.state.shells_blank_total = data.get("shells_blank_total", 0)
        self.state.current_shell = data.get("current_shell", "unknown")
        self.state.shotgun_damage = data.get("shotgun_damage", 1)
        self.state.barrel_sawed_off = data.get("barrel_sawed_off", False)
        self.state.dealer_cuffed = data.get("dealer_cuffed", False)
        self.state.player_cuffed = data.get("player_cuffed", False)
        self.state.player_items = data.get("player_items", [])
        self.state.dealer_items = data.get("dealer_items", [])
        self.state.round = data.get("round", 0)
        self.state.batch = data.get("batch", 0)
        self.state.endless = data.get("endless", False)

    def run(self):
        mode_str = "观察模式" if self.watch_only else "自动决策模式"
        auto_str = " + 全自动化" if self.auto_mode else ""
        engine_str = "混合引擎（代码+LLM）" if self.hybrid else "确定性引擎（透视）"
        print(f"{C_CYAN}{'='*50}")
        print(f"  恶魔轮盘 AI Bot - {mode_str}{auto_str}")
        print(f"  决策引擎: {engine_str}")
        print(f"  --watch / --legacy / --no-auto")
        print(f"  按 Ctrl+C 退出（日志会自动保存）")
        print(f"{'='*50}{C_RESET}")

        # 自动启动 Godot
        if self.auto_mode:
            self.launch_godot()
            print(f"[Bot] 等待游戏启动...")
            time.sleep(5)  # 给 Godot 启动时间

        while self.running:
            # 连接
            if not self.sock:
                if not self.connect():
                    print(f"[Bot] {RECONNECT_INTERVAL}秒后重连...")
                    time.sleep(RECONNECT_INTERVAL)
                    continue

            # 检测 Godot 进程是否退出
            if self.godot_process and self.godot_process.poll() is not None:
                print(f"\n{C_YELLOW}[Bot] 游戏已关闭，停止运行{C_RESET}")
                self.running = False
                break

            # 接收消息
            messages = self.receive_messages()
            if not self.sock:
                continue

            for msg in messages:
                msg_type = msg.get("type", "")
                if msg_type == "game_state":
                    self.update_state(msg)
                    # 混合引擎：更新情报追踪
                    if self.hybrid:
                        self.engine.on_state_update(self.state)
                    state_log = {
                        "type": "state", "phase": self.state.phase,
                        "hp": self.state.health_player, "hp_opp": self.state.health_opponent,
                        "hp_max": self.state.max_health,
                        "shells": self.state.shells_remaining,
                        "live": self.state.live_remaining, "blank": self.state.blank_remaining,
                        "player_items": list(self.state.player_items),
                        "dealer_items": list(self.state.dealer_items),
                        "barrel_sawed": self.state.barrel_sawed_off,
                        "dealer_cuffed": self.state.dealer_cuffed,
                    }
                    self.log(state_log)
                    print(self.state.summary())

                    # 自动决策
                    if not self.watch_only and self.state.phase == "player_turn":
                        if self.last_acted_phase != f"player_turn_{self.state.shells_remaining}_{self.state.health_player}_{self.state.health_opponent}_{len(self.state.player_items)}":
                            self.last_acted_phase = f"player_turn_{self.state.shells_remaining}_{self.state.health_player}_{self.state.health_opponent}_{len(self.state.player_items)}"
                            decision = self.engine.decide(self.state)
                            self.log({"type": "decision", **decision})
                            print(f"\n{C_YELLOW}[决策] {decision.get('reason', '')}{C_RESET}")
                            time.sleep(1.5)  # 给点思考时间（节目效果）
                            self.send_command(decision)

                elif msg_type == "game_event":
                    event = msg.get("event", "")
                    self.game_events.append(msg)
                    self.log(msg)
                    # 特殊事件处理
                    if event == "victory":
                        self.wins += 1
                        print(f"\n{C_GREEN}{C_BOLD}{'='*50}")
                        print(f"  胜利！总计 {self.wins}胜/{self.losses}负")
                        print(f"{'='*50}{C_RESET}")
                        stats = {k: v for k, v in msg.items() if k.startswith("stat_")}
                        if stats:
                            print(f"{C_CYAN}  统计: {stats}{C_RESET}")
                    elif event == "defeat":
                        self.losses += 1
                        print(f"\n{C_RED}{C_BOLD}{'='*50}")
                        print(f"  败北... 总计 {self.wins}胜/{self.losses}负")
                        print(f"{'='*50}{C_RESET}")
                    elif event == "death_path":
                        path = msg.get("path", "unknown")
                        print(f"{C_MAGENTA}[死亡] 路径: {path}{C_RESET}")
                    elif event == "death_recovery_main":
                        print(f"{C_CYAN}[复活] 已回到主场景，等待重新开始...{C_RESET}")
                    elif event == "batch_won":
                        print(f"{C_GREEN}[胜利] 庄家被击败！{C_RESET}")
                    elif event == "scene_changed":
                        scene = msg.get("scene", "")
                        print(f"{C_CYAN}[事件] 场景切换: {scene}{C_RESET}")
                    elif event == "waiver_signed":
                        print(f"{C_MAGENTA}[事件] 签署生死状: {msg.get('name', '')}{C_RESET}")
                    # 过程事件 — 战斗中发生了什么
                    elif event == "dealer_shot_player":
                        print(f"{C_RED}[战斗] 庄家射击玩家！伤害 {msg.get('damage',1)}，剩余 HP {msg.get('hp_remaining',0)}{C_RESET}")
                    elif event == "player_shot_dealer":
                        print(f"{C_GREEN}[战斗] 玩家射击庄家！伤害 {msg.get('damage',1)}，庄家剩余 HP {msg.get('hp_remaining',0)}{C_RESET}")
                    elif event == "player_shot_dealer_blank":
                        print(f"{C_YELLOW}[战斗] 玩家射击庄家，空弹没有造成伤害{C_RESET}")
                    elif event == "dealer_shot_self":
                        print(f"{C_GREEN}[战斗] 庄家自射实弹翻车！伤害 {msg.get('damage',1)}，庄家剩余 HP {msg.get('hp_remaining',0)}{C_RESET}")
                    elif event == "dealer_shot_self_blank":
                        print(f"{C_CYAN}[战斗] 庄家自射空弹，白嫖一回合{C_RESET}")
                    elif event == "player_shot_self_blank":
                        print(f"{C_CYAN}[战斗] 玩家自射空弹，白嫖一回合{C_RESET}")
                    elif event == "player_took_damage":
                        print(f"{C_RED}[战斗] 玩家受伤！伤害 {msg.get('damage',1)}，剩余 HP {msg.get('hp_remaining',0)}{C_RESET}")
                    elif event == "player_healed":
                        print(f"{C_GREEN}[战斗] 玩家回复 {msg.get('amount',1)} HP，当前 HP {msg.get('hp_now',0)}{C_RESET}")
                    elif event == "dealer_healed":
                        print(f"{C_RED}[战斗] 庄家回复 {msg.get('amount',1)} HP，当前 HP {msg.get('hp_now',0)}{C_RESET}")
                    elif event == "player_turn_start":
                        print(f"{C_BOLD}[回合] 轮到玩家{C_RESET}")
                    elif event == "dealer_turn_start":
                        print(f"{C_BOLD}[回合] 轮到庄家{C_RESET}")
                    elif event == "round_start":
                        print(f"{C_CYAN}[回合] 新回合开始 — 回合 {msg.get('round',0)}，阶段 {msg.get('batch',0)}{C_RESET}")
                    elif event == "briefcase_opened":
                        print(f"{C_MAGENTA}[事件] 打开了奖金箱！{C_RESET}")
                    else:
                        print(f"{C_YELLOW}[事件] {event}{C_RESET}")

                elif msg_type == "action_executed":
                    print(f"{C_GREEN}[游戏] 动作已执行: {msg.get('action')}{C_RESET}")
                    # 混合引擎：处理道具使用结果（放大镜/手机/逆转器返回的情报）
                    if self.hybrid and msg.get("action") == "use_item":
                        self.engine.on_action_executed(msg)
                elif msg_type == "error":
                    print(f"{C_RED}[游戏] 错误: {msg.get('message')}{C_RESET}")
                elif msg_type == "connected":
                    print(f"{C_GREEN}[游戏] {msg.get('message')}{C_RESET}")

            time.sleep(0.05)  # 主循环间隔


def main():
    watch_only = "--watch" in sys.argv
    auto_mode = "--no-auto" not in sys.argv
    hybrid = "--legacy" not in sys.argv  # 默认混合引擎，--legacy 用旧透视引擎
    bot = BuckshotBot(watch_only=watch_only, auto_mode=auto_mode, hybrid=hybrid)
    try:
        bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n{C_YELLOW}{'='*50}")
        print(f"  恶魔轮盘 Bot 已停止")
        print(f"  总计 {bot.wins}胜/{bot.losses}负")
        print(f"{'='*50}{C_RESET}")
        if bot.sock:
            bot.sock.close()
        if bot.godot_process and bot.godot_process.poll() is None:
            print(f"{C_YELLOW}[Bot] 关闭 Godot 进程...{C_RESET}")
            bot.godot_process.terminate()
        bot.log({"type": "session_end", "wins": bot.wins, "losses": bot.losses})
        bot.save_logs()
        print(f"\n按回车键关闭窗口...")
        input()


if __name__ == "__main__":
    main()
