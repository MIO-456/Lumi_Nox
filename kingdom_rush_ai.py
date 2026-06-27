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
GAME_EXE = r"C:\Games\Kingdom Rush 1\Kingdom Rush.exe"
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

# T3→T4 的两个分支：default=UPGRADE_PATH 里的默认分支，alt=另一个分支。
# 军师可用 t4_branch 按基础塔型指定走哪个（看怪情）；不指定则走 default。
T4_BRANCHES = {
    "archer":   {"default": "tower_ranger",        "alt": "tower_musketeer"},
    "barrack":  {"default": "tower_barbarian",     "alt": "tower_paladin"},
    "mage":     {"default": "tower_arcane_wizard",  "alt": "tower_sorcerer"},
    "engineer": {"default": "tower_tesla",          "alt": "tower_bfg"},
}
# 每个 T4 分支的定位（喂给 LLM 选分支用）
T4_BRANCH_DESC = {
    "tower_ranger":       "游侠:毒箭(克高血重甲,持续掉血)+荆棘反伤,单体强",
    "tower_musketeer":    "火枪:狙击(超远单体爆发,秒高威胁)+散射榴弹(物理AOE)",
    "tower_barbarian":    "蛮族:多兵+旋风斩,出兵多挡得住、近战输出高",
    "tower_paladin":      "圣骑:治疗+圣盾,坦度续航强,挡高输出怪不易崩",
    "tower_arcane_wizard": "奥术:瓦解(概率秒杀高血)+传送(把怪传回起点)",
    "tower_sorcerer":     "术士:变形(把怪变羊)+召唤元素帮挡,控场",
    "tower_tesla":        "特斯拉:闪电链(跳跃打多目标,克密集群怪)+过载",
    "tower_bfg":          "大炮DWAARP:导弹+集束爆破,大范围高爆发,克成片重甲",
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

# T4 特化塔技能的进攻价值权重（越高越优先购买；表外默认 1.0）
# 依据：高单体爆发 / AOE / 控制 / 真伤类技能价值最高
POWER_OFFENSE_VALUE = {
    "poison": 1.4, "thorn": 0.8,            # 游侠：毒箭(克高血重甲)/荆棘反伤
    "sniper": 1.4, "shrapnel": 1.2,         # 火枪：狙击(单体爆发)/散射(AOE)
    "healing": 1.1, "shield": 0.9, "holystrike": 1.2,  # 圣骑：治疗/护盾/圣光
    "dual": 1.0, "twister": 1.1, "throwing": 1.0,      # 蛮族
    "disintegrate": 1.5, "teleport": 1.2,   # 奥术：瓦解(秒杀)/传送
    "polymorph": 1.4, "elemental": 1.2,     # 巫师：变形/元素
    "bolt": 1.2, "overcharge": 1.2,         # 特斯拉
    "missile": 1.3, "cluster": 1.2,         # 超级大炮
}
DEFAULT_POWER_VALUE = 1.0

# 军师"组合缺口"高优先级：当 LLM 明确点名某路要某塔型，而该路缺这型/没升到有效级别时，
# 把"建出来 + 集中升到有效级别"提到仅次于 focus_tower 的高优先（用户决定：LLM 明确指令
# 就给足权重，否则它形同虚设）。达标后自动停（served），不会无限乱投。
LLM_TYPE_GAP_BOOST = 5000       # 补缺加分（>常规升级/铺塔的几百，< focus 的 1e6）
LLM_TYPE_EFFECTIVE_LEVEL = 3    # 该路有一座达到此级(T3)的该型塔即视为"配置到位"，停止高优先补缺
EARLY_PREP_LEAD = 3             # 提前布防前瞻波数：算到的波进入"已知死亡波前N波"内即弹提前布防指令

# 技能/英雄释放的"路段位置优先级"：路程进度 0=入口 1=终点。
# 火雨/增援/英雄一律不放在前半段，优先靠后(快进家)>路口>后半段，避免砸/挡在没用的前段。
POWER_MIN_PROGRESS = 0.5        # 前半段(<0.5)一律不放技能/不派英雄驻守拦截
POWER_NEAR_HOME_PROGRESS = 0.75  # 进度>此值=快进家，最高优先（应急拦截）

# 技能中文名（解说事件用）
CN_POWER = {
    "poison": "毒箭", "thorn": "荆棘光环", "sniper": "狙击", "shrapnel": "散射榴弹",
    "healing": "治疗术", "shield": "圣盾", "holystrike": "圣光打击",
    "dual": "双持", "twister": "旋风斩", "throwing": "投掷斧",
    "disintegrate": "瓦解术", "teleport": "传送术", "polymorph": "变形术", "elemental": "元素召唤",
    "bolt": "闪电链", "overcharge": "过载", "missile": "导弹", "cluster": "集束炸弹",
}

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


# 召唤型怪物（边走边生小怪，本体不杀就源源不断）——援军优先锁死它们断召唤源
SUMMONER_TEMPLATES = {"enemy_necromancer", "enemy_shaman"}


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


def cn_power(name):
    return CN_POWER.get(name, name)


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
        self._level_paths = set()   # 全关所有波会用到的路（bridge v5.9 提供，给塔位估值）
        self._level_path_weights = {}  # 路 → 全关波次数归一化后的traffic因子(0~1)
        self._wave_bias = None      # Phase2 LLM 每波军师返回的"本波偏置"（流水线后台更新）
        self._bestiary = {}         # 怪物图鉴说明 {template: {name, desc}}（一关取一次）
        self._bestiary_fetched = False  # 本关是否已取过图鉴
        self._bias_target = None    # 当前已为哪个 group_idx 触发过偏置计算（去重）
        self._last_pick_fail_holder = None  # 上次选型失败的 holder id，避免重复日志
        self._path_totals = {}  # 每条路径的总节点数 {path_index: total}
        self._path_entry_xy = {}  # 每条路出生点坐标 {path_index: (x,y)}
        self._level_exit = None   # 关卡终点坐标 (x,y)，bridge v5.13 下发
        self._result = None  # 对战结果: "win"/"lose"/"timeout"/"abort"
        self._record_persisted = False  # 本局结果是否已写入历史（防重复）
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
            # 工作副本：把本方案"计划建"的塔也并进去，供后续塔位的阻挡约束/
            # 路口判断参考。否则同一活跃路径上的每个塔位都会因"缺兵营阻挡"
            # 被强制建兵营 → 全兵营无输出、三套模板产出相同被去重成单候选
            # （2026-06-17 修：第8关反复全兵营开局的根因）
            planned_towers = list(towers)

            for i, holder in enumerate(sorted_holders[:max_build]):
                tower_type = self._pick_tower_for_position(
                    holder, counts, int(budget), wave_analysis,
                    log=False, towers=planned_towers, strategy_bias=template["bias"])
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
                # 计划建的塔进工作副本（兵营→该路径视为已阻挡；炮塔→路口已有AOE）
                planned_towers.append({
                    "id": f"plan_{holder['id']}",
                    "type": tower_type,
                    "template": f"tower_{tower_type}_1",
                    "nearby_paths": holder.get("nearby_paths", []),
                    "x": holder.get("x", 0),
                    "y": holder.get("y", 0),
                })

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

    def do_upgrade_power(self, tower_id, power, cost, reason=""):
        tag = "[试运行]" if self.dry_run else "[技能]"
        msg = f"塔{tower_id} 技能{cn_power(power)}(¥{cost})"
        self._print(f"  {tag} {msg}  ({reason})")
        self._log_action("技能升级", f"{msg} | {reason}")
        # 开局布阵阶段不推（避免挤掉策略事件）
        if not self._llm_plan_ready:
            self._push_event(f"购买技能{cn_power(power)} ({reason})", "tower_power_upgraded")
        if not self.dry_run:
            return self.bot.upgrade_power(tower_id, power)
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
        """找路径进度最大的敌人（最接近终点）。用全局进度，不用段内 ni(多段路会判错)。"""
        if not enemies:
            return None
        return max(enemies, key=lambda e: self._enemy_progress(e))

    def find_most_dangerous_enemy(self, enemies):
        """找最危险的敌人：按威胁度评分（接近终点 + lives_cost）"""
        if not enemies:
            return None
        return max(enemies, key=lambda e: self._enemy_threat(e))

    def _enemy_threat(self, enemy):
        """敌人威胁度：路径进度占比 × lives_cost。进度越接近终点越危险。"""
        progress = self._enemy_progress(enemy)  # 0~1, 越大越接近终点
        lives_cost = enemy.get("lives_cost", 1)
        return progress * 10 + lives_cost

    def _enemy_progress(self, enemy):
        """敌人路程进度 0~1：0=刚进入口，1=快到终点(家)。供技能/英雄的路段位置判断。
        优先用桥接下发的 path_progress(已把段内ni折算成全局进度)；旧桥接没有则兜底
        用 段内ni/全局总长(会偏小,仅防崩)。"""
        p = enemy.get("path_progress")
        if isinstance(p, (int, float)) and p >= 0:
            return min(float(p), 1.0)
        ni = enemy.get("path_ni", 0)
        pi = enemy.get("path_index")
        total = self._get_path_total(pi) if pi is not None else 1
        return ni / total if total > 0 else 0

    def _get_path_total(self, path_index):
        """获取路径总节点数（缓存在 _path_totals 中）"""
        return self._path_totals.get(path_index, 100)  # 默认100

    def _pos_progress(self, x, y, pi=None):
        """任意位置离终点多近 0~1（同敌人口径：1 - 到终点距离 / 出生点到终点距离）。
        需要 bridge v5.13 下发的终点坐标；没有(旧桥接)则返回 None，调用方退回别的判据。"""
        if not self._level_exit or not self._path_entry_xy:
            return None
        ex, ey = self._level_exit
        if pi is not None and pi in self._path_entry_xy:
            sx, sy = self._path_entry_xy[pi]
            ref = dist(sx, sy, ex, ey)
        else:
            ref = max((dist(sx, sy, ex, ey) for sx, sy in self._path_entry_xy.values()),
                      default=0)
        if ref <= 0:
            return None
        return max(0.0, min(1.0, 1 - dist(x, y, ex, ey) / ref))

    def _junction_progress(self, t):
        """路口离终点多近：取它覆盖的各路里最大的进度（最靠家的那条）。无终点信息→None。"""
        jx, jy = t.get("x", 0), t.get("y", 0)
        vals = [self._pos_progress(jx, jy, pi) for pi in (t.get("nearby_paths") or [None])]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    def _is_high_threat(self, enemy):
        """判断敌人是否高威胁：接近终点（进度>60%）或 lives_cost≥3"""
        if self._enemy_progress(enemy) > 0.6:
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
        """战斗中决定建塔/升级动作。每个 tick 最多执行一个。

        优先级阶梯（取代旧"先铺满再升级"）：
        P0 覆盖底线：每条活跃路要有兵营阻挡 + 非兵营输出，缺则先补
        P1 集中升满焦点塔到 T4：把钱喂给承压最高的非满级塔，没满 T4 前不建新塔/不买技能
        P2 全 T4 后铺下一座：下个 tick P1 会把它升满 = 铺一座升满一座
        P3 技能（最后、限量）：全图 T4 后才买，优先各技能首级
        每一级买不起就 return False 攒钱，绝不挪作他用（这是"集中"的关键）。
        """
        gold = state.get("gold", 0)
        towers = state.get("towers", [])
        holders = state.get("holders", [])
        empty_holders = [h for h in holders
                         if not h.get("blocked") and not h.get("unblock_price")
                         and h.get("template", "").startswith("tower_holder")]
        wave_analysis = self.analyze_next_wave(state)

        bias = self._wave_bias or {}  # Phase2 LLM 本波偏置（没有则纯算法骨架）

        # P0 覆盖底线
        gap = self._coverage_gap(towers, empty_holders, wave_analysis, bias.get("path_types"))
        if gap:
            holder, ttype = gap
            cost = BUILD_COSTS.get(ttype, 70)
            if gold >= cost:
                pressure = self._calc_holder_pressure(holder, wave_analysis)
                self.do_build(holder["id"], ttype, f"补覆盖底线 承压={pressure:.2f}")
                return True
            return False  # 攒钱补覆盖

        # 偏置：LLM 指定本波破例要买的技能（覆盖"技能最后买"）——覆盖达标后优先
        for ab in bias.get("abilities_now", []):
            t = self._find_tower(towers, ab.get("tower_id"))
            ability = ab.get("ability")
            cost = self._ability_cost(t, ability)
            if cost is not None and gold >= cost:
                self.do_upgrade_power(t["id"], ability, cost,
                                      f"军师指定买技能 {cn_template(t['template'])}")
                return True

        # P1+P2 统一决策（带本波偏置：focus_tower 置顶 / prefer_type 偏好 / save_gold 不铺新塔）
        # 升级候选 承压×50+等级×30（集中）；铺塔候选 承压×50；高承压空路口不再被饿死。
        best = self._best_build_or_upgrade(towers, empty_holders, wave_analysis, gold, bias)
        if best is not None:
            kind, cost = best["kind"], best["cost"]
            if cost > gold:
                return False  # 攒钱给最高价值项，不挪作他用
            note = best.get("bias_note", "")
            if kind == "upgrade":
                t = best["tower"]
                paths_str = ",".join(str(p) for p in t.get("nearby_paths", []))
                self.do_upgrade(t["id"], best["target"], cost,
                                f"集中升级{note} {cn_template(t['template'])} 路径=[{paths_str}]")
            else:
                h = best["holder"]
                pressure = self._calc_holder_pressure(h, wave_analysis)
                self.do_build(h["id"], best["ttype"],
                              f"铺塔{note} 承压={pressure:.2f} 价值={best['score']:.0f}")
            return True

        # P3 技能（最后、限量）；军师让攒钱时不买
        if not bias.get("save_gold"):
            pw = self._best_power_buy(towers, wave_analysis)
            if pw:
                t, ability, cost = pw
                if cost > gold:
                    return False
                self.do_upgrade_power(t["id"], ability, cost, f"买技能 {cn_template(t['template'])}")
                return True

        return False

    def _maybe_trigger_wave_bias(self, state):
        """流水线：打当前波时，为"下一波"异步算 LLM 偏置（不阻塞主循环）。

        一关取一次图鉴说明；每当"下一波预览"换了一组(group_idx 变)就后台触发一次，
        结果写入 self._wave_bias 供 decide_tower_actions 应用。LLM 超时/报错 → None
        → 退纯算法骨架。游戏越过最后一波(无 next_wave)则不触发。
        """
        # A/B 对照开关：设 KR_DISABLE_LLM_DIRECTOR=1 → 不调 LLM，纯算法骨架跑
        # （用于测试"LLM 的结论是否比纯算法更好"）
        if os.environ.get("KR_DISABLE_LLM_DIRECTOR"):
            self._wave_bias = None
            return
        if not self._bestiary_fetched:
            self._bestiary_fetched = True
            try:
                if self.bot:
                    res = self.bot.send_and_receive({"action": "get_bestiary"}, timeout=5.0)
                    if res and res.get("type") == "bestiary":
                        self._bestiary = res.get("data", {}) or {}
            except Exception:
                pass
        nw = state.get("next_wave")
        if not nw:
            return
        target = nw.get("group_idx")
        if target is None or target == self._bias_target:
            return  # 已为这波触发过
        self._bias_target = target
        ctx = self._build_wave_bias_context(state)
        # 把军师输入上下文落盘——终端太长不打印，但要能核对图鉴/历史是否真喂到 LLM
        # （用模块级 os，切勿在此函数局部 import os：上面 1018 行已用 os.environ，
        #   局部 import 会让 os 变成局部变量导致 UnboundLocalError 崩桥）
        try:
            _dbg = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "logs", "kr_wave_context.log")
            with open(_dbg, "a", encoding="utf-8") as _f:
                _f.write(f"\n===== 关{self.level_idx} wave{target} =====\n{ctx}\n")
        except Exception:
            pass
        import threading
        threading.Thread(target=self._compute_wave_bias, args=(ctx, target),
                         daemon=True).start()

    def _compute_wave_bias(self, ctx, target):
        try:
            from kr_strategy_llm import decide_wave_bias
            bias = decide_wave_bias(ctx)
        except Exception:
            bias = None
        if self._bias_target == target:  # 仍在等这波 → 采用（None=退骨架）
            self._wave_bias = bias
            if bias:
                # 归因日志：打印 LLM 返回的结构化字段（不只 reason），方便对照决策
                print(f"  [军师·偏置 wave{target}] focus={bias.get('focus_tower')} "
                      f"prefer={bias.get('prefer_type')} path_types={bias.get('path_types')} "
                      f"abilities={bias.get('abilities_now')} save={bias.get('save_gold')} "
                      f"| {bias.get('reason','')}")
                if bias.get("reason"):
                    self._push_event(f"军师·下波打法: {bias['reason']}", "wave_bias")
            else:
                print(f"  [军师·偏置 wave{target}] 无返回(超时/报错)→本波走算法骨架")

    def _build_wave_bias_context(self, state):
        """拼给 LLM 的局面文本：金币 / 当前防线 / 空位 / 下一波(数值+图鉴) / 上波漏哪。"""
        lines = [f"金币: {state.get('gold', 0)}"]
        towers = [t for t in state.get("towers", []) if not t.get("is_special")]
        lines.append("当前防线(可指定 focus_tower 升级):")
        for t in towers:
            lvl = self._get_tower_level(t.get("template", ""))
            pwr = ""
            if t.get("powers"):
                buyable = [p.get("name") for p in t["powers"] if p.get("next_cost") is not None]
                if buyable:
                    pwr = " 可买技能=" + ",".join(buyable)
            lines.append(f"  塔{t.get('id')} {cn_template(t.get('template',''))}(T{lvl}) "
                         f"覆盖路={t.get('nearby_paths')}{pwr}")
        empty = [h for h in state.get("holders", [])
                 if not h.get("blocked") and not h.get("unblock_price")
                 and str(h.get("template", "")).startswith("tower_holder")]
        if empty:
            lines.append(f"空塔位 {len(empty)} 个(覆盖越多越值钱):")
            for h in sorted(empty, key=lambda x: -(x.get("path_score") or 0))[:8]:
                lines.append(f"  位{h.get('id')} 覆盖路={h.get('nearby_paths')} 覆盖量={h.get('path_score')}")
        nw = state.get("next_wave") or {}
        lines.append("下一波来袭(请据图鉴机制定塔型):")
        for p in nw.get("paths", []):
            pi = p.get("path_index")
            for sp in p.get("spawns", []):
                tmpl = sp.get("template")
                best = self._bestiary.get(tmpl) or {}
                desc = best.get("desc", "")
                lines.append(
                    f"  路{pi}: {cn_enemy(tmpl)}×{sp.get('count')} "
                    f"[血{sp.get('hp')} 甲{sp.get('armor')} 法抗{sp.get('magic_armor')}] "
                    f"图鉴:{desc}")
        recent = self._life_lost_events[-3:] if self._life_lost_events else []
        if recent:
            lines.append("本局上波漏命:")
            for e in recent:
                lines.append(f"  {cn_enemy(e.get('enemy_template',''))} 在路{e.get('path_index','?')}突破")
        # 本关跨局历史失败——让军师知道这关上次死在哪波、被什么怪突破，提前备克制
        hist = get_level_history(self._battle_history, self.level_idx,
                                 self.level_mode) if self.level_idx else []
        losses = [r for r in hist if r.get("result") in ("lose", "timeout")]
        death_info = {}  # {死亡波: 突破怪描述} —— 供"提前布防"判断
        if losses:
            lines.append("本关历史失败(重点提前防住这些波):")
            for r in losses[-2:]:
                breakers = {}
                for e in (r.get("life_lost_log") or r.get("life_lost_events") or []):
                    t = cn_enemy(e.get("enemy_template", ""))
                    breakers[t] = breakers.get(t, 0) + 1
                top = "、".join(f"{k}×{v}" for k, v in
                                sorted(breakers.items(), key=lambda x: -x[1])[:3])
                fw = r.get("final_wave")
                lines.append(f"  上次第{fw}波阵亡/重伤，突破怪: {top or '未知'}")
                if isinstance(fw, int):
                    death_info[fw] = top or "未知"
        # 提前布防：只对"有历史失败记录的波"生效——算到的波进入死亡波前 EARLY_PREP_LEAD 波内，
        # 就弹醒目指令让军师现在就开始堆克制塔到 T3，而不是临波才建(来不及升级被淹)。
        # 没失败过的普通波不触发，保持常规 1 波前瞻，避免全局超前囤塔吃光经济。
        target = (state.get("next_wave") or {}).get("group_idx")
        if isinstance(target, int) and death_info:
            for dw in sorted(death_info):
                lead = dw - target
                if 0 <= lead <= EARLY_PREP_LEAD:
                    lines.append(
                        f"⚠️提前布防：本关历史在第{dw}波被[{death_info[dw]}]突破；你现在算第{target}波，"
                        f"距该死亡波还有{lead}波。从现在这波起就把克制它的塔型(召唤/群潮→工程师AOE)"
                        f"写进 path_types、在那条路集中堆到 T3，别等到第{dw}波临波才建——临建来不及升级会被淹。")
                    break
        return "\n".join(lines)

    def _find_tower(self, towers, tid):
        if tid is None:
            return None
        for t in towers:
            if str(t.get("id")) == str(tid):
                return t
        return None

    def _ability_cost(self, tower, ability):
        """返回该塔某技能升下一级的花费，不可买则 None。"""
        if not tower or not ability:
            return None
        for pw in tower.get("powers", []) or []:
            if pw.get("name") == ability and pw.get("next_cost") is not None:
                return pw["next_cost"]
        return None

    def _count_tower_types(self, towers):
        """统计现有塔类型（特殊塔算半个，性价比低）。"""
        type_counts = {}
        for t in towers:
            tt = t.get("type", "")
            if t.get("is_special"):
                type_counts[tt] = type_counts.get(tt, 0) + 0.5
            else:
                type_counts[tt] = type_counts.get(tt, 0) + 1
        return type_counts

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

    def _coverage_gap(self, towers, empty_holders, wave_analysis, path_types=None):
        """覆盖底线检测：每条活跃路需"一个兵营阻挡 + 至少一座非兵营输出塔"。
        返回 (holder, tower_type) 表示要补的塔，或 None 表示覆盖达标。
        优先补无兵营的路（漏穿最急），再补无输出的路；同类挑承压最高的空位。
        补输出时若军师给了 path_types(按路塔型)，按它选 DPS 类型。
        """
        if not self._active_paths:
            return None
        barrack_paths, dps_paths = set(), set()
        for t in towers:
            tmpl = t.get("template", "")
            ttype = t.get("type", "")
            covered = [p for p in t.get("nearby_paths", []) if p in self._active_paths]
            if ttype == "barrack" or self._is_barrack(tmpl):
                barrack_paths.update(covered)
            else:
                dps_paths.update(covered)

        # need_dps=False 先补兵营缺口，再 need_dps=True 补输出缺口
        for need_dps, covered in ((False, barrack_paths), (True, dps_paths)):
            missing = [p for p in self._active_paths if p not in covered]
            if not missing:
                continue
            cands = [h for h in empty_holders
                     if any(p in missing for p in h.get("nearby_paths", []))]
            if not cands:
                continue
            cands.sort(key=lambda h: self._calc_holder_pressure(h, wave_analysis), reverse=True)
            holder = cands[0]
            if not need_dps:
                return (holder, "barrack")
            # 补输出：军师按路指定了非兵营塔型就用它，否则按本波抗性
            ptype = self._path_type_for_holder(holder, path_types)
            if ptype and ptype != "barrack":
                return (holder, ptype)
            return (holder, self._pick_dps_type(wave_analysis))
        return None

    def _path_type_for_holder(self, holder, path_types):
        """军师 path_types(按路塔型) → 这个塔位该建什么。
        塔位可能覆盖多条路，取其中"全关最忙的那条有指定的路"的塔型。无则 None。
        """
        if not path_types:
            return None
        best_type, best_w = None, -1.0
        for pi in holder.get("nearby_paths", []):
            if pi in path_types:
                w = self._level_path_weights.get(pi, 1.0)
                if w > best_w:
                    best_w, best_type = w, path_types[pi]
        return best_type

    def _pick_dps_type(self, wave_analysis):
        """按下一波抗性选一个 DPS 塔类型（绝不返回兵营）。"""
        if wave_analysis:
            if wave_analysis.get("is_swarm"):
                return "engineer"
            if wave_analysis.get("prefer_magic", 0) > 0.6:
                return "mage"
        return "archer"

    def _tier_upgrade_score(self, t, wave_analysis, per_path, towers):
        """塔升级评分：承压×50 + 等级×30（集中升满一座）+ 类型权重 + 克制加分。"""
        TYPE_WEIGHT = {"archer": 20, "mage": 20, "engineer": 15, "barrack": 10}
        t_type = t.get("type", "")
        level = self._get_tower_level(t.get("template", ""))
        score = self._calc_holder_pressure(t, wave_analysis) * 50 + level * 30 + TYPE_WEIGHT.get(t_type, 0)
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
        # 路口兵营T1且附近有T4塔 → 紧急升级（坦度跟不上火力）
        if t_type == "barrack" and level == 1:
            active_nearby = sum(1 for pi in nearby if pi in self._active_paths)
            if active_nearby >= 2:
                has_t4 = any(
                    self._get_tower_level(ot.get("template", "")) >= 4
                    and dist(t.get("x", 0), t.get("y", 0), ot.get("x", 0), ot.get("y", 0)) < 200
                    for ot in towers if ot["id"] != t["id"])
                if has_t4:
                    score += 40
        return score

    def _resolve_t4_target(self, t3_template, base_type, t4_branch):
        """T3→T4 选分支：军师 t4_branch 给该基础塔型指定了走哪个就用，否则用默认。
        返回 (target_template, cost)。t4_branch 形如 {'engineer':'tower_tesla', ...}。"""
        default_target, cost = UPGRADE_PATH[t3_template]
        chosen = (t4_branch or {}).get(base_type)
        branches = T4_BRANCHES.get(base_type, {})
        if chosen and chosen in (branches.get("default"), branches.get("alt")):
            return chosen, TOWER_COSTS.get(chosen, cost)  # 分支成本可能不同(如bfg400≠tesla375)
        return default_target, cost

    def _best_build_or_upgrade(self, towers, empty_holders, wave_analysis, gold, bias=None):
        """把"升级现有塔"和"在空位建新塔"放进同一队列，按价值返回最高项（dict）或 None。

        升级项 value = _tier_upgrade_score（含 等级×30 集中加成 → 把动工的塔升满再换）。
        铺塔项 value = 承压×50（空位是0级、无等级加成；但路口覆盖多条活跃路→承压本就高）。
        高承压空路口会排到前面被铺，不再被"先升满所有塔"饿死；同承压下已动工的塔靠
        等级加成胜出 → 仍是集中、不会全局铺1级塔。

        bias（Phase2 LLM 本波偏置，可选）：
        - focus_tower：把该塔的升级候选置顶（本波集中升它）
        - prefer_type：建/选型偏这种塔（针对本波怪），并给该类型小幅加分
        - path_types：按路点名塔型——组合缺口高优先（缺则建、低级则集中升到 T3），达标转温和
        - save_gold：本波不铺新塔（只考虑升级，集中攒钱）
        """
        bias = bias or {}
        focus = bias.get("focus_tower")
        prefer = bias.get("prefer_type")
        save_gold = bool(bias.get("save_gold"))
        path_types = bias.get("path_types") or {}
        # path_types（按路塔型）现在"给足权重"：LLM 看得见怪的机制(召唤/群潮要 AOE)而兜底
        # 看不见，所以它明确点名某路要某型时，若该路缺这型或没升到有效级别(T3)，就把
        # "建出来+集中升到 T3"提到仅次于 focus 的高优先(LLM_TYPE_GAP_BOOST)，达标后(served)
        # 自动转温和(+60)，不会无限乱投。这是用户决定：LLM 大多对，明确指令就听它的。
        per_path = wave_analysis.get("per_path", {}) if wave_analysis else {}

        def _served_level(pi, ttype):
            """某路现有该塔型的最高级别（0=该路没有这型塔）——用于组合缺口判断。"""
            lv = 0
            for tw in towers:
                if tw.get("type") == ttype and pi in (tw.get("nearby_paths") or []):
                    lv = max(lv, self._get_tower_level(tw.get("template", "")))
            return lv

        candidates = []
        # 升级候选
        for t in towers:
            if t.get("is_special"):
                continue
            template = t.get("template", "")
            if template in UPGRADE_PATH:
                target, cost = UPGRADE_PATH[template]
                # T3→T4：军师 t4_branch 指定该型走哪个分支就用它，否则走默认
                if template.endswith("_3"):
                    target, cost = self._resolve_t4_target(
                        template, t.get("type"), bias.get("t4_branch"))
                if target in self._locked_towers:
                    continue
                score = self._tier_upgrade_score(t, wave_analysis, per_path, towers)
                note = ""
                if prefer and t.get("type") == prefer:
                    score += 60  # 偏好该伤害类型
                    note = f"[军师偏好{prefer}]"
                # 军师按路塔型——组合缺口高优先：LLM 点名该路要这型，而该路还没有一座达标
                # (T3)的这型塔、且这座(正是这型、还没到 T3) → 强力顶上去（把停在1级的炮塔
                # 集中升到 T3，AOE 才清得动召唤小怪潮）；已达标则只温和倾斜。用户决定：LLM
                # 明确指令给足权重。served 门限避免该路已有 T3 这型塔时还硬升第二座。
                cur_lv = self._get_tower_level(template)
                under_served = any(
                    path_types.get(pi) == t.get("type")
                    and _served_level(pi, t.get("type")) < LLM_TYPE_EFFECTIVE_LEVEL
                    for pi in t.get("nearby_paths", []))
                if under_served and 0 < cur_lv < LLM_TYPE_EFFECTIVE_LEVEL:
                    score += LLM_TYPE_GAP_BOOST
                    note = f"[军师补缺升{t.get('type')}→T{cur_lv + 1}]"
                elif any(path_types.get(pi) == t.get("type")
                         for pi in t.get("nearby_paths", [])):
                    score += 60
                    note = note or f"[军师路型升级{t.get('type')}]"
                if focus is not None and str(t.get("id")) == str(focus):
                    score += 1e6  # 军师指定的焦点塔置顶
                    note = "[军师focus]"
                candidates.append({"kind": "upgrade", "score": score, "tower": t,
                                   "target": target, "cost": cost, "bias_note": note})
        # 铺塔候选（save_gold 时不铺新塔，集中攒钱）
        if not save_gold:
            type_counts = self._count_tower_types(towers)
            for h in empty_holders:
                note = ""
                ptype = self._path_type_for_holder(h, path_types)
                if ptype and ptype in BUILD_COSTS:
                    ttype = ptype  # 军师按路指定的塔型（最精确）
                    note = f"[军师路型{ptype}]"
                elif prefer and prefer in BUILD_COSTS:
                    ttype = prefer  # 全局偏好
                    note = f"[军师偏好{prefer}]"
                else:
                    ttype = self._pick_tower_for_position(h, type_counts, 999999,
                                                          wave_analysis, log=False, towers=towers)
                if not ttype:
                    continue
                score = self._calc_holder_pressure(h, wave_analysis) * 50
                if prefer and ttype == prefer:
                    score += 30
                # 组合缺口高优先：军师点名该路要这型、而该路一座这型塔都没有 → 先建出来。
                # 已有(哪怕1级)就不再建第二座，交给升级分支把它顶到 T3，避免铺一堆1级塔。
                if note.startswith("[军师路型") and ttype:
                    pis = h.get("nearby_paths") or []
                    if any(path_types.get(pi) == ttype and _served_level(pi, ttype) == 0
                           for pi in pis):
                        score += LLM_TYPE_GAP_BOOST
                        note = f"[军师补缺建{ttype}]"
                candidates.append({"kind": "build", "score": score, "holder": h,
                                   "ttype": ttype, "cost": BUILD_COSTS[ttype], "bias_note": note})
        if not candidates:
            return None
        candidates.sort(key=lambda c: -c["score"])
        return candidates[0]

    def _best_power_buy(self, towers, wave_analysis):
        """选一个 T4 塔技能购买（限量：优先各技能首级 level0→1）。
        返回 (tower, power_name, cost) 或 None。
        """
        cands = []
        for t in towers:
            if t.get("is_special"):
                continue
            for pw in t.get("powers", []) or []:
                if pw.get("next_cost") is None:  # 已满级
                    continue
                cands.append((t, pw, pw["next_cost"]))
        if not cands:
            return None
        # 限量：先把各技能买到 1 级（首级最划算），都买过首级再升已有技能
        lvl0 = [c for c in cands if c[1].get("level", 0) == 0]
        pool = lvl0 if lvl0 else cands

        def score(item):
            t, pw, cost = item
            return (self._calc_holder_pressure(t, wave_analysis) * 50
                    + POWER_OFFENSE_VALUE.get(pw.get("name", ""), DEFAULT_POWER_VALUE) * 20)

        pool.sort(key=lambda x: -score(x))
        t, pw, cost = pool[0]
        return (t, pw.get("name", ""), cost)

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
        """计算塔位的"位置价值"分（覆盖主导版，2026-06-21）。

        用户实测指出：覆盖路径最多/路环绕的塔位最好。之前的版本只看"最近路点离入口
        多远 × 车道繁忙度"，完全没用 path_score（覆盖=有多少条路段从射程内经过），
        导致高覆盖的环绕路口被埋没（同样覆盖12的塔位分值能从0.63掉到0.10）。
        改成：**覆盖(path_score)为主项**，入口远近×车道繁忙度只做小幅微调（≤0.2，
        不足以翻盘一个整覆盖单位）。只统计"覆盖到至少一条会用到的路"的塔位，否则
        该位置对本关无价值记 0。

        分值 = path_score/4（归一化到与历史承压相近量级）+ 微调(0~0.2)
        微调 = 在会用到的路里，max(入口权重 × 车道traffic) × 0.2
        """
        path_distances = holder.get("path_distances", {})
        nearby_paths = holder.get("nearby_paths", [])
        scoring_paths = self._level_paths if self._level_paths else self._active_paths

        # 覆盖到的"会用到的路"（没有路集信息时退化为所有附近路）
        used_cov = [p for p in nearby_paths
                    if (not scoring_paths or p in scoring_paths)]
        if scoring_paths and not used_cov:
            return 0.0  # 不覆盖任何会用到的路 → 对本关无价值

        coverage = holder.get("path_score", 0)  # 主项：罩住多少路段（含环绕）

        pos = 0.0  # 次项：入口近×车道忙，做小幅微调
        for pi in used_cov:
            w = self._calc_path_weight(path_distances, pi)
            if w > 0:
                pos = max(pos, w * self._level_path_weights.get(pi, 1.0))

        return coverage / 4.0 + pos * 0.2

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
            # 聚集必须在后半段才去驻守——前段聚集不去(白挡、把怪拖慢帮倒忙、旁边没塔还易死)
            cluster_prog = max((self._enemy_progress(e) for e in enemies
                                if dist(cx, cy, e.get("x", 0), e.get("y", 0)) <= 150),
                               default=0)
            if cluster_count >= 3 and cluster_dist > 250 and cluster_prog >= POWER_MIN_PROGRESS:
                # 选驻守点：优先"有 DPS 塔火力支援"的，其次离聚集最近
                best_intercept = None
                best_key = (-1, 1.0)  # (有支援:1/0, -距聚集)
                best_ps = 0
                for px, py, ps in path_positions:
                    d_to_cluster = dist(cx, cy, px, py)
                    if d_to_cluster < 200:
                        enemies_near = self.count_nearby_enemies(enemies, px, py, 250)
                        if enemies_near >= 2:
                            support = 1 if self._has_dps_support({"x": px, "y": py}, towers) else 0
                            key = (support, -d_to_cluster)
                            if key > best_key:
                                best_key = key
                                best_intercept = (px, py)
                                best_ps = ps
                if best_intercept:
                    tx, ty = best_intercept
                    reason = (f"路口驻守(进度{cluster_prog:.0%},覆盖={best_ps},"
                              f"聚集{cluster_count},支援={'有' if best_key[0] else '无'})")
                    should_move = True

        # 优先级3: 高威胁敌人 → 直接去它所在位置拦它（仅当它已在后半段，前段不追）。
        # 不再用"前方路径点拦截"：那套拿塔的全局节点序号 vs 敌人的段内序号比大小，单位不一致，
        # 会算出乱七八糟的前段点，把英雄拽到全图乱跑。后半段高威胁怪本就离家近，去它位置即可。
        if not should_move:
            dangerous = self.find_most_dangerous_enemy(enemies)
            if (dangerous and self._is_high_threat(dangerous)
                    and self._enemy_progress(dangerous) >= POWER_MIN_PROGRESS):
                dx, dy = dangerous.get("x", 0), dangerous.get("y", 0)
                if dist(hx, hy, dx, dy) > 250:
                    dprog = self._enemy_progress(dangerous)
                    tx, ty = dx, dy
                    reason = (f"拦截{cn_enemy(dangerous.get('template', '?'))}"
                              f"(进度{dprog:.0%},扣{dangerous.get('lives_cost')}命) "
                              f"敌位({dx:.0f},{dy:.0f})")
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
            fx, fy, eff_dmg, direct_count, trail_count, diag, prog = best_fire
            seg = "快进家" if prog >= POWER_NEAR_HOME_PROGRESS else "后半段"
            self._print(f"    [火雨诊断] 目标({fx:.0f},{fy:.0f}) 进度{prog:.0%}({seg}) "
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
        """找火雨释放点：留 CD 给靠后的怪，前半段一律不砸。
        位置优先级：快进家(>0.75) > 后半段(>0.5)；前半段(<0.5)直接跳过(留着 CD)。
        同一档内再比有效伤害(直击 min(hp,400) + 走入火坑 min(hp,75))，要 ≥ 门槛才算合格。
        返回 (x, y, effective_damage, direct_count, trail_count, diag) 或 None(没合格目标→不放，留CD)。
        """
        best = None
        best_key = (-1, -1.0)  # (位置档:2=快进家/1=后半段, 有效伤害)
        best_diag = []

        for center in enemies:
            # 前半段不砸——把火雨留给快进家/后段的目标，避免一扎堆就空放
            c_prog = self._enemy_progress(center)
            if c_prog < POWER_MIN_PROGRESS:
                continue
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

            if eff_dmg < self.FIRE_MIN_EFFECTIVE:
                continue
            bucket = 2 if c_prog >= POWER_NEAR_HOME_PROGRESS else 1
            key = (bucket, eff_dmg)
            if key > best_key:
                best_key = key
                best = (cx, cy, eff_dmg, direct_count, trail_count, c_prog)
                best_diag = diag_parts

        if best:
            cx, cy, eff_dmg, direct_count, trail_count, prog = best
            return (cx, cy, eff_dmg, direct_count, trail_count, best_diag, prog)
        return None

    # ---------- 增援目标选择 ----------

    def _is_summoner(self, template):
        """是否召唤型怪（死灵法师等，或图鉴说明里写了会召唤/产卵/孵化）。"""
        if template in SUMMONER_TEMPLATES:
            return True
        desc = (self._bestiary.get(template) or {}).get("desc", "")
        return any(k in desc for k in ("召唤", "孵化", "产卵", "下崽", "生出"))

    def _has_dps_support(self, spot, towers, radius=160):
        """spot(塔/坐标)附近是否有 DPS 塔火力覆盖——被挡住/驻守点的怪能挨打。"""
        sx, sy = spot.get("x", 0), spot.get("y", 0)
        for t in towers:
            ttype = t.get("type", "")
            is_dps = ttype in DPS_TOWER_TYPES or (
                (t.get("range") or 0) > 0 and not self._is_barrack(t.get("template", "")))
            if not is_dps:
                continue
            r = t.get("range") or radius
            if dist(sx, sy, t.get("x", 0), t.get("y", 0)) <= max(r, 100):
                return True
        return False

    def _find_reinforce_junction(self, enemies, towers):
        """找离最前方敌人最近的路口，在路径上放援军。
        路口 = 有塔且 nearby_paths ≥ 2 的位置。
        放在路口塔附近的路径点上（而非塔本身坐标），确保援军在路径上阻挡敌人。
        增援不留 CD、有就放（持续保持兵在场上挡线，与火雨攒爆发不同），但落点讲究：
        - 后半段有威胁 → 去离最前方(最危险)怪最近的、有 DPS 火力的路口挡它；
        - 后半段没威胁(刚开局/怪都在前段) → 不浪费 CD，放"常备挡线"：后半段+有DPS火力的
          路口里覆盖最高的(没终点坐标信息时退回有火力的高覆盖路口)。绝不挡在前半段。
        返回 (x, y, reason) 或 None(连个像样的路口都没有)。
        """
        # 最高优先：锁死灵法师等召唤者——放它身上触发近战、断召唤，且它已进 DPS 塔火力网
        # (用户:这种怪只要被锁住,任何非兵营塔都能打死)。锁最靠前(召唤最久/最危险)的那只。
        summoners = [e for e in enemies
                     if self._is_summoner(e.get("template", "")) and self._has_dps_support(e, towers)]
        if summoners:
            tgt = self.find_most_forward_enemy(summoners)
            sx, sy = tgt.get("x", 0), tgt.get("y", 0)
            self._print(f"    [增援诊断] 锁召唤者 {cn_enemy(tgt.get('template',''))} "
                        f"@({sx:.0f},{sy:.0f}) 进度{self._enemy_progress(tgt):.0%}")
            return (sx, sy,
                    f"锁召唤者{cn_enemy(tgt.get('template',''))}(断召唤+塔火力秒) "
                    f"进度{self._enemy_progress(tgt):.0%}")

        junctions = [t for t in towers if len(t.get("nearby_paths", [])) >= 2]
        covered = [t for t in junctions if self._has_dps_support(t, towers)]
        forward = self.find_most_forward_enemy(enemies)
        fprog = self._enemy_progress(forward) if forward else 0.0

        if forward is not None and fprog >= POWER_MIN_PROGRESS:
            # 有后半段威胁 → 去离它最近的(有DPS火力的)路口挡它
            fx, fy = forward.get("x", 0), forward.get("y", 0)
            if not junctions:
                return (fx, fy,
                        f"无路口 拦截{cn_enemy(forward.get('template', '?'))} 前敌进度{fprog:.0%}")
            pool = covered or junctions
            best_tower = min(pool, key=lambda t: dist(fx, fy, t.get("x", 0), t.get("y", 0)))
            anchor = (fx, fy)
            tag = f"拦截 前敌进度{fprog:.0%}"
        else:
            # 后半段没威胁(刚开局/怪都在前段) → 不浪费 CD，放常备挡线：
            # 选"后半段 + 有DPS火力"的路口里覆盖最高的(没终点信息就退回有火力的高覆盖路口)。
            if not covered and not junctions:
                return None
            backhalf = [t for t in covered
                        if (self._junction_progress(t) or 1.0) >= POWER_MIN_PROGRESS]
            pool = backhalf or covered or junctions
            best_tower = max(pool, key=lambda t: t.get("path_score") or 0)
            anchor = (best_tower.get("x", 0), best_tower.get("y", 0))
            jp = self._junction_progress(best_tower)
            tag = f"常备挡线 路口进度{('%d%%' % (jp * 100)) if jp is not None else '?'}"

        tx, ty = best_tower.get("x", 0), best_tower.get("y", 0)
        paths_str = ",".join(str(p) for p in best_tower.get("nearby_paths", []))
        # 在路口塔附近找路径点，选最接近锚点(前敌/路口)的点
        rx, ry = tx, ty
        result = self.bot.get_path_points(tx, ty, 180)
        if result and result.get("type") == "ok":
            path_pts = result.get("points", [])
            if path_pts:
                best_pt = min(path_pts, key=lambda p: dist(anchor[0], anchor[1], p["x"], p["y"]))
                rx, ry = best_pt["x"], best_pt["y"]
        support = "有" if best_tower in covered else "无"
        self._print(f"    [增援诊断] {tag} 落点=({rx:.0f},{ry:.0f}) "
                    f"路径=[{paths_str}] DPS支援={support}")
        return (rx, ry, f"{tag} 路径=[{paths_str}] 支援={support}")

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

                # 全关会用到的路 + 各路traffic权重（bridge v5.9 提供，静态）
                lvl_paths = state.get("level_paths")
                if lvl_paths:
                    self._level_paths = set(lvl_paths)
                    wc = state.get("level_path_wave_counts") or []
                    max_wc = max(wc) if wc else 0
                    if max_wc > 0 and len(wc) == len(lvl_paths):
                        self._level_path_weights = {
                            p: wc[i] / max_wc for i, p in enumerate(lvl_paths)}

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

                # 更新路径总节点数 + 出生点坐标（出生点坐标用于算"某位置离家多近"）
                pe = state.get("path_entries")
                if pe:
                    entries = pe.items() if isinstance(pe, dict) else enumerate(pe, 1)
                    for k, entry in entries:
                        if isinstance(entry, dict):
                            pi = int(k)
                            if entry.get("total"):
                                self._path_totals[pi] = entry["total"]
                            if entry.get("x") is not None and entry.get("y") is not None:
                                self._path_entry_xy[pi] = (entry["x"], entry["y"])
                # 关卡终点(掉血点汇聚点，bridge v5.13)——用于判断任意位置/路口在不在后半段
                ex = state.get("exit")
                if isinstance(ex, dict) and ex.get("x") is not None:
                    self._level_exit = (ex["x"], ex["y"])

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
                self._maybe_trigger_wave_bias(state)  # Phase2: 为下一波异步算LLM偏置
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
        self._persist_battle_record()  # 一判定胜负就立刻存，防止导演切环节抢在 auto_loop 保存前
        return self._result

    def _persist_battle_record(self):
        """把本局结果立刻写进对战历史（幂等：同一局只存一次）。
        放在 run() 内，确保直播中导演胜利后立刻切环节、也不会丢掉这条记录。
        """
        if self._record_persisted:
            return
        if self._result not in ("win", "lose", "timeout"):
            return
        if self.level_idx is None:
            return
        try:
            record = self.get_battle_record()
            add_record(self._battle_history, record)
            save_history(self._battle_history)
            self._record_persisted = True
            self._print(f"  [历史] 已保存对战记录: {record['result']} "
                        f"{record.get('stars', 0)}星 (关{record.get('level_idx')} "
                        f"波{record.get('final_wave')}/{record.get('wave_total')} "
                        f"剩{record.get('final_lives')}命)")
        except Exception as e:
            self._print(f"  [历史] 保存失败: {e}")


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
        self._level_paths = set()
        self._level_path_weights = {}
        self._wave_bias = None
        self._bestiary = {}
        self._bestiary_fetched = False
        self._bias_target = None
        self._last_pick_fail_holder = None
        self._path_totals = {}
        self._path_entry_xy = {}
        self._level_exit = None
        self._result = None
        self._record_persisted = False
        self._prep_done = False


def detect_screen(bot):
    """检测当前界面"""
    result = bot.send_and_receive({"action": "detect_screen"})
    if result and result.get("type") == "screen_info":
        return result.get("screen", "unknown")
    return "unknown"


def pick_next_level(bot):
    """选择下一个要打的关卡：未三星 > 未通关，返回 (level_idx, mode, current_stars) 或 None
    设环境变量 KR_FORCE_LEVEL=N 可强制反复重打第 N 关（A/B 对照测试用，已通关也重打）。"""
    result = bot.send_and_receive({"action": "get_level_list"})
    if not result or result.get("type") != "level_list":
        return None
    levels = result.get("levels", [])
    if not levels:
        return None

    # 强制指定关卡（A/B 对照：纯算法 vs LLM 打同一关）
    force = os.environ.get("KR_FORCE_LEVEL")
    if force:
        try:
            fidx = int(force)
            stars = next((lv.get("stars", 0) for lv in levels if lv.get("idx") == fidx), 0)
            print(f"  [强制关卡] KR_FORCE_LEVEL={fidx}（当前{stars}星，对照测试反复重打）")
            return (fidx, 1, stars)
        except ValueError:
            pass

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


def _setup_win32_hwnd_types(user32):
    """给窗口相关 API 设置正确的 HWND 类型，避免 64 位下句柄被截断成 32 位。
    句柄截断会让 GetForegroundWindow()==hwnd 之类的比较得出错误结论。"""
    HWND = ctypes.wintypes.HWND
    user32.FindWindowW.restype = HWND
    user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
    user32.GetForegroundWindow.restype = HWND
    user32.GetForegroundWindow.argtypes = []
    user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
    user32.SetForegroundWindow.argtypes = [HWND]
    user32.BringWindowToTop.argtypes = [HWND]
    user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
    user32.IsIconic.argtypes = [HWND]
    user32.IsWindowVisible.argtypes = [HWND]
    user32.GetClassNameW.argtypes = [HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [HWND]
    user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(ctypes.wintypes.RECT)]
    user32.PostMessageW.argtypes = [HWND, ctypes.wintypes.UINT,
                                    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
    user32.PostMessageW.restype = ctypes.wintypes.BOOL


def _force_foreground(hwnd, attempts=6):
    """把窗口可靠地抢到最前台并验证。

    Windows 对跨进程置前有"前台锁"限制，单纯 SetForegroundWindow 会静默失败。
    这里先把窗口从最小化恢复，再把本线程的输入队列临时挂到当前前台窗口线程上
    （绕过前台锁），循环重试直到 GetForegroundWindow 确认确实是目标窗口。
    返回 True/False 表示是否真的置前成功。
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    cur_tid = kernel32.GetCurrentThreadId()

    for _ in range(attempts):
        if user32.GetForegroundWindow() == hwnd:
            return True
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(fg_tid) and fg_tid != cur_tid
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
        time.sleep(0.2)

    return user32.GetForegroundWindow() == hwnd


def _press_enter():
    """向当前前台窗口发送一次回车按下+抬起（全局键盘注入，需窗口在前台）。

    SDL 游戏按硬件扫描码识别按键，scancode=0 会被当成未知键忽略，
    所以必须带上真实扫描码（回车主键区=0x1C），模拟真实键盘。
    """
    user32 = ctypes.windll.user32
    VK_RETURN = 0x0D
    KEYEVENTF_KEYUP = 0x0002
    MAPVK_VK_TO_VSC = 0
    scan = user32.MapVirtualKeyW(VK_RETURN, MAPVK_VK_TO_VSC) or 0x1C
    user32.keybd_event(VK_RETURN, scan, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_RETURN, scan, KEYEVENTF_KEYUP, 0)


def _post_enter(hwnd):
    """把回车直接投递到指定窗口（焦点无关，不需要窗口在前台）。

    直播时 lumi 是后台进程，Windows 前台锁会拒绝 SetForegroundWindow，
    导致依赖前台的全局回车失效。PostMessage 直接把按键消息塞进该窗口的
    消息队列，SDL 的窗口过程照常处理，不要求前台焦点（已实测后台可触发）。
    lParam 的 16-23 位带真实扫描码 0x1C，否则 SDL 当未知键忽略。
    """
    user32 = ctypes.windll.user32
    WM_KEYDOWN, WM_KEYUP, VK_RETURN = 0x0100, 0x0101, 0x0D
    scan = user32.MapVirtualKeyW(VK_RETURN, 0) or 0x1C
    lp_down = 1 | (scan << 16)
    lp_up = 1 | (scan << 16) | (1 << 30) | (1 << 31)
    user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, lp_down)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, lp_up)


def _trigger_launcher_start(hwnd):
    """触发启动器的"开始"。

    主路径：PostMessage 回车（焦点无关，直播后台也能用）。
    冗余路径：若能成功置前，再发一次全局回车（多按一次回车无害）。
    """
    _post_enter(hwnd)
    if _force_foreground(hwnd):
        _press_enter()


def trigger_launcher_start():
    """重新定位启动器窗口并触发"开始"，供启动失败后的重试调用。
    返回是否找到了启动器窗口。"""
    user32 = ctypes.windll.user32
    _setup_win32_hwnd_types(user32)
    hwnd = user32.FindWindowW(None, LAUNCHER_TITLE)
    if not hwnd:
        return False
    _trigger_launcher_start(hwnd)
    return True


def launch_game():
    """启动游戏：运行 exe → 等待启动器窗口 → 置前 → 按回车进入。

    LÖVE 启动器的"开始"是默认按钮，窗口获得焦点后按回车即可触发，
    比按像素坐标点击稳得多（不受分辨率/DPI缩放/窗口被遮挡影响）。
    像素点击仅作为置前失败时的兜底。
    """
    user32 = ctypes.windll.user32
    _setup_win32_hwnd_types(user32)  # 修正 HWND 类型，避免 64 位句柄截断

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

    print("  [启动] 找到启动器窗口，等待渲染完成...")
    time.sleep(2)  # 等启动器入场动画 + 按钮画出来

    # 触发"开始"：焦点无关的 PostMessage 回车为主（直播后台进程也能用），
    # 能置前时再补一发全局回车冗余。是否真的进入游戏由调用方按"bridge 是否
    # 连上"判定并重试（见 kingdom_rush_bridge.py 的启动重试），这里只负责触发。
    _trigger_launcher_start(hwnd)
    print("  [启动] 已触发开始（PostMessage 回车），等待游戏加载...")
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

            # 3. 对战记录已在 ai.run() 内即时保存（防直播切环节丢记录），此处不再重复保存

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
