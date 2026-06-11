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
