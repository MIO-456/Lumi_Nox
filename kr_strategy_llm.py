"""
Kingdom Rush LLM 开局策略决策模块

使用 Doubao-Seed-2.0-lite 模型，从算法生成的多套开局方案中选择或微调。
每局开始前调用一次，15秒超时兜底。
"""

import json
import os
import time
import requests

# ========== API 配置 ==========
KR_API_KEY = os.environ.get("KR_ARK_API_KEY")
if not KR_API_KEY:
    raise RuntimeError("缺少 KR_ARK_API_KEY，请先在 .env 中配置")
KR_MODEL = "doubao-seed-2-0-lite-260215"
KR_API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
KR_TIMEOUT = 15  # 秒

# ========== 策略知识 ==========
STRATEGY_KNOWLEDGE = """## 王国保卫战策略知识

### 塔类型特点
- **弓箭塔**：物理伤害，攻速快，能打飞行怪（飞行怪只有弓箭塔和法师塔能攻击）
- **兵营**：产出士兵阻挡敌人，本身不输出伤害，关键路口必须有兵营挡住敌人
- **法师塔**：魔法伤害，攻速慢但穿甲，高物抗敌人用法师塔克制
- **炮塔**：物理AOE伤害，对群怪有效，路口覆盖多路径时价值最大

### 克制关系
- 飞行怪 → 弓箭塔应对（不是法师塔！弓箭塔攻速快更适合）
- 高物抗敌人 → 法师塔克制
- 高法抗敌人 → 弓箭塔/炮塔物理输出
- 群怪/小怪潮 → 炮塔AOE
- 快速敌人 → 兵营阻挡 + 高DPS集火

### 通用原则
- 每条活跃路径至少有一个兵营阻挡
- 路口（覆盖多条路径的位置）是最高价值塔位
- 开局先建弓箭塔性价比最高（70金币），除非有明确的法抗/群怪压力
- 升级比新建性价比更高，但开局需要先铺开防线
"""

# ========== 系统提示词 ==========
SYSTEM_PROMPT = f"""你是王国保卫战的开局策略顾问。你需要从候选方案中选择一个最优的开局建塔方案。

{STRATEGY_KNOWLEDGE}

### 你的任务
1. 分析候选方案的优劣
2. 参考历史对战记录（如果有），避免重复失败策略
3. 选择一个方案，可以微调其中某些塔位的建塔类型
4. 用 choose_opening_plan 工具返回你的选择

### 星数系统
- 3星(满星)：通关后剩余18命以上
- 2星：通关后剩余16-17命
- 1星：通关后剩余不足16命
- 目标是每关都拿到3星。如果历史记录显示已经赢了但没有拿到3星，说明当前策略不够好，必须换一种不同的方案来减少扣命。
- **重点**：赢了≠策略好。2星甚至1星的胜利意味着防线有漏洞，需要调整策略堵住突破点。

### 约束
- 不能选出与历史失败记录完全相同的方案组合（除非你做了微调使其不同）
- 如果历史有胜利但未满星的记录，必须选择与之前不同的方案或做出有针对性的微调
- 微调时只能改变已有塔位的类型，不能新增或删除塔位
- 可用塔类型：archer（弓箭塔）、barrack（兵营）、mage（法师塔）、engineer（炮塔）
"""

# ========== 工具定义 ==========
TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "choose_opening_plan",
        "description": "选择或调整开局建塔方案",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "string",
                    "description": "选择的方案编号(A/B/C/D/E)"
                },
                "adjustments": {
                    "type": "array",
                    "description": "可选，微调列表。修改某个塔位的建塔类型",
                    "items": {
                        "type": "object",
                        "properties": {
                            "holder_id": {"type": "string", "description": "要调整的塔位ID"},
                            "tower_type": {
                                "type": "string",
                                "enum": ["archer", "barrack", "mage", "engineer"],
                                "description": "改成什么塔类型"
                            }
                        },
                        "required": ["holder_id", "tower_type"]
                    }
                },
                "reasoning": {
                    "type": "string",
                    "description": "选择理由（1-2句话）"
                }
            },
            "required": ["plan_id", "reasoning"]
        }
    }
}

# 合法塔类型
VALID_TOWER_TYPES = {"archer", "barrack", "mage", "engineer"}

# 塔基础费用
_BUILD_COSTS = {"archer": 70, "barrack": 70, "mage": 100, "engineer": 125}


