"""
Kingdom Rush AI 自动对战模块
基于规则的决策系统，通过 KingdomRushBot TCP 连接控制游戏

用法:
    python kingdom_rush_ai.py              # 自动对战
    python kingdom_rush_ai.py --dry-run    # 只显示决策，不执行
"""

import time
import sys
import os
import argparse
import math
import json
import subprocess
import ctypes
import ctypes.wintypes
from datetime import datetime
from kingdom_rush_bot import KingdomRushBot
from kr_battle_history import load_history, save_history, add_record, get_level_history
from kr_strategy_llm import call_llm_for_strategy

# ========== 游戏启动 ==========
GAME_EXE = r"C:\Games\Kingdom Rush 1\Kingdom Rush.exe"  # set to your own game install path
LAUNCHER_TITLE = "王国保卫战"
LAUNCHER_BUTTON = "开始"

# ========== 塔费用表 ==========
TOWER_COSTS = {
    "tower_archer_1": 70, "tower_archer_2": 110, "tower_archer_3": 160,
    "tower_ranger": 230, "tower_musketeer": 230,
    "tower_barrack_1": 70, "tower_barrack_2": 110, "tower_barrack_3": 160,
    "tower_paladin": 230, "tower_barbarian": 230,
    "tower_mage_1": 100, "tower_mage_2": 160, "tower_mage_3": 240,
    "tower_arcane_wizard": 300, "tower_sorcerer": 300,
    "tower_engineer_1": 125, "tower_engineer_2": 220, "tower_engineer_3": 320,
    "tower_tesla": 375, "tower_bfg": 400,
}

# 升级路径: 当前模板 → (下一级模板, 费用)
UPGRADE_PATH = {
    "tower_archer_1":  ("tower_archer_2", 110),
    "tower_archer_2":  ("tower_archer_3", 160),
    "tower_archer_3":  ("tower_ranger", 230),      # 默认走游侠
    "tower_barrack_1": ("tower_barrack_2", 110),
    "tower_barrack_2": ("tower_barrack_3", 160),
    "tower_barrack_3": ("tower_barbarian", 230),    # 默认走蛮族
    "tower_mage_1":    ("tower_mage_2", 160),
    "tower_mage_2":    ("tower_mage_3", 240),
    "tower_mage_3":    ("tower_arcane_wizard", 300),
    "tower_engineer_1": ("tower_engineer_2", 220),
    "tower_engineer_2": ("tower_engineer_3", 320),
    "tower_engineer_3": ("tower_tesla", 375),
}

# 建塔基础费用
BUILD_COSTS = {
    "archer": 70,
    "barrack": 70,
    "mage": 100,
    "engineer": 125,
}

# DPS塔类型（非兵营）
DPS_TOWER_TYPES = {"archer", "mage", "engineer"}

# 伤害类型分类
PHYSICAL_TOWER_TYPES = {"archer", "engineer"}  # 物理伤害塔
MAGICAL_TOWER_TYPES = {"mage"}  # 魔法伤害塔

# 中文名称映射
CN_TOWER_TYPE = {
    "archer": "弓箭塔", "barrack": "兵营", "mage": "法师塔", "engineer": "炮塔",
}
CN_TOWER_TEMPLATE = {
    "tower_archer_1": "弓箭塔I", "tower_archer_2": "弓箭塔II", "tower_archer_3": "弓箭塔III",
    "tower_ranger": "游侠哨塔", "tower_musketeer": "火枪塔",
    "tower_barrack_1": "兵营I", "tower_barrack_2": "兵营II", "tower_barrack_3": "兵营III",
    "tower_paladin": "圣骑士", "tower_barbarian": "蛮族营地",
    "tower_mage_1": "法师塔I", "tower_mage_2": "法师塔II", "tower_mage_3": "法师塔III",
    "tower_arcane_wizard": "奥术法师塔", "tower_sorcerer": "巫师塔",
    "tower_engineer_1": "炮塔I", "tower_engineer_2": "炮塔II", "tower_engineer_3": "炮塔III",
    "tower_tesla": "特斯拉", "tower_bfg": "超级大炮",
}

TOWER_TEMPLATE_ALIASES = {
    "ranger": "tower_ranger",
    "musketeer": "tower_musketeer",
    "paladin": "tower_paladin",
    "barbarian": "tower_barbarian",
    "arcane_wizard": "tower_arcane_wizard",
    "sorcerer": "tower_sorcerer",
    "tesla": "tower_tesla",
    "bfg": "tower_bfg",
}

SPECIAL_TOWER_CN = {
    "holder_sasquash": "大脚怪洞穴",
    "tower_holder_sasquash": "大脚怪洞穴",
}

# 怪物中文名映射（KR1 全怪物表）
CN_ENEMY = {
    # 哥布林系
    "enemy_goblin": "哥布林",
    "enemy_goblin_zapper": "哥布林电击兵",
    # 兽人系
    "enemy_orc": "兽人",
    "enemy_fat_orc": "胖兽人",
    "enemy_orc_champion": "兽人勇士",
    "enemy_ogre": "食人魔",
    "enemy_ogre_magi": "食人魔法师",
    # 人类系
    "enemy_bandit": "土匪",
    "enemy_brigand": "强盗",
    "enemy_marauder": "劫掠者",
    "enemy_dark_knight": "黑暗骑士",
    "enemy_dark_slayer": "黑暗剑客",
    # 兽类
    "enemy_wolf": "狼",
    "enemy_wolf_small": "小狼",
    "enemy_worg": "座狼",
    "enemy_worg_rider": "骑狼兽人",
    "enemy_gargoyle": "石像鬼",
    # 蜘蛛系
    "enemy_spider_tiny": "小蜘蛛",
    "enemy_spider_small": "蜘蛛",
    "enemy_spider_big": "大蜘蛛",
    "enemy_spider_matriarch": "蜘蛛女王",
    # 巨魔系
    "enemy_troll": "巨魔",
    "enemy_troll_champion": "巨魔勇士",
    "enemy_troll_chieftain": "巨魔酋长",
    "enemy_troll_breaker": "巨魔破坏者",
    # 法师/萨满系
    "enemy_shaman": "萨满",
    "enemy_necromancer": "死灵法师",
    "enemy_magus": "暗黑法师",
    # 亡灵系
    "enemy_skeleton": "骷髅",
    "enemy_skeleton_knight": "骷髅骑士",
    "enemy_zombie": "僵尸",
    "enemy_shadow": "暗影",
    # 恶魔系
    "enemy_demon": "恶魔",
    "enemy_demon_lord": "恶魔领主",
    "enemy_demon_hound": "地狱犬",
    "enemy_juggernaut": "巨像",
    "enemy_son_of_sarelgaz": "萨雷尔加兹之子",
    # BOSS
    "enemy_boss_vez_nan": "维兹南",
    "enemy_boss_juggernaut": "巨像(BOSS)",
    "enemy_boss_myconid": "菌族王",
}

ENEMY_ALIASES = {
    "shadow_archer": "暗影弓手",
    "whitewolf": "白狼",
    "yeti": "雪怪",
    "juggernaut": "巨像",
    "eb_juggernaut": "巨像",
    "thrower": "投石巨魔",
    "troll_thrower": "投石巨魔",
}


def _normalize_template_key(template):
    return str(template or "").strip().lower().replace(" ", "_").replace("-", "_")


def _infer_enemy_cn_from_key(key):
    """未知怪物按内部名关键词降级成中文粗类型，保留局面信息但不泄露英文。"""
    if "archer" in key:
        return "远程怪"
    if "wolf" in key or "worg" in key:
        return "狼类怪"
    if "yeti" in key:
        return "雪怪"
    if "troll" in key:
        return "巨魔类怪"
    if "spider" in key:
        return "蜘蛛类怪"
    if "demon" in key:
        return "恶魔类怪"
    if "gargoyle" in key or "flying" in key or "air" in key:
        return "飞行怪"
    if "boss" in key or "juggernaut" in key:
        return "首领怪"
    if "thrower" in key:
        return "投掷怪"
    return "未知怪物"


def cn_enemy(template):
    """怪物模板名 → 中文名；未知时不把英文内部名喂给快脑。"""
    key = _normalize_template_key(template)
    if key in CN_ENEMY:
        return CN_ENEMY[key]
    if key.startswith("enemy_") and key[6:] in ENEMY_ALIASES:
        return ENEMY_ALIASES[key[6:]]
    if key in ENEMY_ALIASES:
        return ENEMY_ALIASES[key]
    return _infer_enemy_cn_from_key(key[6:] if key.startswith("enemy_") else key)


# ========== 策略模板（LLM 开局决策用） ==========
STRATEGY_TEMPLATES = [
    {
        "label": "均衡防守",
        "description": "基线策略，物理法术兼顾",
        "bias": {},  # 无偏移
    },
    {
        "label": "物理火力",
        "description": "侧重弓箭+炮塔物理输出，适合敌人高法抗时",
        "bias": {"phys_boost": 0.25, "mage_boost": -0.15, "type_preference": "archer"},
    },
    {
        "label": "法伤克制",
        "description": "侧重法师塔魔法输出，适合敌人高物抗时",
        "bias": {"mage_boost": 0.25, "phys_boost": -0.15, "type_preference": "mage"},
    },
]


def cn_type(tower_type):
    return CN_TOWER_TYPE.get(tower_type, tower_type)


def cn_template(template):
    key = _normalize_template_key(template)
    key = TOWER_TEMPLATE_ALIASES.get(key, key)
    if key in CN_TOWER_TEMPLATE:
        return CN_TOWER_TEMPLATE[key]
    if key in SPECIAL_TOWER_CN:
        return SPECIAL_TOWER_CN[key]
    return CN_TOWER_TYPE.get(key, "未知防御塔")


def format_tower_summary(towers):
    """把当前场上塔列表压成中文摘要，避免把内部模板名写进 prompt。"""
    counts = {}
    for t in towers or []:
        name = cn_template(t.get("template") or t.get("type", ""))
        if name == "未知防御塔":
            name = cn_type(t.get("type", "")) if t.get("type") else name
        if not name or name == "未知防御塔":
            continue
        counts[name] = counts.get(name, 0) + 1
    return "、".join(f"{name}{count}座" for name, count in counts.items()) or "还没建塔"


def dist(x1, y1, x2, y2):
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