def _build_user_message(level_idx, level_mode, plans_text, history_records,
                        next_wave_summary, holders_desc=None, star_goal=None):
    """构建发给 LLM 的用户消息"""
    mode_names = {1: "普通", 2: "钢铁", 3: "英雄"}
    mode_str = mode_names.get(level_mode, str(level_mode))

    parts = [f"## 当前关卡：第{level_idx}关（{mode_str}模式）\n"]

    # 刷星目标提示
    if star_goal and history_records:
        best_stars = max((r.get("stars", 0) for r in history_records if r.get("result") == "win"), default=0)
        best_lives = max((r.get("final_lives", 0) for r in history_records if r.get("result") == "win"), default=0)
        if best_stars < star_goal:
            parts.append(f"### ⚠ 刷星目标\n"
                         f"这关之前赢过但只拿了{best_stars}星（最好成绩剩{best_lives}命），目标是{star_goal}星（需要剩18命以上）。\n"
                         f"**必须换一种不同的策略来减少扣命，不要沿用之前的方案！**\n"
                         f"请重点参考历史扣命记录，分析哪些位置防守薄弱，针对性调整塔的类型。\n")

    # 空塔位列表
    if holders_desc:
        parts.append(f"### 可用塔位\n{holders_desc}\n")

    # 第一波敌人预览
    if next_wave_summary:
        parts.append(f"### 第一波敌人预览\n{next_wave_summary}\n")

    # 候选方案
    parts.append(f"### 候选开局方案\n{plans_text}\n")

    # 历史记录
    if history_records:
        parts.append("### 历史对战记录\n")
        for i, rec in enumerate(history_records, 1):
            result = "✓胜利" if rec.get("result") == "win" else "✗失败"
            fw = rec.get("final_wave", "?")
            wt = rec.get("wave_total", "?")
            lives = rec.get("final_lives", "?")
            stars = rec.get("stars", 0)
            star_str = f" ({stars}星)" if rec.get("result") == "win" else ""
            parts.append(f"第{i}次：{result}{star_str} 波{fw}/{wt} 剩余{lives}命")

            # 开局方案
            plan = rec.get("opening_plan")
            if plan and isinstance(plan, list):
                label = rec.get("strategy_label", "")
                if label:
                    parts.append(f"  开局策略：{label}")
                for step in plan:
                    if isinstance(step, dict):
                        parts.append(f"  - {step.get('holder_id', '?')} → {step.get('tower_type', '?')}")

            # 扣命记录
            lost_events = rec.get("life_lost_log", rec.get("life_lost_events", []))
            if lost_events:
                parts.append("  扣命记录：")
                for evt in lost_events[:5]:  # 最多显示5条
                    tpl = evt.get("enemy_template", "未知")
                    w = evt.get("wave", "?")
                    parts.append(f"    波{w}: {tpl} 突破")
            parts.append("")
    else:
        parts.append("### 历史对战记录\n首次挑战该关卡，无历史记录。\n")

    return "\n".join(parts)


def call_llm_for_strategy(level_idx, level_mode, plans, plans_text,
                          history_records=None, next_wave_summary=None,
                          holders_desc=None, star_goal=None):
    """调用 LLM 选择开局策略

    返回 (chosen_plan, reasoning, elapsed_seconds) 或超时/错误时返回兜底方案。
    chosen_plan 是 plans 列表中某个元素的深拷贝（可能已应用微调）。
    """
    user_msg = _build_user_message(
        level_idx, level_mode, plans_text,
        history_records, next_wave_summary, holders_desc, star_goal)

    start_time = time.time()

    try:
        response = requests.post(
            KR_API_URL,
            headers={
                "Authorization": f"Bearer {KR_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": KR_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "tools": [TOOL_DEFINITION],
                "tool_choice": {"type": "function", "function": {"name": "choose_opening_plan"}},
                "reasoning_effort": "medium",
            },
            timeout=KR_TIMEOUT,
        )
        elapsed = time.time() - start_time
        response.raise_for_status()
        data = response.json()

        # 提取工具调用
        message = data.get("choices", [{}])[0].get("message", {})
        tool_calls = message.get("tool_calls", [])

        if not tool_calls:
            print(f"  [LLM策略] 未返回工具调用，使用兜底 ({elapsed:.1f}s)")
            return _fallback_plan(plans), "LLM未返回工具调用", elapsed

        args_str = tool_calls[0].get("function", {}).get("arguments", "{}")
        args = json.loads(args_str)

        plan_id = args.get("plan_id", "A").upper()
        adjustments = args.get("adjustments", [])
        reasoning = args.get("reasoning", "")

        # 解析方案编号
        idx = ord(plan_id) - ord('A')
        if idx < 0 or idx >= len(plans):
            idx = 0

        # 深拷贝选中方案
        chosen = json.loads(json.dumps(plans[idx]))
        chosen["strategy_label"] = chosen["label"]

        # 应用微调
        if adjustments:
            holder_map = {step["holder_id"]: step for step in chosen["build_sequence"]}
            total_cost = sum(step["cost"] for step in chosen["build_sequence"])
            gold_available = total_cost + chosen["remaining_gold"]

            for adj in adjustments:
                hid = adj.get("holder_id", "")
                new_type = adj.get("tower_type", "")
                if hid not in holder_map:
                    continue  # 塔位不在方案中，忽略
                if new_type not in VALID_TOWER_TYPES:
                    continue  # 非法类型，忽略
                old_cost = holder_map[hid]["cost"]
                new_cost = _BUILD_COSTS.get(new_type, old_cost)
                # 检查金币是否够用
                if total_cost - old_cost + new_cost <= gold_available:
                    total_cost = total_cost - old_cost + new_cost
                    holder_map[hid]["tower_type"] = new_type
                    holder_map[hid]["cost"] = new_cost

            chosen["remaining_gold"] = gold_available - total_cost

        print(f"  [LLM策略] 选择方案{plan_id}【{chosen['label']}】 ({elapsed:.1f}s)")
        if reasoning:
            print(f"  [LLM策略] 理由: {reasoning}")

        return chosen, reasoning, elapsed

    except requests.Timeout:
        elapsed = time.time() - start_time
        print(f"  [LLM策略] 超时 ({elapsed:.1f}s)，使用兜底方案")
        return _fallback_plan(plans), "LLM调用超时", elapsed

    except Exception as e:
        elapsed = time.time() - start_time
        print(f"  [LLM策略] 调用失败: {e} ({elapsed:.1f}s)，使用兜底方案")
        return _fallback_plan(plans), f"LLM调用失败: {e}", elapsed


def _fallback_plan(plans):
    """兜底：选第一个未被标记为与失败记录重复的方案，都被标记则用方案A"""
    for plan in plans:
        if not plan.get("warning"):
            chosen = json.loads(json.dumps(plan))
            chosen["strategy_label"] = chosen["label"]
            return chosen
    if plans:
        chosen = json.loads(json.dumps(plans[0]))
        chosen["strategy_label"] = chosen["label"]
        return chosen
    return {"label": "空", "build_sequence": [], "remaining_gold": 0, "strategy_label": "空"}


# ==========================================================================
# Phase 2：每波军师（LLM 看下一波的怪+图鉴说明，给算法一份"本波偏置"）
# ==========================================================================