class KingdomRushAI:
    def __init__(self, bot, dry_run=False, bridge_version="?", event_callback=None,
                 level_idx=None, level_mode=1, battle_history=None, star_goal=None):
        self.bot = bot
        self.dry_run = dry_run
        self.bridge_version = bridge_version
        self.level_idx = level_idx      # 当前关卡索引
        self.level_mode = level_mode    # 1=normal, 2=iron, 3=heroic
        self.tick_count = 0
        self.start_time = datetime.now()
        self.log_entries = []  # 每条: {tick, time, state, actions}
        self._current_actions = []  # 当前 tick 的动作列表
        self._current_prints = []  # 当前 tick 的终端输出
        self._last_hero_move_time = 0  # 英雄上次移动时间
        self._rally_set = set()  # 已设置集结点的兵营塔 ID
        self._low_lives_since = None  # 低血量开始的 tick
        self._locked_towers = set()  # 当前关卡锁定的 T4 塔模板
        self._special_towers_logged = False  # 是否已打印过特殊塔分析
        self._active_paths = set()  # 当前已知活跃的路径（有出怪的路径）
        self._last_pick_fail_holder = None  # 上次选型失败的 holder id，避免重复日志
        self._path_totals = {}  # 每条路径的总节点数 {path_index: total}
        self._result = None  # 对战结果: "win"/"lose"/"timeout"/"abort"
        self._event_callback = event_callback  # Lumi 事件推送回调
        self._life_lost_events = []  # 累积扣命事件（来自 Bridge）
        self._opening_plan = None   # LLM 选定的开局方案
        self._battle_history = battle_history or {}  # 对战历史引用
        self._llm_plan_ready = False  # LLM 决策是否已完成
        self._star_goal = star_goal  # 刷星目标（None=首次推图，3=刷三星）

    def _push_event(self, text, event=""):
        """推送事件给 Lumi Bridge"""
        if self._event_callback:
            self._event_callback("game_event", {"text": text, "event": event})

    def _print(self, msg):
        """同时输出到终端和日志缓冲"""
        print(msg)
        self._current_prints.append(msg)

    def _log_action(self, action_type, detail):
        """记录一条动作到当前 tick"""
        self._current_actions.append({"type": action_type, "detail": detail})

    def _log_tick(self, state):
        """记录一个 tick 的状态和动作"""
        entry = {
            "tick": self.tick_count,
            "time": datetime.now().strftime("%H:%M:%S"),
            "gold": state.get("gold", 0),
            "lives": state.get("lives", 0),
            "wave": state.get("wave", 0),
            "wave_total": state.get("wave_total", 0),
            "towers": len(state.get("towers", [])),
            "enemies": len(state.get("enemies", [])),
            "actions": list(self._current_actions),
            "prints": list(self._current_prints),
        }
        self.log_entries.append(entry)
        self._current_actions.clear()
        self._current_prints.clear()

    def save_log(self):
        """保存日志到文件"""
        os.makedirs("logs", exist_ok=True)
        ts = self.start_time.strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"logs/kr_ai_{ts}.log"

        duration = (datetime.now() - self.start_time).total_seconds()
        # 统计结果
        last = self.log_entries[-1] if self.log_entries else {}
        final_lives = last.get("lives", "?")
        final_wave = last.get("wave", "?")
        total_actions = sum(len(e.get("actions", [])) for e in self.log_entries)

        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"Kingdom Rush AI 对战日志\n")
            f.write(f"{'=' * 50}\n")
            f.write(f"开始时间: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"持续时间: {duration:.0f}秒\n")
            f.write(f"总Tick数: {self.tick_count}\n")
            f.write(f"总动作数: {total_actions}\n")
            f.write(f"最终状态: 命={final_lives} 波={final_wave}/{last.get('wave_total', '?')}\n")
            f.write(f"模式: {'试运行' if self.dry_run else '实战'}\n")
            f.write(f"Bridge版本: {self.bridge_version}\n")
            f.write(f"{'=' * 50}\n\n")

            for entry in self.log_entries:
                actions = entry.get("actions", [])
                prints = entry.get("prints", [])
                f.write(f"[Tick {entry['tick']}] {entry['time']} | "
                        f"金:{entry['gold']} 命:{entry['lives']} "
                        f"波:{entry['wave']}/{entry['wave_total']} "
                        f"塔:{entry['towers']} 怪:{entry['enemies']}\n")
                # 写结构化动作（保持老格式兼容）
                for a in actions:
                    f.write(f"  → {a['type']}: {a['detail']}\n")
                # 写额外终端输出（诊断信息、分析等，跳过已被 actions 覆盖的行）
                action_tags = {"[建塔]", "[升级]", "[英雄]", "[火雨]", "[增援]", "[集结]",
                               "[出波]", "[试运行]"}
                for line in prints:
                    stripped = line.strip()
                    # 跳过 action 已包含的行（避免重复）
                    if any(stripped.startswith(tag) for tag in action_tags):
                        continue
                    # 跳过状态摘要行（文件头已包含同样信息）
                    if stripped.startswith("[Tick "):
                        continue
                    f.write(f"{line}\n")
                if not actions and not prints:
                    f.write(f"  (无动作)\n")
                f.write("\n")

        self._print(f"\n  日志已保存: {filename}")
        return filename

    def get_battle_record(self):
        """返回本局对战记录，供历史持久化使用"""
        last = self.log_entries[-1] if self.log_entries else {}
        final_lives = last.get("lives", 0)
        result = self._result or "unknown"
        # 星数估算：20命满星3，扣命≤4为2星，否则1星；失败0星
        if result == "win":
            if final_lives >= 18:
                stars = 3
            elif final_lives >= 16:
                stars = 2
            else:
                stars = 1
        else:
            stars = 0
        return {
            "result": result,
            "level_idx": self.level_idx,
            "level_mode": self.level_mode,
            "stars": stars,
            "final_wave": last.get("wave", 0),
            "wave_total": last.get("wave_total", 0),
            "final_lives": final_lives,
            "life_lost_log": self._life_lost_events,
            "opening_plan": self._opening_plan,
            "strategy_label": getattr(self, '_opening_plan_label', ''),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def generate_opening_plans(self, state, history_records=None):
        """根据当前地图数据和策略模板，生成多套开局候选方案。

        返回列表，每项包含 label/description/build_sequence/remaining_gold/warning。
        history_records: 该关卡的对战历史记录列表，用于标记重复方案。
        """
        gold = state.get("gold", 0)
        towers = list(state.get("towers", []))
        holders = state.get("holders", [])
        empty_holders = [h for h in holders
                         if not h.get("blocked") and not h.get("unblock_price")
                         and h.get("template", "").startswith("tower_holder")]

        max_build = max(2, len(empty_holders) // 3)
        wave_analysis = self.analyze_next_wave(state)

        # 统计现有塔类型
        type_counts = {}
        for t in towers:
            tt = t.get("type", "")
            if t.get("is_special"):
                type_counts[tt] = type_counts.get(tt, 0) + 0.5
            else:
                type_counts[tt] = type_counts.get(tt, 0) + 1

        # 按承压分排序塔位
        sorted_holders = sorted(
            empty_holders,
            key=lambda h: self._calc_holder_pressure(h, wave_analysis),
            reverse=True)

        plans = []
        seen_sequences = []  # 用于模板间去重

        for template in STRATEGY_TEMPLATES:
            budget = gold
            counts = dict(type_counts)  # 拷贝
            build_seq = []

            for i, holder in enumerate(sorted_holders[:max_build]):
                tower_type = self._pick_tower_for_position(
                    holder, counts, int(budget), wave_analysis,
                    log=False, towers=towers, strategy_bias=template["bias"])
                if not tower_type or budget < BUILD_COSTS.get(tower_type, 999):
                    continue
                cost = BUILD_COSTS[tower_type]
                paths_str = ",".join(str(p) for p in holder.get("nearby_paths", []))
                pressure = self._calc_holder_pressure(holder, wave_analysis)
                build_seq.append({
                    "holder_id": holder["id"],
                    "tower_type": tower_type,
                    "cost": cost,
                    "reason": f"承压={pressure:.1f} 路径=[{paths_str}]",
                })
                budget -= cost
                counts[tower_type] = counts.get(tower_type, 0) + 1

            # 序列签名（用于去重）
            sig = tuple((b["holder_id"], b["tower_type"]) for b in build_seq)

            # 模板间去重：与已生成的方案相同则跳过
            if sig in seen_sequences:
                continue
            seen_sequences.append(sig)

            # 与历史失败记录比对，标记警告
            warning = None
            if history_records:
                for rec in history_records:
                    if rec.get("result") != "lose":
                        continue
                    hist_plan = rec.get("opening_plan")
                    if not hist_plan:
                        continue
                    # 提取历史方案的签名
                    hist_sig = tuple((b["holder_id"], b["tower_type"])
                                     for b in hist_plan if isinstance(b, dict))
                    if sig == hist_sig:
                        fw = rec.get("final_wave", "?")
                        wt = rec.get("wave_total", "?")
                        warning = f"与历史失败记录相同（第{fw}/{wt}波失败）"
                        break

            plans.append({
                "label": template["label"],
                "description": template["description"],
                "build_sequence": build_seq,
                "remaining_gold": int(budget),
                "warning": warning,
            })

        return plans

    def format_plans_for_llm(self, plans):
        """将候选方案格式化为文本，供 LLM 阅读选择"""
        lines = []
        for i, plan in enumerate(plans):
            marker = chr(ord('A') + i)
            warn = f" ⚠️ {plan['warning']}" if plan.get("warning") else ""
            lines.append(f"方案{marker}【{plan['label']}】：{plan['description']}{warn}")
            for step in plan["build_sequence"]:
                lines.append(f"  - {step['holder_id']}({step['reason']}) → "
                             f"{cn_type(step['tower_type'])} ¥{step['cost']}")
            lines.append(f"  剩余金币: ¥{plan['remaining_gold']}")
            lines.append("")
        return "\n".join(lines)

    def _do_llm_opening_decision(self, state):
        """执行 LLM 开局策略决策：生成方案 → 调用 LLM → 设置 _opening_plan"""
        self._print("\n  === LLM 开局策略决策 ===")

        # 获取该关卡历史记录
        history = get_level_history(
            self._battle_history, self.level_idx, self.level_mode
        ) if self.level_idx else []

        # 生成候选方案
        plans = self.generate_opening_plans(state, history)
        if not plans:
            self._print("  [LLM策略] 无法生成候选方案，跳过 LLM 决策")
            return

        plans_text = self.format_plans_for_llm(plans)
        self._print(f"  [LLM策略] 生成 {len(plans)} 个候选方案:")
        self._print(plans_text)

        # 下一波预览摘要
        wave_analysis = self.analyze_next_wave(state)
        nw_summary = wave_analysis.get("summary", "") if wave_analysis else ""

        # 生成空塔位描述
        holders = state.get("holders", [])
        empty_holders = [h for h in holders
                         if not h.get("blocked") and not h.get("unblock_price")
                         and h.get("template", "").startswith("tower_holder")]
        holders_lines = []
        for h in empty_holders:
            paths = ",".join(str(p) for p in h.get("nearby_paths", []))
            active_count = sum(1 for p in h.get("nearby_paths", []) if p in self._active_paths)
            junction = "路口" if active_count >= 2 else "单路"
            pressure = self._calc_holder_pressure(h, wave_analysis)
            holders_lines.append(f"- {h['id']} ({junction}, 覆盖路径[{paths}], 承压={pressure:.1f})")
        holders_desc = "\n".join(holders_lines) if holders_lines else None

        # 调用 LLM
        chosen, reasoning, elapsed = call_llm_for_strategy(
            level_idx=self.level_idx or 0,
            level_mode=self.level_mode,
            plans=plans,
            plans_text=plans_text,
            history_records=history,
            next_wave_summary=nw_summary,
            holders_desc=holders_desc,
            star_goal=self._star_goal,
        )

        # 保存选定方案
        self._opening_plan = chosen.get("build_sequence", [])
        self._opening_plan_label = chosen.get("label", "")

        self._push_event(
            f"我决定这局走{self._opening_plan_label}路线！{reasoning}",
            "strategy_chosen")
        self._print(f"  [LLM策略] 决策完成 ({elapsed:.1f}s)")

    # ========== 状态获取 ==========

    def get_state(self):
        """获取游戏状态，静默模式（不打印）"""
        result = self.bot.send_and_receive({"action": "get_state"})
        if result and result.get("type") == "game_state":
            return result
        return None

    # ========== 动作封装 ==========

    def do_build(self, holder_id, tower_type, reason=""):
        tag = "[试运行]" if self.dry_run else "[建塔]"
        msg = f"塔位{holder_id} 建造{cn_type(tower_type)}(¥{BUILD_COSTS[tower_type]})"
        self._print(f"  {tag} {msg}  ({reason})")
        self._log_action("建塔", f"{msg} | {reason}")
        # 开局布阵阶段不推建塔事件，避免挤掉策略选择事件
        if not self._llm_plan_ready:
            self._push_event(f"建造{cn_type(tower_type)} ({reason})", "tower_built")
        if not self.dry_run:
            return self.bot.build_tower(holder_id, tower_type)
        return None

    def do_upgrade(self, tower_id, target, cost, reason=""):
        tag = "[试运行]" if self.dry_run else "[升级]"
        msg = f"塔{tower_id} → {cn_template(target)}(¥{cost})"
        self._print(f"  {tag} {msg}  ({reason})")
        self._log_action("升级", f"{msg} | {reason}")
        # 只有升到终极塔(T4)才推事件，且开局阶段不推（避免挤掉策略事件）
        _T4_TEMPLATES = {"tower_ranger", "tower_musketeer", "tower_paladin", "tower_barbarian",
                         "tower_arcane_wizard", "tower_sorcerer", "tower_tesla", "tower_bfg"}
        if target in _T4_TEMPLATES and not self._llm_plan_ready:
            self._push_event(f"升级→{cn_template(target)}", "tower_upgraded")

        if not self.dry_run:
            return self.bot.upgrade_tower(tower_id, target)
        return None

    def do_move_hero(self, x, y, reason=""):
        tag = "[试运行]" if self.dry_run else "[英雄]"
        msg = f"移动到({x:.0f},{y:.0f})"
        self._print(f"  {tag} {msg}  ({reason})")
        self._log_action("英雄", f"{msg} | {reason}")
        if not self.dry_run:
            return self.bot.move_hero(x, y)
        return None

    def do_use_power(self, power, x, y, reason=""):
        name = "火雨" if power == 1 else "增援"
        self._push_event(f"释放{name} ({reason})", "power_used")
        if self.dry_run:
            msg = f"{name}→游戏坐标({x:.0f},{y:.0f})"
            self._print(f"  [试运行] {msg}  ({reason})")
            self._log_action(name, f"{msg} | {reason}")
            return None
        result = self.bot.use_power(power, x, y)
        if result and result.get("type") == "ok":
            msg = f"{name}→({x:.0f},{y:.0f})"
            self._print(f"  [{name}] {msg}  ({reason})")
            self._log_action(name, f"{msg} | {reason}")
        elif result and result.get("type") == "error":
            err = result.get("message", "")
            if "cooldown" not in err and "locked" not in err:
                self._print(f"  [{name}] 失败: {err}")
                self._log_action(f"{name}失败", err)
        return result

    def do_set_rally(self, tower_id, x, y, reason=""):
        tag = "[试运行]" if self.dry_run else "[集结]"
        msg = f"塔{tower_id}集结点→({x:.0f},{y:.0f})"
        self._print(f"  {tag} {msg}  ({reason})")
        if not self.dry_run:
            result = self.bot.set_rally_point(tower_id, x, y)
            if result and result.get("type") == "ok":
                diag = result.get("diag", {})
                self._log_action("集结", f"{msg} | {reason}")
                # 打印诊断
                self._print(f"    [诊断] 模板={diag.get('tower_template')} "
                      f"塔位置={diag.get('tower_pos')} "
                      f"距离={diag.get('dist_to_target','?'):.1f} "
                      f"rally_range={diag.get('rally_range', '?')}")
                self._print(f"    [诊断] rally前={diag.get('rally_before')} → "
                      f"rally后={diag.get('rally_after')} "
                      f"rally_new={diag.get('rally_new_set')}")
                # 士兵状态
                sb = diag.get("soldiers_before", {})
                sa = diag.get("soldiers_after", {})
                for k in sorted(set(list(sb.keys()) + list(sa.keys()))):
                    b = sb.get(k, {})
                    a = sa.get(k, {})
                    a_rally = a.get('rally', '?')
                    a_new = a.get('rally_new', '?')
                    self._print(f"      兵{k}: 前rally={b.get('rally','?')} → "
                          f"后rally={a_rally} new={a_new}")
            elif result:
                self._print(f"    [失败] {result.get('message', '?')}")
                self._log_action("集结失败", f"{msg} | {result.get('message', '?')}")
            else:
                self._log_action("集结失败", f"{msg} | 无响应")
            return result
        self._log_action("集结", f"{msg} | {reason}")
        return None

    def do_send_wave(self, reason=""):
        tag = "[试运行]" if self.dry_run else "[出波]"
        self._print(f"  {tag} 出波  ({reason})")
        self._log_action("出波", reason)
        self._push_event(f"提前出波 ({reason})", "send_wave")
        if not self.dry_run:
            return self.bot.send_wave()
        return None

    def dismiss_popups(self):
        """自动关闭教程/提示弹窗"""
        result = self.bot.send_and_receive({"action": "dismiss_popups"})
        if result and result.get("type") == "ok":
            dismissed = result.get("dismissed", 0)
            if dismissed > 0:
                self._print(f"  [关闭] {dismissed}个提示弹窗")
                self._log_action("关闭弹窗", f"{dismissed}个")

    # ========== 分析工具 ==========

    def find_most_forward_enemy(self, enemies):
        """找路径进度最大的敌人（最接近终点）"""
        if not enemies:
            return None
        return max(enemies, key=lambda e: e.get("path_ni", 0))

    def find_most_dangerous_enemy(self, enemies):
        """找最危险的敌人：按威胁度评分（接近终点 + lives_cost）"""
        if not enemies:
            return None
        return max(enemies, key=lambda e: self._enemy_threat(e))

    def _enemy_threat(self, enemy):
        """敌人威胁度：路径进度占比 × lives_cost。进度越接近终点越危险。"""
        ni = enemy.get("path_ni", 0)
        pi = enemy.get("path_index")
        total = self._get_path_total(pi) if pi is not None else 1
        progress = ni / total if total > 0 else 0  # 0~1, 越大越接近终点
        lives_cost = enemy.get("lives_cost", 1)
        return progress * 10 + lives_cost

    def _get_path_total(self, path_index):
        """获取路径总节点数（缓存在 _path_totals 中）"""
        return self._path_totals.get(path_index, 100)  # 默认100

    def _is_high_threat(self, enemy):
        """判断敌人是否高威胁：接近终点（进度>60%）或 lives_cost≥3"""
        ni = enemy.get("path_ni", 0)
        pi = enemy.get("path_index")
        total = self._get_path_total(pi) if pi is not None else 1
        progress = ni / total if total > 0 else 0
        if progress > 0.6:
            return True
        if enemy.get("lives_cost", 1) >= 3:
            return True
        return False

    def find_enemy_cluster(self, enemies, radius=80):
        """找敌人密集区域，返回 (中心敌人x, 中心敌人y, 数量)
        使用邻居最多的那个敌人的实际坐标（保证在路径上），不用质心（可能落在两条路径中间空地）。
        """
        if not enemies:
            return None
        best_enemy = None
        best_count = 0
        for e in enemies:
            ex, ey = e.get("x", 0), e.get("y", 0)
            count = sum(1 for o in enemies if dist(ex, ey, o.get("x", 0), o.get("y", 0)) < radius)
            if count > best_count:
                best_count = count
                best_enemy = e
        if best_enemy:
            return (best_enemy.get("x", 0), best_enemy.get("y", 0), best_count)
        return None

    def count_nearby_enemies(self, enemies, x, y, radius=200):
        """计算某坐标附近敌人数量（用于评估塔位价值）"""
        return sum(1 for e in enemies if dist(x, y, e.get("x", 0), e.get("y", 0)) < radius)

    # ========== 决策逻辑 ==========

    def prepare_tick(self, state):
        """
        开战前准备阶段 (wave==0)，每 tick 执行一个动作：
        - 每 tick 建一座塔或升一级（从游戏读最新金币，不会双重扣款）
        - 建完目标数量后移动英雄、出波
        策略：建约 1/3 空位的塔，剩余金币升级
        """
        gold = state.get("gold", 0)
        towers = list(state.get("towers", []))
        holders = state.get("holders", [])
        heroes = state.get("heroes", [])
        empty_holders = [h for h in holders
                         if not h.get("blocked") and not h.get("unblock_price")
                         and h.get("template", "").startswith("tower_holder")]

        # 初始化准备计划（只在第一次 tick 时计算）
        if not hasattr(self, '_prep_max_build'):
            if self._opening_plan:
                self._prep_max_build = len(self._opening_plan)
            else:
                self._prep_max_build = max(2, len(empty_holders) // 3)
            self._prep_built = 0
            self._prep_upgraded = 0
            self._prep_hero_moved = False
            self._print(f"\n  === 开战前准备阶段 ===")
            plan_src = f"LLM方案【{getattr(self, '_opening_plan_label', '')}】" if self._opening_plan else "算法决策"
            self._print(f"  金币: ¥{gold} | 空位: {len(empty_holders)} | 计划建塔: {self._prep_max_build} ({plan_src})")

        # 统计塔类型（包含特殊塔，影响建塔类型选择）
        type_counts = {}
        for t in towers:
            tt = t.get("type", "")
            if t.get("is_special"):
                # 特殊塔算半个（性价比低，权重降低）
                type_counts[tt] = type_counts.get(tt, 0) + 0.5
            else:
                type_counts[tt] = type_counts.get(tt, 0) + 1

        # 分析下一波敌人抗性
        wave_analysis = self.analyze_next_wave(state)

        # 阶段1：每 tick 建一座塔
        # 有 LLM 方案时按方案执行，否则走算法决策
        if self._opening_plan and self._prep_built < len(self._opening_plan):
            # === LLM 方案模式 ===
            step = self._opening_plan[self._prep_built]
            holder_id = step.get("holder_id")
            tower_type = step.get("tower_type")
            cost = BUILD_COSTS.get(tower_type, 0)
            if tower_type and gold >= cost:
                self.do_build(holder_id, tower_type,
                              f"方案({self._prep_built+1}/{len(self._opening_plan)}) "
                              f"策略={self._opening_plan_label} {step.get('reason', '')}")
                self._prep_built += 1
                return  # 本 tick 只做一件事
        elif not self._opening_plan:
            # === 算法决策模式（兜底/无 LLM 时） ===
            min_reserve = 110
            if self._prep_built < self._prep_max_build and empty_holders:
                budget = gold - min_reserve if gold > min(BUILD_COSTS.values()) + min_reserve else gold
                # 按承压分排序：活跃入口近的路口优先
                empty_holders.sort(
                    key=lambda h: self._calc_holder_pressure(h, wave_analysis),
                    reverse=True)
                holder = empty_holders[0]
                pressure = self._calc_holder_pressure(holder, wave_analysis)
                tower_type = self._pick_tower_for_position(holder, type_counts, int(budget), wave_analysis, towers=towers)
                if tower_type and budget >= BUILD_COSTS[tower_type]:
                    paths_str = ",".join(str(p) for p in holder.get("nearby_paths", []))
                    self.do_build(holder["id"], tower_type,
                                  f"准备({self._prep_built+1}/{self._prep_max_build}) "
                                  f"承压={pressure:.2f} 覆盖={holder.get('path_score', 0)} 路径=[{paths_str}]")
                    self._prep_built += 1
                    return  # 本 tick 只做一件事

        # 阶段2：每 tick 升一级
        if gold >= 110:
            upgradable = []
            for t in towers:
                if t.get("is_special"):
                    continue
                template = t.get("template", "")
                if template in UPGRADE_PATH:
                    target, cost = UPGRADE_PATH[template]
                    if target in self._locked_towers:
                        continue
                    if cost <= gold:
                        upgradable.append((t, target, cost))
            if upgradable:
                # 准备阶段优先升DPS塔（开局需要输出），同分按覆盖度和费用排
                def _prep_upgrade_priority(item):
                    t = item[0]
                    is_dps = 1 if t.get("type") in DPS_TOWER_TYPES else 0
                    return (-is_dps, -t.get("path_score", 0), item[2])
                upgradable.sort(key=_prep_upgrade_priority)
                t, target, cost = upgradable[0]
                self.do_upgrade(t["id"], target, cost,
                                f"准备升级 {cn_template(t['template'])} 覆盖={t.get('path_score', 0)}")
                self._prep_upgraded += 1
                return  # 本 tick 只做一件事

        # 阶段3：英雄移到前线
        if not self._prep_hero_moved and heroes:
            all_holders = state.get("holders", holders)
            if all_holders:
                xs = [h.get("x", 0) for h in all_holders]
                ys = [h.get("y", 0) for h in all_holders]
                center_x = sum(xs) / len(xs)
                front_y = min(ys) + (max(ys) - min(ys)) * 0.35
                self.do_move_hero(center_x, front_y, "前线待命")
            self._prep_hero_moved = True
            return

        # 全部完成 → 出波
        self._print(f"  准备完成: 建{self._prep_built}塔, 升级{self._prep_upgraded}次, 剩余¥{gold}")
        self.ensure_rally_points(state)
        self.do_send_wave("准备完成，开战！")
        self._prep_done = True

    def decide_tower_actions(self, state):
        """战斗中决定建塔/升级动作。每个 tick 最多执行一个。"""
        gold = state.get("gold", 0)
        towers = state.get("towers", [])
        holders = state.get("holders", [])
        enemies = state.get("enemies", [])
        empty_holders = [h for h in holders
                         if not h.get("blocked") and not h.get("unblock_price")
                         and h.get("template", "").startswith("tower_holder")]

        # 分析下一波敌人抗性
        wave_analysis = self.analyze_next_wave(state)

        # 统计现有塔类型（特殊塔算半个，性价比低）
        type_counts = {}
        for t in towers:
            tt = t.get("type", "")
            if t.get("is_special"):
                type_counts[tt] = type_counts.get(tt, 0) + 0.5
            else:
                type_counts[tt] = type_counts.get(tt, 0) + 1

        # 策略：先铺基础塔覆盖路径，再升级
        # 当空位多（>一半）时优先建塔；空位少时优先升级
        prefer_upgrade = len(empty_holders) <= len(holders) // 2 if holders else True

        # 1) 建新塔 (空位较多时优先，或者金币充足时也建)
        min_build_cost = min(BUILD_COSTS.values())
        if empty_holders and gold >= min_build_cost and (not prefer_upgrade or gold >= 200):
            # 按承压分排序：活跃入口近的路口优先
            empty_holders.sort(
                key=lambda h: self._calc_holder_pressure(h, wave_analysis),
                reverse=True)
            best_holder = empty_holders[0]
            pressure = self._calc_holder_pressure(best_holder, wave_analysis)
            # 只在首次或 holder 变化时打印选型日志，避免每tick重复
            should_log = best_holder["id"] != self._last_pick_fail_holder
            tower_type = self._pick_tower_for_position(best_holder, type_counts, gold, wave_analysis, log=should_log, towers=towers)
            if tower_type and gold >= BUILD_COSTS[tower_type]:
                self._last_pick_fail_holder = None
                paths_str = ",".join(str(p) for p in best_holder.get("nearby_paths", []))
                self.do_build(best_holder["id"], tower_type,
                              f"空位{len(empty_holders)}个 承压={pressure:.2f} "
                              f"覆盖={best_holder.get('path_score', 0)} 路径=[{paths_str}]")
                return True
            else:
                self._last_pick_fail_holder = best_holder["id"]

        # 2) 升级已有塔 — 集中升满一座再升下一座
        if prefer_upgrade:
            best = self._pick_best_upgrade(towers, gold, wave_analysis)
            if best:
                t, target, cost = best
                # 存钱逻辑：如果最优目标当前买不起，不要浪费金币升别的
                if cost > gold:
                    return False  # 攒钱等待
                paths_str = ",".join(str(p) for p in t.get("nearby_paths", []))
                self.do_upgrade(t["id"], target, cost,
                                f"{cn_template(t['template'])}升级 覆盖={t.get('path_score', 0)} "
                                f"路径=[{paths_str}]")
                return True

        return False

    def _get_tower_level(self, template):
        """获取塔等级: 1/2/3/4, 4级后的技能算5"""
        if not template:
            return 0
        if "_1" in template:
            return 1
        if "_2" in template:
            return 2
        if "_3" in template:
            return 3
        # 四级分叉塔（游侠/蛮族/奥术/特斯拉等）
        if template in ("tower_ranger", "tower_musketeer", "tower_paladin",
                        "tower_barbarian", "tower_arcane_wizard", "tower_sorcerer",
                        "tower_tesla", "tower_bfg"):
            return 4
        return 0

    def _pick_best_upgrade(self, towers, gold, wave_analysis):
        """选择最优升级目标。
        策略：高覆盖路口的塔一路升满到IV级 → 克制敌人类型优先
        路口兵营T1且附近有T4塔 → 紧急升级（坦度不足）
        返回 (tower, target_template, cost) 或 None（表示应该存钱）
        """
        # 收集所有可升级的塔（不管当前金币够不够）
        all_upgradable = []
        for t in towers:
            # 跳过关卡特殊塔（不可升级/拆除）
            if t.get("is_special"):
                continue
            template = t.get("template", "")
            if template in UPGRADE_PATH:
                target, cost = UPGRADE_PATH[template]
                # 检查目标是否被当前关卡锁定
                if target in self._locked_towers:
                    continue
                all_upgradable.append((t, target, cost))

        if not all_upgradable:
            return None

        per_path = wave_analysis.get("per_path", {}) if wave_analysis else {}

        # 塔类型升级优先权重
        TYPE_WEIGHT = {"archer": 20, "mage": 20, "engineer": 15, "barrack": 10}

        def upgrade_score(item):
            t, target, cost = item
            level = self._get_tower_level(t.get("template", ""))
            t_type = t.get("type", "")
            pressure = self._calc_holder_pressure(t, wave_analysis)

            # 1) 承压分 — 最核心指标（距活跃入口近的塔优先升级）
            score = pressure * 50

            # 2) 已有等级越高越优先升（集中升满一座）
            score += level * 30

            # 3) 塔类型权重
            score += TYPE_WEIGHT.get(t_type, 0)

            # 4) 克制加分（按路径距离加权）
            nearby = t.get("nearby_paths", [])
            path_distances = t.get("path_distances", {})
            for pi in nearby:
                if pi not in self._active_paths:
                    continue
                pd_data = per_path.get(pi, {})
                w = self._calc_path_weight(path_distances, pi)
                if t_type in PHYSICAL_TOWER_TYPES:
                    score += pd_data.get("prefer_physical", 0) * w * 25
                elif t_type in MAGICAL_TOWER_TYPES:
                    score += pd_data.get("prefer_magic", 0) * w * 25

            # 5) 路口兵营T1且附近有T4塔 → 紧急升级（坦度跟不上火力）
            if t_type == "barrack" and level == 1:
                active_nearby = sum(1 for pi in nearby if pi in self._active_paths)
                if active_nearby >= 2:  # 路口兵营
                    has_t4_nearby = any(
                        self._get_tower_level(ot.get("template", "")) >= 4
                        and dist(t.get("x", 0), t.get("y", 0),
                                 ot.get("x", 0), ot.get("y", 0)) < 200
                        for ot in towers if ot["id"] != t["id"]
                    )
                    if has_t4_nearby:
                        score += 40  # 紧急提升优先级

            return score

        # 按评分排序
        all_upgradable.sort(key=lambda x: -upgrade_score(x))
        best = all_upgradable[0]

        # 最优目标买不起 → 返回它（让调用方存钱）
        # 买得起 → 直接返回
        return best

    def analyze_next_wave(self, state):
        """分析下一波敌人的抗性构成，返回建塔建议。
        返回: {
            "prefer_magic": float,   # 0~1, 越高越需要法伤
            "prefer_physical": float, # 0~1, 越高越需要物伤
            "is_swarm": bool,         # 群怪波（数量多血少）
            "per_path": {path_index: {"prefer_magic": float, ...}, ...},
            "summary": str,           # 日志摘要
        }
        """
        nw = state.get("next_wave")
        if not nw or not nw.get("paths"):
            return None

        total_physical_need = 0  # 需要物伤的权重（敌人有法抗）
        total_magical_need = 0   # 需要法伤的权重（敌人有物抗）
        total_count = 0
        total_hp = 0
        per_path = {}
        summary_parts = []
        enemy_types = {}  # {template: count} 用于口语化描述

        for path_info in nw["paths"]:
            pi = path_info.get("path_index", 0)
            path_phys = 0
            path_magic = 0
            path_count = 0

            for sp in path_info.get("spawns", []):
                count = sp.get("count", 1)
                armor = sp.get("armor", 0)
                magic_armor = sp.get("magic_armor", 0)
                hp = sp.get("hp", 0)
                template = sp.get("template", "?")

                path_count += count
                total_count += count
                total_hp += hp * count
                enemy_types[template] = enemy_types.get(template, 0) + count

                # 有物抗 → 需要法伤来打
                if armor > 0:
                    total_magical_need += count * (1 + armor / 10)
                    path_magic += count
                # 有法抗 → 需要物伤来打
                if magic_armor > 0:
                    total_physical_need += count * (1 + magic_armor / 10)
                    path_phys += count
                # 无抗性 → 两种都行，不计入偏好

                armor_str = ""
                if armor > 0:
                    armor_str += f"物抗{armor}"
                if magic_armor > 0:
                    armor_str += f"法抗{magic_armor}"
                if not armor_str:
                    armor_str = "无抗"
                summary_parts.append(f"路{pi}:{template}×{count}({armor_str})")

            per_path[pi] = {
                "prefer_magic": path_magic / max(1, path_count),
                "prefer_physical": path_phys / max(1, path_count),
                "count": path_count,
            }

        total = total_magical_need + total_physical_need
        prefer_magic = total_magical_need / total if total > 0 else 0.5
        prefer_physical = total_physical_need / total if total > 0 else 0.5

        # 群怪判定：数量>=8 且平均HP较低
        avg_hp = total_hp / max(1, total_count)
        is_swarm = total_count >= 8 and avg_hp < 100

        return {
            "prefer_magic": prefer_magic,
            "prefer_physical": prefer_physical,
            "is_swarm": is_swarm,
            "per_path": per_path,
            "total_count": total_count,
            "enemy_types": enemy_types,
            "summary": " | ".join(summary_parts),
        }

    def _calc_path_weight(self, path_distances, pi):
        """计算塔位在某条路径上的距离权重。
        距离入口越近 → 承压越大 → 权重越高（0~1）。
        path_distances 可能是 dict（key=str(pi)）或 list（index=pi-1），取决于 JSON 序列化。
        """
        pd_entry = None
        if isinstance(path_distances, dict):
            pd_entry = path_distances.get(str(pi)) or path_distances.get(pi)
        elif isinstance(path_distances, list):
            # Lua 数组序列化：pi=1 → index 0 或 index 1（取决于是否有 null 填充）
            # 尝试 pi-1（0-based）和 pi（1-based with null at 0）
            for idx in (pi - 1, pi):
                if 0 <= idx < len(path_distances) and path_distances[idx]:
                    pd_entry = path_distances[idx]
                    break
        if not pd_entry or not isinstance(pd_entry, dict):
            return 0.0
        ni = pd_entry.get("ni", 0)
        total = pd_entry.get("total", 1)
        if total <= 0:
            return 0.0
        return max(0.0, 1.0 - ni / total)

    def _calc_holder_pressure(self, holder, wave_analysis=None):
        """计算塔位的实际承压分。
        承压分 = Σ (路径活跃 ? 1 : 0) × f(路径距离)
        f(路径距离) = 1 - node_index / total_nodes（入口=1, 终点=0）
        如果没有活跃路径信息，fallback 到 path_score（兼容旧逻辑）。
        """
        path_distances = holder.get("path_distances", {})
        nearby_paths = holder.get("nearby_paths", [])

        # 没有路径距离数据或没有活跃路径 → fallback 到 path_score
        if not path_distances or not self._active_paths:
            return holder.get("path_score", 0) * 0.1  # 缩放到相近量级

        score = 0.0
        for pi in nearby_paths:
            if pi not in self._active_paths:
                continue  # 非活跃路径，不计入承压
            w = self._calc_path_weight(path_distances, pi)
            score += w
        return score

    def _pick_tower_type(self, type_counts, gold, wave_analysis=None):
        """选择建什么类型的塔，考虑敌人抗性"""
        archer_count = type_counts.get("archer", 0)
        barrack_count = type_counts.get("barrack", 0)
        mage_count = type_counts.get("mage", 0)

        # 有波次分析时，根据抗性调整选择
        if wave_analysis:
            pm = wave_analysis["prefer_magic"]
            pp = wave_analysis["prefer_physical"]

            # 群怪 → 炮塔（AOE）
            if wave_analysis["is_swarm"] and gold >= BUILD_COSTS.get("engineer", 125):
                return "engineer"

            # 强烈偏向法伤（>60% 的敌人有物抗）
            if pm > 0.6 and gold >= BUILD_COSTS["mage"]:
                return "mage"

            # 强烈偏向物伤（>60% 的敌人有法抗）
            if pp > 0.6 and gold >= BUILD_COSTS["archer"]:
                return "archer"

        # 默认比例目标：弓箭:兵营:法师 ≈ 3:1:1
        if gold >= BUILD_COSTS["archer"] and archer_count <= barrack_count * 2:
            return "archer"
        if gold >= BUILD_COSTS["barrack"] and barrack_count < max(1, archer_count // 3):
            return "barrack"
        if gold >= BUILD_COSTS["mage"] and mage_count < max(1, archer_count // 3):
            return "mage"
        if gold >= BUILD_COSTS["archer"]:
            return "archer"
        if gold >= BUILD_COSTS["barrack"]:
            return "barrack"
        return None

    def _pick_tower_for_position(self, holder, type_counts, gold, wave_analysis,
                                 log=True, towers=None, strategy_bias=None):
        """根据塔位覆盖的路径和敌人抗性，选择该位置的最佳塔类型。
        核心思路：按路径距离加权 — 距入口近的路径权重大，该路径上的敌人抗性主导选型。
        路口（≥2条活跃路径）物理需求时优先炮塔（AOE），且检查阻挡约束（每条路需有兵营）。
        log=False 时不打印选型诊断（用于试探性调用）。
        """
        nearby_paths = holder.get("nearby_paths", [])
        path_distances = holder.get("path_distances", {})

        if not wave_analysis or not nearby_paths:
            return self._pick_tower_type(type_counts, gold, wave_analysis)

        per_path = wave_analysis.get("per_path", {})

        # 按路径距离加权统计抗性需求
        weighted_magic = 0.0
        weighted_phys = 0.0
        total_weight = 0.0
        diag_parts = []  # 选型诊断信息

        for pi in nearby_paths:
            if pi not in self._active_paths:
                continue  # 非活跃路径不参与选型
            path_data = per_path.get(pi, {})
            count = path_data.get("count", 0)
            if count == 0:
                continue
            # 路径距离权重：距入口越近权重越大
            w = self._calc_path_weight(path_distances, pi)
            if w <= 0:
                continue
            pm = path_data.get("prefer_magic", 0)
            pp = path_data.get("prefer_physical", 0)
            weighted_magic += pm * count * w
            weighted_phys += pp * count * w
            total_weight += count * w
            diag_parts.append(f"路{pi}(距离权重={w:.2f},怪×{count},法需={pm:.1f},物需={pp:.1f})")

        if total_weight == 0:
            result = self._pick_tower_type(type_counts, gold, wave_analysis)
            if log:
                self._print(f"    [选型] 无活跃路径数据 → 默认比例 → {cn_type(result) if result else '无'}")
            return result

        local_magic = weighted_magic / total_weight
        local_phys = weighted_phys / total_weight

        active_nearby = sum(1 for pi in nearby_paths if pi in self._active_paths)
        is_junction = active_nearby >= 2  # 路口：覆盖≥2条活跃路径

        # 群怪 + 路口 → 炮塔（AOE 价值最大化）— 硬约束，不受 bias 影响
        if wave_analysis["is_swarm"] and is_junction and gold >= BUILD_COSTS.get("engineer", 125):
            if log:
                self._print(f"    [选型] {' | '.join(diag_parts)} → 群怪+路口 → 炮塔")
            return "engineer"

        # --- 阻挡约束：检查该塔位覆盖的活跃路径是否都有兵营覆盖 ---
        uncovered_paths = self._find_uncovered_paths(nearby_paths, towers) if towers else []
        if uncovered_paths and gold >= BUILD_COSTS["barrack"]:
            if log:
                self._print(f"    [选型] {' | '.join(diag_parts)} → "
                            f"路径{uncovered_paths}缺兵营阻挡 → 兵营")
            return "barrack"

        # --- 应用策略偏好（strategy_bias）---
        # bias 将法需/物需的阈值做偏移，影响选型倾向
        bias = strategy_bias or {}
        adj_magic = local_magic + bias.get("mage_boost", 0)
        adj_phys = local_phys + bias.get("phys_boost", 0)

        # --- 路口物理需求 → 炮塔优先（替代弓箭） ---
        if adj_magic > 0.5 and gold >= BUILD_COSTS["mage"]:
            reason = f"加权法需={local_magic:.2f}(adj={adj_magic:.2f})>0.5 → 法师塔"
            result = "mage"
        elif adj_phys > 0.5:
            if is_junction and gold >= BUILD_COSTS["engineer"]:
                # 路口物理需求：检查附近是否已有炮塔
                has_artillery = self._junction_has_type(holder, towers, "engineer") if towers else False
                if not has_artillery:
                    reason = f"加权物需={local_phys:.2f}(adj={adj_phys:.2f})>0.5 路口无炮塔 → 炮塔"
                    result = "engineer"
                else:
                    reason = f"加权物需={local_phys:.2f}(adj={adj_phys:.2f})>0.5 路口已有炮塔 → 弓箭塔"
                    result = "archer" if gold >= BUILD_COSTS["archer"] else None
            elif gold >= BUILD_COSTS["archer"]:
                reason = f"加权物需={local_phys:.2f}(adj={adj_phys:.2f})>0.5 → 弓箭塔"
                result = "archer"
            else:
                result = None
                reason = f"加权物需={local_phys:.2f}(adj={adj_phys:.2f})>0.5 金币不足"
        else:
            # 无明确偏向时，用 bias 的 type_preference 直接影响选择
            preferred = bias.get("type_preference")
            if preferred and gold >= BUILD_COSTS.get(preferred, 999):
                result = preferred
                reason = f"法需={local_magic:.2f} 物需={local_phys:.2f} 策略偏好 → {cn_type(result)}"
            else:
                result = self._pick_tower_type(type_counts, gold, wave_analysis)
                reason = f"法需={local_magic:.2f} 物需={local_phys:.2f} 无偏向 → 默认{cn_type(result) if result else '无'}"

        if log:
            self._print(f"    [选型] {' | '.join(diag_parts)} → {reason}")
        return result

    def _find_uncovered_paths(self, nearby_paths, towers):
        """找出 nearby_paths 中没有被任何兵营集结点覆盖的活跃路径。"""
        if not towers:
            return [pi for pi in nearby_paths if pi in self._active_paths]
        # 收集所有兵营覆盖的路径
        barrack_covered = set()
        for t in towers:
            if t.get("type") == "barrack" or self._is_barrack(t.get("template", "")):
                for pi in t.get("nearby_paths", []):
                    barrack_covered.add(pi)
        return [pi for pi in nearby_paths
                if pi in self._active_paths and pi not in barrack_covered]

    def _junction_has_type(self, holder, towers, tower_type, radius=200):
        """检查路口附近（radius 范围内）是否已有指定类型的塔。"""
        if not towers:
            return False
        hx, hy = holder.get("x", 0), holder.get("y", 0)
        for t in towers:
            if t.get("type") != tower_type:
                continue
            tx, ty = t.get("x", 0), t.get("y", 0)
            if (tx - hx) ** 2 + (ty - hy) ** 2 < radius ** 2:
                return True
        return False

    def _pick_holder(self, empty_holders, enemies, tower_type):
        """选择最佳塔位"""
        if not enemies:
            return empty_holders[0]

        if tower_type in DPS_TOWER_TYPES:
            # DPS塔：选附近敌人最多的位置
            return max(empty_holders,
                       key=lambda h: self.count_nearby_enemies(enemies, h.get("x", 0), h.get("y", 0)))
        else:
            # 兵营：选最前方敌人附近的位置
            forward = self.find_most_forward_enemy(enemies)
            if forward:
                return min(empty_holders,
                           key=lambda h: dist(h.get("x", 0), h.get("y", 0),
                                              forward.get("x", 0), forward.get("y", 0)))
            return empty_holders[0]

    def _find_intercept_point(self, enemy, hx, hy, towers):
        """找敌人前方的拦截点：同路径上、在敌人前方20%路程内、离英雄最近的路径点。
        英雄走直线，敌人沿弯曲路径走，选敌人还没到的前方塔位路径点。
        """
        e_pi = enemy.get("path_index")
        e_ni = enemy.get("path_ni", 0)
        if e_pi is None:
            return None

        # 从塔的 path_distances 找同路径上、在敌人前方的点
        candidates = []
        for t in towers:
            pd = t.get("path_distances", {})
            npx = t.get("nearest_path_x")
            npy = t.get("nearest_path_y")
            if not npx or not npy:
                continue
            # 查这个塔在敌人所在路径上的位置
            pd_entry = None
            if isinstance(pd, dict):
                pd_entry = pd.get(str(e_pi)) or pd.get(e_pi)
            elif isinstance(pd, list):
                for idx in (e_pi - 1, e_pi):
                    if 0 <= idx < len(pd) and pd[idx]:
                        pd_entry = pd[idx]
                        break
            if not pd_entry or not isinstance(pd_entry, dict):
                continue
            tower_ni = pd_entry.get("ni", 0)
            total = pd_entry.get("total", 1)
            # 塔在敌人前方（ni更大），且不超过前方20%路程
            max_lookahead = total * 0.2
            if tower_ni > e_ni and (tower_ni - e_ni) <= max_lookahead:
                hero_dist = dist(hx, hy, npx, npy)
                candidates.append((npx, npy, hero_dist, tower_ni))

        if not candidates:
            return None

        # 选离英雄最近的拦截点（英雄能最快到达）
        candidates.sort(key=lambda c: c[2])
        best = candidates[0]
        return (best[0], best[1])

    def decide_hero(self, state):
        """英雄策略：
        优先级1: 附近有敌人 → 不动，继续输出
        优先级2: 路口有敌人聚集 → 去最近的路口驻守
        优先级3: 高威胁敌人 → 拦截（去敌人前方路径点）
        """
        heroes = state.get("heroes", [])
        enemies = state.get("enemies", [])

        if not heroes or not enemies:
            return

        hero = heroes[0]
        if hero.get("dead"):
            return

        now = time.time()
        hx, hy = hero.get("x", 0), hero.get("y", 0)
        nearby_count = self.count_nearby_enemies(enemies, hx, hy, 250)

        # 优先级1: 附近有敌人 → 不动，继续输出
        # 例外：如果有高威胁敌人不在附近，不能被杂兵拖住
        if nearby_count >= 2:
            dangerous = self.find_most_dangerous_enemy(enemies)
            if dangerous and self._is_high_threat(dangerous):
                d_to_danger = dist(hx, hy,
                                   dangerous.get("x", 0), dangerous.get("y", 0))
                if d_to_danger > 250:
                    pass  # 高威胁目标不在附近，不停留，继续往下评估追击
                else:
                    return  # 高威胁目标就在附近，留下打
            else:
                return  # 没有高威胁目标，留下打附近敌人

        # 移动冷却：附近没敌人时缩短到2秒（追击模式），否则5秒
        cooldown = 2 if nearby_count == 0 else 5
        if now - self._last_hero_move_time < cooldown:
            return

        towers = state.get("towers", [])

        # 收集路口驻守位置（塔位附近的路径点）
        path_positions = []
        for t in towers:
            npx = t.get("nearest_path_x")
            npy = t.get("nearest_path_y")
            ps = t.get("path_score", 0)
            if npx and npy and ps > 0:
                path_positions.append((npx, npy, ps))

        should_move = False
        tx, ty = hx, hy
        reason = ""

        # 优先级2: 找有敌人聚集的路口 → 去最近的路口驻守
        cluster = self.find_enemy_cluster(enemies, radius=150)
        if cluster:
            cx, cy, cluster_count = cluster
            cluster_dist = dist(hx, hy, cx, cy)
            if cluster_count >= 3 and cluster_dist > 250:
                # 选离聚集中心最近的路口点
                best_intercept = None
                best_dist = 999999
                best_ps = 0
                for px, py, ps in path_positions:
                    d_to_cluster = dist(cx, cy, px, py)
                    if d_to_cluster < 200:
                        enemies_near = self.count_nearby_enemies(enemies, px, py, 250)
                        if enemies_near >= 2 and d_to_cluster < best_dist:
                            best_dist = d_to_cluster
                            best_intercept = (px, py)
                            best_ps = ps
                if best_intercept:
                    tx, ty = best_intercept
                    reason = f"路口驻守(覆盖={best_ps},聚集{cluster_count},距聚集{best_dist:.0f})"
                    should_move = True

        # 优先级3: 高威胁敌人 → 去敌人前方路径点拦截
        if not should_move:
            dangerous = self.find_most_dangerous_enemy(enemies)
            if dangerous and self._is_high_threat(dangerous):
                dx, dy = dangerous.get("x", 0), dangerous.get("y", 0)
                if dist(hx, hy, dx, dy) > 250:
                    # 找敌人前方的拦截点
                    intercept = self._find_intercept_point(dangerous, hx, hy, towers)
                    if intercept:
                        tx, ty = intercept
                        reason = (f"拦截{cn_enemy(dangerous.get('template', '?'))}"
                                  f"(扣{dangerous.get('lives_cost')}命) "
                                  f"敌({dx:.0f},{dy:.0f})→拦截点({tx:.0f},{ty:.0f})")
                    else:
                        # fallback: 没有前方路径点，直接去敌人位置
                        tx, ty = dx, dy
                        reason = f"追击{cn_enemy(dangerous.get('template', '?'))}(扣{dangerous.get('lives_cost')}命)"
                    should_move = True

        if should_move:
            self.do_move_hero(tx, ty, reason)
            self._last_hero_move_time = now

    def _filter_combat_enemies(self, enemies, min_ni=8):
        """过滤掉刚出生的敌人（path_ni太小，还在地图入口外）"""
        return [e for e in enemies if e.get("path_ni", 0) >= min_ni]

    def decide_powers(self, state):
        """决定技能使用。直接尝试释放，由 bridge 检查 CD。"""
        enemies = state.get("enemies", [])
        if not enemies or state.get("paused"):
            return

        # 过滤刚出生的敌人，只对已进入战场的敌人计算
        combat_enemies = self._filter_combat_enemies(enemies)
        if not combat_enemies:
            return

        # === 火雨：最大化有效伤害 ===
        best_fire = self._find_best_fire_target(combat_enemies)
        if best_fire:
            fx, fy, eff_dmg, direct_count, trail_count, diag = best_fire
            self._print(f"    [火雨诊断] 目标({fx:.0f},{fy:.0f}) "
                        f"有效伤害={eff_dmg:.0f} 直击{direct_count} 火坑{trail_count}: "
                        f"{' | '.join(diag)}")
            self.do_use_power(1, fx, fy,
                              f"有效伤害{eff_dmg:.0f} 直击{direct_count}个 火坑{trail_count}个")

        # === 增援：放在离最前方敌人最近的路口 ===
        towers = state.get("towers", [])
        reinforce_target = self._find_reinforce_junction(combat_enemies, towers)
        if reinforce_target:
            rx, ry, reason = reinforce_target
            self.do_use_power(2, rx, ry, reason)

    # ---------- 火雨目标选择 ----------

    # 火雨伤害常量（从游戏数据提取）
    FIRE_DIRECT_DAMAGE = 400   # 5颗火球(50-80) + 火坑全程(10-20×5)
    FIRE_PIT_DAMAGE = 75       # 仅火坑持续伤害(10-20×5秒)
    FIRE_DIRECT_RADIUS = 60    # 火球爆炸半径
    FIRE_PIT_RADIUS = 65       # 火坑半径
    FIRE_PIT_DURATION = 5       # 火坑持续时间（秒）
    FIRE_MIN_EFFECTIVE = 100   # 最低有效伤害门槛（避免浪费在残血小怪上）

    def _find_best_fire_target(self, enemies):
        """找火雨最优释放点：最大化有效伤害。
        有效伤害 = 直击敌人的 min(hp, 400) + 后续走入火坑的 min(hp, 75)。
        返回 (x, y, effective_damage, direct_count, trail_count, diag) 或 None。
        """
        best = None
        best_score = 0
        best_diag = []

        for center in enemies:
            cx, cy = center.get("x", 0), center.get("y", 0)
            c_pi = center.get("path_index")
            c_ni = center.get("path_ni", 0)
            eff_dmg = 0.0
            direct_count = 0
            trail_count = 0
            diag_parts = []

            for e in enemies:
                ex, ey = e.get("x", 0), e.get("y", 0)
                hp = e.get("hp", 0)
                if hp <= 0:
                    continue
                d = dist(cx, cy, ex, ey)

                if d < self.FIRE_DIRECT_RADIUS:
                    # 直接命中区域：吃满火球+火坑
                    dmg = min(hp, self.FIRE_DIRECT_DAMAGE)
                    eff_dmg += dmg
                    direct_count += 1
                    diag_parts.append(f"{e.get('template','?')}(hp={hp:.0f},伤={dmg:.0f})")
                else:
                    # 火坑：同路径、在后方的敌人可能在5秒内走到
                    e_pi = e.get("path_index")
                    e_ni = e.get("path_ni", 0)
                    e_speed = e.get("speed", 0) or 60  # 默认速度
                    walk_range = e_speed * self.FIRE_PIT_DURATION
                    if e_pi == c_pi and e_ni < c_ni and d < walk_range:
                        dmg = min(hp, self.FIRE_PIT_DAMAGE)
                        eff_dmg += dmg
                        trail_count += 1

            if eff_dmg > best_score and eff_dmg >= self.FIRE_MIN_EFFECTIVE:
                best_score = eff_dmg
                best = (cx, cy, eff_dmg, direct_count, trail_count)
                best_diag = diag_parts

        if best:
            cx, cy, eff_dmg, direct_count, trail_count = best
            return (cx, cy, eff_dmg, direct_count, trail_count, best_diag)
        return None

    # ---------- 增援目标选择 ----------

    def _find_reinforce_junction(self, enemies, towers):
        """找离最前方敌人最近的路口，在路径上放援军。
        路口 = 有塔且 nearby_paths ≥ 2 的位置。
        放在路口塔附近的路径点上（而非塔本身坐标），确保援军在路径上阻挡敌人。
        返回 (x, y, reason) 或 None。
        """
        # 找最前方敌人
        forward = self.find_most_forward_enemy(enemies)
        if not forward:
            return None
        fx, fy = forward.get("x", 0), forward.get("y", 0)

        # 收集路口塔位（nearby_paths ≥ 2）
        junctions = []
        for t in towers:
            nearby = t.get("nearby_paths", [])
            if len(nearby) >= 2:
                junctions.append(t)

        if not junctions:
            # 没有路口，退回到最前方敌人位置
            return (fx, fy,
                    f"无路口 拦截{cn_enemy(forward.get('template', '?'))}")

        # 选离最前方敌人最近的路口
        best_tower = min(junctions,
                         key=lambda t: dist(fx, fy, t.get("x", 0), t.get("y", 0)))
        tx, ty = best_tower.get("x", 0), best_tower.get("y", 0)
        paths_str = ",".join(str(p) for p in best_tower.get("nearby_paths", []))

        # 在路口塔附近找路径点，选最接近前方敌人的点
        rx, ry = tx, ty  # fallback 到塔坐标
        result = self.bot.get_path_points(tx, ty, 180)
        if result and result.get("type") == "ok":
            path_pts = result.get("points", [])
            if path_pts:
                best_pt = min(path_pts,
                              key=lambda p: dist(fx, fy, p["x"], p["y"]))
                rx, ry = best_pt["x"], best_pt["y"]

        d = dist(fx, fy, rx, ry)
        self._print(f"    [增援诊断] 最前敌=({fx:.0f},{fy:.0f}) "
                    f"路口路径点=({rx:.0f},{ry:.0f}) 距离={d:.0f} "
                    f"路径=[{paths_str}]")
        return (rx, ry,
                f"路口路径拦截 距前敌{d:.0f} 路径=[{paths_str}]")

    def _is_barrack(self, template):
        return "barrack" in template or "paladin" in template or "barbarian" in template

    def _classify_special_tower(self, tower):
        """分析特殊塔的战术角色，返回 (role, description)。
        role: 'barrack'=出兵挡路, 'dps'=远程火力, 'unknown'=未知
        """
        t_type = tower.get("type", "")
        template = tower.get("template", "")
        has_soldiers = tower.get("soldier_count", 0) > 0
        has_range = (tower.get("range") or 0) > 0

        if t_type == "barrack" or has_soldiers or self._is_barrack(template):
            return "barrack", "出兵型"
        elif t_type in DPS_TOWER_TYPES or has_range:
            return "dps", f"{cn_type(t_type)}型" if t_type in CN_TOWER_TYPE else "远程火力型"
        else:
            return "unknown", "未知型"

    def _get_special_towers(self, towers):
        """从塔列表中提取特殊塔，附带分类信息。"""
        specials = []
        for t in towers:
            if t.get("is_special"):
                role, desc = self._classify_special_tower(t)
                specials.append({**t, "_role": role, "_desc": desc})
        return specials

    def _log_special_towers(self, towers):
        """首次发现特殊塔时打印分析（只打印一次）。"""
        if self._special_towers_logged:
            return
        specials = self._get_special_towers(towers)
        if not specials:
            return
        self._special_towers_logged = True
        self._print(f"  [特殊塔] 检测到 {len(specials)} 座关卡特殊建筑:")
        for s in specials:
            name = cn_template(s.get("template", "")) or s.get("template", "?")
            r = s.get("range") or 0
            ps = s.get("path_score", 0)
            extra = f"射程={r}" if r else ""
            sc = s.get("soldier_count", 0)
            if sc:
                extra = f"士兵={sc}"
            self._print(f"    · {name} [{s['_desc']}] 覆盖={ps} {extra} (低优先级，不拆不升)")

    def _score_rally_candidate(self, px, py, ps, dps_towers, other_rally_positions):
        """评分一个集结点候选位置。
        策略：士兵站在 DPS 塔火力覆盖下的路口，分散不堆叠。
        """
        score = 0.0

        # 1) 路径覆盖度（路口价值高，多条路经过 = 拦截更多敌人）
        score += ps * 10

        # 2) DPS 塔火力覆盖（附近 DPS 塔越多，士兵挡住的敌人被集火越猛）
        for t in dps_towers:
            tx, ty = t.get("x", 0), t.get("y", 0)
            t_range = t.get("range", 150) or 150
            d = dist(px, py, tx, ty)
            if d < t_range:
                # 在塔攻击范围内：满分
                score += 20
            elif d < t_range * 1.3:
                # 略超范围：部分分
                score += 10

        # 3) 与其他兵营集结点保持距离（避免堆叠，分散防御）
        for ox, oy in other_rally_positions:
            d = dist(px, py, ox, oy)
            if d < 60:
                score -= 15  # 太近了，惩罚
            elif d < 120:
                score -= 5

        return score

    def ensure_rally_points(self, state):
        """兵营集结点策略管理。
        为每座兵营选择最佳集结点：路口 + DPS 塔火力覆盖下 + 在 rally_range 内 + 在路径上。
        只设一次（_rally_set 追踪），新建塔时重置。
        """
        towers = state.get("towers", [])

        # 分类：兵营 vs DPS 塔（包含特殊塔的火力/挡路贡献）
        barracks = []
        dps_towers = []
        for t in towers:
            template = t.get("template", "")
            if t.get("is_special"):
                role, _ = self._classify_special_tower(t)
                if role == "barrack":
                    # 特殊兵营不需要设集结点，但参与分散计算
                    pass
                elif role == "dps":
                    dps_towers.append(t)  # 特殊 DPS 塔纳入火力覆盖评分
            elif self._is_barrack(template):
                barracks.append(t)
            elif t.get("type") in DPS_TOWER_TYPES:
                dps_towers.append(t)

        if not barracks:
            return

        # 收集已设置的集结点位置（用于分散惩罚）
        other_rally = []
        # 特殊兵营的位置也算在内（它们的士兵已经在挡路）
        for t in towers:
            if t.get("is_special"):
                role, _ = self._classify_special_tower(t)
                if role == "barrack":
                    other_rally.append((t.get("x", 0), t.get("y", 0)))
        for t in barracks:
            if t["id"] in self._rally_set:
                rx = t.get("rally_x") or t.get("x", 0)
                ry = t.get("rally_y") or t.get("y", 0)
                other_rally.append((rx, ry))

        for barrack in barracks:
            bid = barrack["id"]
            if bid in self._rally_set:
                continue  # 已设置过

            bx = barrack.get("x", 0)
            by = barrack.get("y", 0)
            rally_range = barrack.get("rally_range")
            template = barrack.get("template", "")

            # rally_range 未初始化（刚建好还没完成初始化），跳过等下一个 tick
            if not rally_range:
                continue

            # 从 bridge 获取 rally_range 内的路径点（加 15% 安全边距，避免视觉圈外）
            safe_range = rally_range * 0.85
            result = self.bot.get_path_points(bx, by, safe_range)
            if not result or result.get("type") != "ok":
                continue

            candidates = result.get("points", [])
            if not candidates:
                self._print(f"    [集结] 兵营{bid}({cn_template(template)}) 范围内无路径点，保持默认")
                self._rally_set.add(bid)
                continue

            # 评分所有候选路径点
            best_point = None
            best_score = -999
            for pt in candidates:
                px, py, ps = pt["x"], pt["y"], pt.get("path_score", 0)
                score = self._score_rally_candidate(px, py, ps, dps_towers, other_rally)
                if score > best_score:
                    best_score = score
                    best_point = (px, py, ps)

            if not best_point:
                self._rally_set.add(bid)
                continue

            rx, ry, rps = best_point

            # 与当前默认集结点比较，如果差距不大就不移动
            cur_rx = barrack.get("rally_x") or bx
            cur_ry = barrack.get("rally_y") or by
            move_dist = dist(rx, ry, cur_rx, cur_ry)

            if move_dist < 20:
                # 默认位置已经很好，不需要移动
                self._rally_set.add(bid)
                continue

            # 计算附近 DPS 塔数量（用于日志）
            nearby_dps = sum(1 for t in dps_towers
                            if dist(rx, ry, t.get("x", 0), t.get("y", 0)) < (t.get("range", 150) or 150))

            # 到塔中心的实际距离（用于与 rally_range 对比）
            dist_to_center = dist(rx, ry, bx, by)

            self.do_set_rally(bid, rx, ry,
                              f"路口覆盖={rps} DPS塔火力={nearby_dps} 移动{move_dist:.0f}px "
                              f"距塔心={dist_to_center:.0f}px rally_range={rally_range}")
            self._rally_set.add(bid)
            other_rally.append((rx, ry))  # 更新分散列表

    def decide_send_wave(self, state):
        """决定是否提前出波。
        核心条件：当前波的怪必须已经全部刷出（wave_spawning=false），
        且场上怪物已清理或数量很少时才出波。后期更保守。
        """
        lives = state.get("lives", 0)
        enemies = state.get("enemies", [])
        wave = state.get("wave", 0)
        wave_total = state.get("wave_total", 0)
        waves_finished = state.get("waves_finished", False)
        wave_spawning = state.get("wave_spawning", True)  # 默认 True 保守

        if waves_finished or wave == 0 or wave >= wave_total:
            return

        # 当前波还在刷怪中 → 绝不出波
        if wave_spawning:
            return

        # 后期阶段（最后 1/3 波次）更保守，只在清场后出波
        late_game = wave >= wave_total * 2 / 3

        # 场上没有敌人 → 出下一波（赚奖励金）
        if len(enemies) == 0:
            self.do_send_wave("清场完毕, 赚奖励金")
        # 前中期 + 满命 + 敌人很少 → 可以提前出波
        elif not late_game and lives >= 20 and len(enemies) <= 2:
            self.do_send_wave(f"满命+敌人少({len(enemies)}个), 赚奖励金")

    # ========== 主循环 ==========

    def run(self):
        self._print("\n" + "=" * 50)
        mode = "试运行模式" if self.dry_run else "自动对战模式"
        self._print(f"  Kingdom Rush AI - {mode}")
        self._print(f"  Bridge: {self.bridge_version}")
        self._print("=" * 50)
        self._print("  按 Ctrl+C 停止\n")

        # 准备阶段状态由 self._prep_done 控制

        while self.bot.connected:
            try:
                state = self.get_state()
                if not state:
                    self._print("  [警告] 无法获取状态，重试...")
                    time.sleep(2)
                    continue

                self.tick_count += 1

                # 收集 Bridge 上报的扣命事件
                life_lost = state.get("life_lost_events", [])
                if life_lost:
                    for evt in life_lost:
                        self._life_lost_events.append(evt)
                        tpl = evt.get("enemy_template", "未知")
                        tpl_cn = cn_enemy(tpl)
                        w = evt.get("wave", "?")
                        lost_n = evt.get("lives_lost", 1)
                        remain = evt.get("lives_remaining", "?")
                        self._print(f"  [扣命] 波{w} {tpl_cn}({tpl}) 突破 -{lost_n}命 (剩{remain})")
                        self._push_event(
                            f"{tpl_cn}突破了防线！被扣了{lost_n}条命，还剩{remain}条",
                            "life_lost")

                # 同步 level_idx（首次获取或 Bridge 端更准确）
                if self.level_idx is None and state.get("level_idx"):
                    self.level_idx = state["level_idx"]

                # 自动关闭教程/提示弹窗
                self.dismiss_popups()

                gold = state.get("gold", 0)
                lives = state.get("lives", 0)
                wave = state.get("wave", 0)
                wave_total = state.get("wave_total", 0)
                enemies = state.get("enemies", [])
                towers = state.get("towers", [])
                paused = state.get("paused", False)
                finished = state.get("waves_finished", False)

                # 更新关卡锁定塔列表
                locked = state.get("locked_towers", [])
                if locked:
                    self._locked_towers = set(locked)

                # 更新活跃路径（从当前敌人和下一波数据中收集）
                prev_active = set(self._active_paths)
                for e in enemies:
                    pi = e.get("path_index")
                    if pi:
                        self._active_paths.add(pi)
                nw = state.get("next_wave")
                if nw:
                    for p in nw.get("paths", []):
                        pi = p.get("path_index")
                        if pi:
                            self._active_paths.add(pi)
                new_paths = self._active_paths - prev_active
                if new_paths:
                    self._print(f"  [路径] 新入口激活: {sorted(new_paths)} | 活跃路径={sorted(self._active_paths)}")

                # 更新路径总节点数（用于威胁度计算）
                pe = state.get("path_entries")
                if pe:
                    if isinstance(pe, dict):
                        for pi_str, entry in pe.items():
                            if isinstance(entry, dict) and entry.get("total"):
                                self._path_totals[int(pi_str)] = entry["total"]
                    elif isinstance(pe, list):
                        for i, entry in enumerate(pe):
                            if isinstance(entry, dict) and entry.get("total"):
                                self._path_totals[i + 1] = entry["total"]

                # 状态摘要
                status_parts = [f"金:{gold}", f"命:{lives}", f"波:{wave}/{wave_total}",
                                f"塔:{len(towers)}", f"怪:{len(enemies)}"]
                if paused:
                    status_parts.append("暂停")
                if finished and len(enemies) == 0:
                    status_parts.append("胜利!")
                self._print(f"\n[Tick {self.tick_count}] {' | '.join(status_parts)}")

                # 下一波预览（每波只打印一次）
                wave_analysis = self.analyze_next_wave(state)
                if wave_analysis:
                    wa_key = wave_analysis.get("summary", "")
                    if wa_key != getattr(self, '_last_wave_summary', ''):
                        self._last_wave_summary = wa_key
                        pm = wave_analysis["prefer_magic"]
                        pp = wave_analysis["prefer_physical"]
                        hint = ""
                        if pm > 0.6:
                            hint = " → 优先法伤塔"
                        elif pp > 0.6:
                            hint = " → 优先物伤塔"
                        if wave_analysis["is_swarm"]:
                            hint += " (群怪→炮塔)"
                        self._print(f"  [侦察] {wa_key}{hint}")
                        # 口语化波次预览给快脑
                        count = wave_analysis.get("total_count", 0)
                        swarm_hint = "好多怪！" if wave_analysis["is_swarm"] else ""
                        resist_hint = ""
                        if pm > 0.6:
                            resist_hint = "它们物抗很高"
                        elif pp > 0.6:
                            resist_hint = "它们法抗很高"
                        # 怪物种类描述（最多列3种）
                        etypes = wave_analysis.get("enemy_types", {})
                        sorted_types = sorted(etypes.items(), key=lambda x: -x[1])[:3]
                        type_desc = "、".join(
                            f"{cn_enemy(t)}×{c}" for t, c in sorted_types
                        ) if sorted_types else ""
                        parts = [f"下一波来了{count}个怪"]
                        if type_desc:
                            parts.append(f"（{type_desc}）")
                        if swarm_hint:
                            parts.append(swarm_hint)
                        if resist_hint:
                            parts.append(resist_hint)
                        self._push_event(" ".join(parts), "wave_preview")

                # 每5个tick推送状态快照给 Lumi
                if self.tick_count % 5 == 0 and self._event_callback:
                    towers_info = format_tower_summary(towers)
                    self._event_callback("game_state", {
                        "gold": gold, "lives": lives,
                        "wave": wave, "wave_total": wave_total,
                        "towers": len(towers), "enemies": len(enemies),
                        "paused": paused,
                        "towers_info": towers_info,
                    })

                # 游戏结束检测
                game_over = state.get("game_over", False)
                level_lost = state.get("level_lost", False)
                level_won = state.get("level_won", False)

                # 胜利检测
                if (finished and len(enemies) == 0) or level_won:
                    self._log_action("结果", "胜利!")
                    self._log_tick(state)
                    self._print("\n  关卡完成！AI 停止。")
                    self._push_event(f"赢了！全部{wave_total}波都扛住了，还剩{lives}条命", "level_win")
                    self._result = "win"
                    break

                # 明确失败
                if lives <= 0 or game_over or level_lost:
                    self._log_action("结果", f"失败 命={lives}")
                    self._log_tick(state)
                    self._print("\n  游戏结束（失败）。AI 停止。")
                    self._push_event(f"输了...才到第{wave}波就被打穿了", "level_lose")
                    self._result = "lose"
                    break

                # 低血量超时检测（防止游戏已结束但状态未更新）
                if lives <= 1:
                    if self._low_lives_since is None:
                        self._low_lives_since = self.tick_count
                    elif self.tick_count - self._low_lives_since > 60:
                        self._log_action("结果", "超时（长期低血量，疑似已结束）")
                        self._log_tick(state)
                        self._print("\n  长时间低血量，AI 自动停止。")
                        self._result = "timeout"
                        break
                else:
                    self._low_lives_since = None

                # 暂停中不做决策（但准备阶段可以在暂停中执行建塔/移动英雄）
                if paused and getattr(self, '_prep_done', False):
                    time.sleep(1)
                    continue

                # 首次检测特殊塔并打印分析
                self._log_special_towers(towers)

                # === 准备阶段：wave==0，每 tick 做一个动作 ===
                if wave == 0 and not getattr(self, '_prep_done', False):
                    # LLM 开局决策（只在准备阶段第一个 tick 执行）
                    if not self._llm_plan_ready:
                        self._do_llm_opening_decision(state)
                        self._llm_plan_ready = True
                    self.prepare_tick(state)
                    self._log_tick(state)
                    time.sleep(1.0)
                    continue

                # 等待第一波实际开始
                if wave == 0:
                    self._print("  等待出波...")
                    time.sleep(1)
                    continue

                # === 战斗阶段 ===
                self.decide_tower_actions(state)
                self.ensure_rally_points(state)
                self.decide_hero(state)
                self.decide_powers(state)
                self.decide_send_wave(state)
                self._log_tick(state)

                time.sleep(1.0)

            except KeyboardInterrupt:
                self._print("\n\n  AI 已手动停止。")
                self._result = "abort"
                break

        self._print(f"\n  共运行 {self.tick_count} 个决策周期。")
        self.save_log()
        return self._result


    def reset(self):
        """重置 AI 状态，准备打下一关"""
        self.tick_count = 0
        self.start_time = datetime.now()
        self.log_entries = []
        self._current_actions = []
        self._current_prints = []
        self._last_hero_move_time = 0
        self._rally_set = set()
        self._low_lives_since = None
        self._locked_towers = set()
        self._special_towers_logged = False
        self._active_paths = set()
        self._last_pick_fail_holder = None
        self._path_totals = {}
        self._result = None
        self._prep_done = False


def detect_screen(bot):
    """检测当前界面"""
    result = bot.send_and_receive({"action": "detect_screen"})
    if result and result.get("type") == "screen_info":
        return result.get("screen", "unknown")
    return "unknown"


def pick_next_level(bot):
    """选择下一个要打的关卡：未三星 > 未通关，返回 (level_idx, mode, current_stars) 或 None"""
    result = bot.send_and_receive({"action": "get_level_list"})
    if not result or result.get("type") != "level_list":
        return None
    levels = result.get("levels", [])
    if not levels:
        return None

    # 优先找未三星但已通关的关卡（刷星）
    for lv in levels:
        stars = lv.get("stars", 0)
        if 0 < stars < 3:
            return (lv["idx"], 1, stars)  # 普通模式，附带当前星数

    # 其次找未通关的第一关（推图）
    for lv in levels:
        stars = lv.get("stars", 0)
        if stars <= 0:
            return (lv["idx"], 1, 0)

    # 全部三星，检查钢铁/英雄模式（未来扩展）
    print("  所有关卡已三星通关！")
    return None


def launch_game():
    """启动游戏：运行 exe → 等待启动器窗口 → 点击'开始'按钮"""
    user32 = ctypes.windll.user32

    print(f"  [启动] 运行游戏: {GAME_EXE}")
    subprocess.Popen([GAME_EXE], cwd=os.path.dirname(GAME_EXE))

    # 等待启动器窗口出现
    hwnd = 0
    for i in range(30):
        hwnd = user32.FindWindowW(None, LAUNCHER_TITLE)
        if hwnd:
            break
        time.sleep(1)
    if not hwnd:
        print("  [启动] 找不到启动器窗口，超时。")
        return False

    print(f"  [启动] 找到启动器窗口，点击'{LAUNCHER_BUTTON}'...")
    time.sleep(1)  # 等启动器完全渲染

    # 遍历子窗口找"开始"按钮
    found_button = [None]

    @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_callback(child_hwnd, _lparam):
        length = user32.GetWindowTextLengthW(child_hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(child_hwnd, buf, length + 1)
            if buf.value == LAUNCHER_BUTTON:
                found_button[0] = child_hwnd
                return False  # 停止枚举
        return True

    user32.EnumChildWindows(hwnd, enum_callback, 0)

    if found_button[0]:
        BM_CLICK = 0x00F5
        user32.SendMessageW(found_button[0], BM_CLICK, 0, 0)
        print("  [启动] 已点击开始按钮，等待游戏加载...")
        return True

    # 后备方案：真实鼠标点击"开始"按钮（LÖVE 用自定义控件，PostMessage 无效）
    print("  [启动] 未找到标准按钮控件，使用鼠标模拟点击...")
    # 获取窗口屏幕坐标
    win_rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(win_rect))
    client_rect = ctypes.wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client_rect))
    cw, ch = client_rect.right, client_rect.bottom
    # 客户区左上角的屏幕坐标
    pt = ctypes.wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
    # "开始"按钮大约在客户区右下方 (78%, 93%)
    btn_screen_x = pt.x + int(cw * 0.78)
    btn_screen_y = pt.y + int(ch * 0.93)
    print(f"  [启动] 客户区 {cw}x{ch}，点击屏幕坐标 ({btn_screen_x}, {btn_screen_y})")
    # 将窗口置前，移动鼠标，真实点击
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    user32.SetCursorPos(btn_screen_x, btn_screen_y)
    time.sleep(0.2)
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.1)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    print("  [启动] 已点击开始，等待游戏加载...")
    return True


def auto_loop(bot, bridge_ver, dry_run=False, max_retries=3, event_callback=None, bridge=None):
    """自动化循环：自动选关、对战、失败重试、胜利换关"""
    def _push(text, event=""):
        if event_callback:
            event_callback("game_event", {"text": text, "event": event})

    print("\n" + "=" * 50)
    print("  Kingdom Rush AI - 自动循环模式")
    print(f"  Bridge: {bridge_ver}")
    print("=" * 50)
    print("  按 Ctrl+C 停止\n")
    _push("王国保卫战自动循环启动", "auto_start")

    _cur_level_idx = None   # 当前关卡索引（map 选关时设置）
    _cur_level_mode = 1     # 当前关卡模式
    _cur_star_goal = None   # 刷星目标（None=首次推图，3=刷三星）
    battle_history = load_history()  # 加载对战历史
    print(f"  [历史] 已加载 {sum(len(v) for v in battle_history.values())} 条对战记录")

    while bot.connected:
        try:
            # 1. 检测当前界面
            screen = detect_screen(bot)
            print(f"  [自动化] 当前界面: {screen}")

            if screen == "main_menu":
                print("  [自动化] 主菜单 → 加载存档槽1...")
                _push("进入主菜单，加载存档", "nav_menu")
                time.sleep(5)  # 让观众看到主菜单
                bot.send_and_receive({"action": "load_slot", "slot": 1})
                time.sleep(5)  # 等待地图加载动画
                continue

            elif screen == "in_level":
                # 已在关卡中，直接运行 AI
                print("  [自动化] 已在关卡中，启动 AI 对战...")
                _push("检测到关卡中，启动AI对战", "nav_level")

            elif screen == "map":
                # 选择下一关
                target = pick_next_level(bot)
                if not target:
                    print("  [自动化] 没有可打的关卡，退出。")
                    _push("所有关卡已通关，退出", "auto_done")
                    break
                level_idx, level_mode, cur_stars = target
                _cur_level_idx = level_idx
                _cur_level_mode = level_mode
                _cur_star_goal = 3 if cur_stars > 0 else None  # 刷星目标
                print(f"  [自动化] 选择关卡 {level_idx} (模式 {level_mode}, 当前{cur_stars}星)...")
                if cur_stars > 0:
                    _push(f"再次挑战第{level_idx}关，目标三星！上次只拿了{cur_stars}星", "nav_map_retry_stars")
                else:
                    _push(f"挑战第{level_idx}关！", "nav_map")
                time.sleep(5)  # 让观众看到地图选关
                result = bot.send_and_receive({
                    "action": "start_level",
                    "level_idx": level_idx,
                    "level_mode": level_mode,
                })
                if not result or result.get("type") != "ok":
                    print(f"  [自动化] 启动失败: {result}")
                    break
                # 关卡已成功启动，标记进入对战状态
                if bridge is not None:
                    bridge.in_round = True
                time.sleep(5)  # 等待关卡加载动画
                continue

            else:
                print(f"  [自动化] 界面加载中 ({screen})，等待...")
                time.sleep(5)
                continue

            # 2. 运行 AI 对战
            ai = KingdomRushAI(bot, dry_run=dry_run, bridge_version=bridge_ver,
                               event_callback=event_callback,
                               level_idx=_cur_level_idx, level_mode=_cur_level_mode,
                               battle_history=battle_history, star_goal=_cur_star_goal)
            result = ai.run()

            # 3. 保存对战记录（胜败都保留）
            if result in ("win", "lose", "timeout"):
                record = ai.get_battle_record()
                add_record(battle_history, record)
                save_history(battle_history)
                print(f"  [历史] 已保存对战记录: {record['result']} "
                      f"(关卡{record.get('level_idx', '?')} 波{record.get('final_wave', '?')}/{record.get('wave_total', '?')})")

            if result == "abort":
                print("\n  [自动化] 手动停止，退出循环。")
                break

            elif result == "win":
                print("  [自动化] 胜利！返回地图...")
                bot._retry_count = 0
                _push("赢了！看看下一关打什么", "nav_win")
                if bridge is not None:
                    bridge.in_round = False
                    bridge._event_callback and bridge._event_callback(
                        "game_event", {"event": "round_complete", "text": "关卡结束，回到地图"})
                time.sleep(5)  # 等待胜利动画
                bot.send_and_receive({"action": "go_to_map"})
                time.sleep(3)  # 等待地图加载

            elif result in ("lose", "timeout"):
                retries = getattr(bot, '_retry_count', 0) + 1
                if retries > max_retries:
                    print(f"  [自动化] 已重试 {max_retries} 次仍失败，跳过此关。")
                    bot._retry_count = 0
                    _push(f"试了{max_retries}次还是打不过，先跳过这关吧", "nav_skip")
                    if bridge is not None:
                        bridge.in_round = False
                        bridge._event_callback and bridge._event_callback(
                            "game_event", {"event": "round_complete", "text": "关卡结束，回到地图"})
                    bot.send_and_receive({"action": "go_to_map"})
                    time.sleep(3)
                else:
                    print(f"  [自动化] 失败，重试 ({retries}/{max_retries})...")
                    bot._retry_count = retries
                    _push(f"不服！再来一次", "nav_retry")
                    time.sleep(3)
                    bot.send_and_receive({"action": "restart_level"})
                    time.sleep(3)  # 等待重置

            else:
                print(f"  [自动化] 未知结果 '{result}'，返回地图...")
                if bridge is not None:
                    bridge.in_round = False
                    bridge._event_callback and bridge._event_callback(
                        "game_event", {"event": "round_complete", "text": "关卡结束，回到地图"})
                bot.send_and_receive({"action": "go_to_map"})
                time.sleep(3)

        except KeyboardInterrupt:
            print("\n\n  [自动化] 手动停止。")
            break

    print("  [自动化] 循环结束。")


def main():
    parser = argparse.ArgumentParser(description="Kingdom Rush AI 自动对战")
    parser.add_argument("--dry-run", action="store_true", help="试运行：只显示决策，不执行操作")
    parser.add_argument("--auto", action="store_true", help="自动循环模式：自动选关、对战、重试")
    parser.add_argument("--max-retries", type=int, default=3, help="自动模式下失败最大重试次数 (默认: 3)")
    parser.add_argument("--host", default="127.0.0.1", help="服务器地址 (默认: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9878, help="端口 (默认: 9878)")
    args = parser.parse_args()

    bot = KingdomRushBot()
    bot.host = args.host
    bot.port = args.port

    try:
        if args.auto:
            # 自动模式：先尝试连接，连不上就启动游戏
            if not bot.connect(retries=3, interval=1, quiet=True):
                print("  [自动化] 游戏未运行，自动启动...")
                if not launch_game():
                    sys.exit(1)
                # 等待游戏加载并注入 bridge（连接重试次数更多）
                if not bot.connect(retries=60, interval=2):
                    print("  [自动化] 游戏启动后仍无法连接。")
                    sys.exit(1)
        else:
            if not bot.connect():
                sys.exit(1)

        # 读取欢迎消息
        welcome = bot.receive(timeout=2.0)
        if welcome:
            print(f"  游戏: {welcome.get('game', '?')}")
            print(f"  Bridge版本: {welcome.get('version', '?')}")
            bridge_ver = welcome.get('version', '?')
        else:
            bridge_ver = '未知'

        if args.auto:
            auto_loop(bot, bridge_ver, dry_run=args.dry_run, max_retries=args.max_retries)
        else:
            ai = KingdomRushAI(bot, dry_run=args.dry_run, bridge_version=bridge_ver)
            ai.run()

    except KeyboardInterrupt:
        pass
    finally:
        bot.close()


if __name__ == "__main__":
    main()