WAVE_BIAS_SYSTEM_PROMPT = """你是王国保卫战的实时战术军师。你给一套已经很稳的算法做"本波偏置"微调，不是替它做主。

## 先懂算法的打法（很重要，别和它对着干）
算法按这个优先级花钱，这套打法在王国保卫战里就是赢的正道：
1. 覆盖底线：每条活跃路先保证"有兵营挡 + 有输出"，不漏穿
2. **集中升级**：把高覆盖路口的塔一路升满到 T4，再开下一座。**少数满级塔的火力远超一堆低级塔**——所以"集中升满"几乎总比"到处铺新塔"强
3. 铺满后才考虑买技能
**关键认知：到处铺低级塔是新手打法、会被怪冲垮。所以你一般不该让它狂建新塔；多数情况让它继续集中升级就对了。** 你的价值不是让它多建塔，而是"这一波该把钱往哪个方向偏"。

## 你的核心价值：用图鉴机制做克制（算法看不到这些）
怪的数值（血/甲/魔抗/速度）算法已知；但**图鉴说明里的机制**算法看不到，这才是你要发挥的：
- "高魔抗 + 会下崽/产幼蛛"群怪 → 物理 AOE 炮塔(engineer)，别用法师
- **"召唤型"（死灵法师召唤骷髅、产卵孵化幼崽等"会源源不断生出大量小怪"的）→ 那条路必须有炮塔(engineer) AOE 清小怪潮，同时 focus_tower 集火秒本体断召唤源。⚠️光"集火本体"挡不住源源不断的召唤物，会被淹——召唤型必须配 AOE！**
- "生命再生" → 高爆发集火秒掉，持续输出扛不住再生
- "闪避近战" → 兵营/士兵拦不住，靠塔直接输出
- "飞行" → 只有 archer/mage 能打，兵营炮塔打不到
- "激怒同胞/使友军暴怒" → 优先集火秒掉这个首领
- "高护甲" → mage 穿甲；"高魔抗" → 物理(archer/engineer)

## 你能看到的局面 + 怎么用各字段
给你的信息里有：当前每座塔(id/类型/级别/覆盖路)、空塔位(id/覆盖路/覆盖量)、下一波每条路出什么怪+图鉴、本局上波漏哪、**本关历史失败(上次第几波、被什么怪突破)**。
**最重点**：看到 **⚠️提前布防** 警告（说"距死亡波还有K波"），立刻在这一波就把克制那个突破怪的塔型写进 path_types、指到它会来的那条路——哪怕当前这波还用不上！算法会从现在起这几波持续把它堆到 T3，等死亡波到时已成型。临波才喊来不及升级，必被淹。这是你能不能翻盘的关键。
- **focus_tower**：本波想集中升哪座塔(给塔id)。这是你影响"集中升级"的主要手柄——比如这波路3来高甲，就 focus 路3那座法师塔，让它优先升满。
- **path_types**：按路给塔型，**权重很大**：你点名某路要某型而该路缺这型/没升到 T3 时，算法会高优先把它建出来并集中升到 T3（不是只温和倾斜）。所以这是你提前布防、补 AOE 缺口的主力手柄——大胆用，但别每路乱点，只点真正需要的。
- **abilities_now**：本波破例买某 T4 塔技能(应对特定威胁，如重甲boss提前点穿甲技能)。
- **t4_branch**：T3 塔升 T4 时走哪个分支(每种基础型有两个终极塔)。看到有塔快到 T3/已 T3、
  且未来怪情对路时再给——比如密集群怪/召唤多→炮塔走 tesla(闪电链)；成片重甲→走 bfg(大炮)；
  高血肉盾多→法师走 arcane_wizard(瓦解秒杀)；需要稳住挡线→兵营走 paladin(高坦)。不给走默认。
- **save_gold**：这波怪不强、下波很硬时，攒钱。

塔型：archer 弓箭(物理/打飞行/快)、engineer 炮塔(物理AOE/克群怪)、mage 法师(魔法/穿甲/打飞行/慢)、barrack 兵营(阻挡不输出)。

## 原则
- 默认信任算法的"集中升级"，主要用 focus_tower 指方向 + path_types 定塔型 + 图鉴克制。
- 不确定的字段就别填，算法走兜底。理由要短(会用于直播解说)。
"""

WAVE_BIAS_TOOL = {
    "type": "function",
    "function": {
        "name": "set_wave_bias",
        "description": "为下一波给出花钱偏置（不填的字段算法走兜底）",
        "parameters": {
            "type": "object",
            "properties": {
                "focus_tower": {
                    "type": "string",
                    "description": "本波优先集中升级哪座塔(塔id)，覆盖算法默认的承压最高选择",
                },
                "prefer_type": {
                    "type": "string",
                    "enum": ["archer", "engineer", "mage", "barrack"],
                    "description": "本波建/选型全局偏哪种塔（不分路；要分路用 path_types 更精确）",
                },
                "path_types": {
                    "type": "array",
                    "description": "按路指定塔型（比全局 prefer_type 精确）：哪条路该上什么塔。"
                                   "例：3号路是会下崽的高魔抗蜘蛛群→engineer；1号路飞行怪→archer。"
                                   "你能看到每条路出什么怪，请尽量按路给，发挥针对性。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "integer", "description": "路编号(下一波信息里的路号)"},
                            "type": {"type": "string",
                                     "enum": ["archer", "engineer", "mage", "barrack"]},
                        },
                        "required": ["path", "type"],
                    },
                },
                "abilities_now": {
                    "type": "array",
                    "description": "本波破例要买的 T4 塔特殊技能（覆盖'技能最后买'）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tower_id": {"type": "string"},
                            "ability": {"type": "string"},
                        },
                        "required": ["tower_id", "ability"],
                    },
                },
                "save_gold": {"type": "boolean", "description": "是否攒钱等更硬的波"},
                "t4_branch": {
                    "type": "array",
                    "description": "T3 塔升 T4 走哪个分支（每种基础塔型有两个终极选择，按怪情选）："
                                   "弓箭→ranger(单体毒,克高血重甲)/musketeer(狙击远程爆发+散射AOE)；"
                                   "兵营→barbarian(多兵近战输出)/paladin(高坦续航,挡硬怪不崩)；"
                                   "法师→arcane_wizard(瓦解秒高血)/sorcerer(变形召唤,控场)；"
                                   "炮塔→tesla(闪电链,克密集群怪)/bfg(大炮大范围高爆发)。不给则走默认。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string",
                                     "enum": ["archer", "engineer", "mage", "barrack"]},
                            "to": {"type": "string",
                                   "enum": ["ranger", "musketeer", "barbarian", "paladin",
                                            "arcane_wizard", "sorcerer", "tesla", "bfg"]},
                        },
                        "required": ["type", "to"],
                    },
                },
                "reason": {"type": "string", "description": "一句话理由（用于解说）"},
            },
            "required": ["reason"],
        },
    },
}

_BIAS_TOWER_TYPES = {"archer", "engineer", "mage", "barrack"}
# 各基础塔型合法的两个 T4 分支（短名）；解析后存全模板名 'tower_<短名>'
_T4_VALID = {
    "archer": {"ranger", "musketeer"},
    "barrack": {"barbarian", "paladin"},
    "mage": {"arcane_wizard", "sorcerer"},
    "engineer": {"tesla", "bfg"},
}


def decide_wave_bias(context_text, timeout=KR_TIMEOUT):
    """调用 LLM 为下一波生成"偏置"。

    context_text: 调用方(kingdom_rush_ai)拼好的局面文本——当前防线 / 下一波的怪
        (数值 + 图鉴说明) / 上波在哪漏命。
    返回 dict {focus_tower?, prefer_type?, path_types?, abilities_now?, save_gold?,
    t4_branch?, reason} 或 None（超时/报错/无工具调用 → 调用方退回纯算法骨架）。
    字段只做轻量类型清洗；塔位是否存在、金币够不够由执行方(算法)最终校验。
    """
    try:
        response = requests.post(
            KR_API_URL,
            headers={
                "Authorization": f"Bearer {KR_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": KR_MODEL,
                "messages": [
                    {"role": "system", "content": WAVE_BIAS_SYSTEM_PROMPT},
                    {"role": "user", "content": context_text},
                ],
                "tools": [WAVE_BIAS_TOOL],
                "tool_choice": {"type": "function", "function": {"name": "set_wave_bias"}},
                "reasoning_effort": "medium",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        message = data.get("choices", [{}])[0].get("message", {})
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            return None
        args = json.loads(tool_calls[0].get("function", {}).get("arguments", "{}"))
        return _clean_wave_bias(args)
    except requests.Timeout:
        print("  [LLM军师] 超时，本波退回算法骨架")
        return None
    except Exception as e:
        print(f"  [LLM军师] 调用失败: {e}，本波退回算法骨架")
        return None


def _clean_wave_bias(args):
    """轻量清洗 LLM 返回的偏置：丢掉类型非法的字段。"""
    bias = {}
    ft = args.get("focus_tower")
    if isinstance(ft, (str, int)):
        bias["focus_tower"] = str(ft)
    pt = args.get("prefer_type")
    if isinstance(pt, str) and pt in _BIAS_TOWER_TYPES:
        bias["prefer_type"] = pt
    ab = args.get("abilities_now")
    if isinstance(ab, list):
        cleaned = []
        for it in ab:
            if isinstance(it, dict) and it.get("tower_id") is not None and it.get("ability"):
                cleaned.append({"tower_id": str(it["tower_id"]), "ability": str(it["ability"])})
        if cleaned:
            bias["abilities_now"] = cleaned
    pts = args.get("path_types")
    if isinstance(pts, list):
        pt_map = {}
        for it in pts:
            if isinstance(it, dict):
                p, ty = it.get("path"), it.get("type")
                if isinstance(p, bool):
                    continue
                if isinstance(p, int) and ty in _BIAS_TOWER_TYPES:
                    pt_map[p] = ty
        if pt_map:
            bias["path_types"] = pt_map
    if isinstance(args.get("save_gold"), bool):
        bias["save_gold"] = args["save_gold"]
    t4b = args.get("t4_branch")
    if isinstance(t4b, list):
        t4_map = {}
        for it in t4b:
            if isinstance(it, dict):
                bt, to = it.get("type"), it.get("to")
                if bt in _T4_VALID and to in _T4_VALID[bt]:
                    t4_map[bt] = "tower_" + to  # 全模板名，供算法直接匹配分支
        if t4_map:
            bias["t4_branch"] = t4_map
    bias["reason"] = str(args.get("reason", ""))[:80]
    return bias
