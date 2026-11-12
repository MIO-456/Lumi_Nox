"""Buckshot prompt text helpers.

This module is intentionally pure: no LLM, TTS, VTS, or event bus imports.
It converts internal game state into Chinese-only text for model input.
"""

import re
import unicodedata

ITEM_NAME_CN = {
    "beer": "啤酒",
    "handsaw": "手锯",
    "handcuffs": "手铐",
    "magnifying glass": "放大镜",
    "cigarettes": "香烟",
    "expired medicine": "过期药",
    "burner phone": "一次性手机",
    "phone": "手机",
    "adrenaline": "肾上腺素",
    "inverter": "逆转器",
    "jammer": "干扰器",
}

# 官方多人模式当前会发放、且主播桥接已经能够原生操作的道具。
# handcuffs/expired medicine 属于单机规则，不能进入双人提示词。
DUEL_SUPPORTED_ITEMS = frozenset({
    "handsaw",
    "magnifying glass",
    "jammer",
    "cigarettes",
    "beer",
    "burner phone",
    "adrenaline",
    "inverter",
})

DUEL_STEALABLE_ITEMS = DUEL_SUPPORTED_ITEMS - {"adrenaline"}

DUEL_COMMON_STRATEGY_TEXT = """道具使用后通常仍可继续行动；有明确收益的道具可以在开枪前使用。
能确定结束本小局时优先结束本小局；血量危险且不能立即取胜时优先考虑稳定恢复。
放大镜查看当前弹种；一次性手机揭示后续随机位置，结果只属于实际使用者。
确定空弹时可以射击自己保留行动权，也可以用逆转器变成实弹；确定实弹时应考虑攻击对手。
手锯提高本次射击伤害；干扰器让对手跳过下一次行动；啤酒会退掉当前子弹。
肾上腺素会偷走指定的对手道具并立即使用，同时获得道具效果并拿走对手资源。
不要使用当前无效、重复生效或明显没有收益的道具。"""

SHELL_NAME_CN = {
    "live": "实弹",
    "blank": "空弹",
    "unknown": "未知",
}

ACTION_TO_COMMAND = {
    "射击庄家": {"action": "shoot", "target": "dealer"},
    "射击自己": {"action": "shoot", "target": "self"},
    "使用啤酒": {"action": "use_item", "item": "beer"},
    "使用手锯": {"action": "use_item", "item": "handsaw"},
    "使用手铐": {"action": "use_item", "item": "handcuffs"},
    "使用放大镜": {"action": "use_item", "item": "magnifying glass"},
    "使用香烟": {"action": "use_item", "item": "cigarettes"},
    "使用过期药": {"action": "use_item", "item": "expired medicine"},
    "使用一次性手机": {"action": "use_item", "item": "burner phone"},
    "使用手机": {"action": "use_item", "item": "burner phone"},
    "使用肾上腺素": {"action": "use_item", "item": "adrenaline"},
    "使用逆转器": {"action": "use_item", "item": "inverter"},
    "使用干扰器": {"action": "use_item", "item": "jammer"},
}

_CN_TO_ITEM = {
    cn_name: item
    for item, cn_name in ITEM_NAME_CN.items()
    if item in DUEL_SUPPORTED_ITEMS
}


def item_to_cn(item: str) -> str:
    return ITEM_NAME_CN.get(item, item)


def items_to_cn_text(items: list[str] | tuple[str, ...] | None) -> str:
    if not items:
        return "无"
    return "、".join(item_to_cn(item) for item in items)


def shell_to_cn(shell: str) -> str:
    return SHELL_NAME_CN.get(shell, "未知")


def build_available_actions(
    player_items: list[str] | tuple[str, ...] | None,
    *,
    opponent_name: str = "",
) -> list[str]:
    opponent_action = f"射击 {opponent_name.strip()}" if opponent_name.strip() else "射击庄家"
    actions = [opponent_action, "射击自己"]
    for item in player_items or []:
        cn = item_to_cn(item)
        action = f"使用{cn}"
        if action in ACTION_TO_COMMAND and action not in actions:
            actions.append(action)
    return actions


def _duel_item_is_usable(item: str, state) -> bool:
    if item not in DUEL_SUPPORTED_ITEMS:
        return False
    if item == "adrenaline":
        return True
    if item == "cigarettes":
        if bool(getattr(state, "player_wire_cut", False)):
            return False
        hp = int(getattr(state, "health_player", 0) or 0)
        hp_max = int(getattr(state, "max_health", 0) or 0)
        if hp_max > 0 and hp >= hp_max:
            return False
    if item == "jammer" and bool(getattr(state, "opponent_jammed", False)):
        return False
    if item == "handsaw" and bool(getattr(state, "barrel_sawed_off", False)):
        return False
    return True


def build_duel_available_actions(
    player_items: list[str] | tuple[str, ...] | None,
    opponent_items: list[str] | tuple[str, ...] | None,
    *,
    state,
    opponent_name: str,
) -> list[str]:
    """Build only actions that exist and are currently useful in hotseat duel."""
    opponent_action = f"射击 {opponent_name.strip()}" if opponent_name.strip() else "射击对手"
    actions = [opponent_action, "射击自己"]
    own_items = list(dict.fromkeys(player_items or []))

    for item in own_items:
        if item == "adrenaline" or not _duel_item_is_usable(item, state):
            continue
        action = f"使用{item_to_cn(item)}"
        if action in ACTION_TO_COMMAND and action not in actions:
            actions.append(action)

    for target_item in build_duel_adrenaline_targets(
        own_items,
        opponent_items,
        state=state,
    ):
        action = f"使用肾上腺素：{item_to_cn(target_item)}"
        if action not in actions:
            actions.append(action)
    return actions


def build_duel_adrenaline_targets(
    player_items: list[str] | tuple[str, ...] | None,
    opponent_items: list[str] | tuple[str, ...] | None,
    *,
    state,
) -> list[str]:
    """Return valid internal item names for the dedicated adrenaline tool."""
    if "adrenaline" not in (player_items or []):
        return []
    targets: list[str] = []
    for target_item in dict.fromkeys(opponent_items or []):
        if target_item not in DUEL_STEALABLE_ITEMS:
            continue
        if not _duel_item_is_usable(target_item, state):
            continue
        targets.append(target_item)
    return targets


def build_duel_action_descriptions(
    state,
    available_actions: list[str],
    *,
    opponent_name: str,
) -> dict[str, str]:
    """Describe deterministic immediate effects without inventing random/private results."""
    hp = int(getattr(state, "health_player", 0) or 0)
    hp_max = int(getattr(state, "max_health", 0) or 0)
    descriptions: dict[str, str] = {}
    item_effects = {
        "handsaw": "锯短枪管，提高下一次射击伤害；使用后仍可继续行动",
        "magnifying glass": "查看当前弹种，真实结果只交给你；使用后仍可继续行动",
        "jammer": "让对手跳过下一次行动；使用后仍可继续行动",
        "beer": "退掉当前子弹但不发射；使用后仍可继续行动",
        "burner phone": "随机揭示弹匣中后续一发，真实结果只交给你；使用后仍可继续行动",
        "inverter": "反转当前弹种；只有反转前已知时，才能推知反转后的弹种；使用后仍可继续行动",
    }
    if hp_max > 0:
        item_effects["cigarettes"] = (
            f"生命从 {hp} 恢复到 {min(hp_max, hp + 1)}；使用后仍可继续行动"
        )
    else:
        item_effects["cigarettes"] = "恢复1点生命；使用后仍可继续行动"

    for action in available_actions:
        command = command_from_chinese_action(action)
        if not command:
            continue
        if command.get("action") == "shoot":
            if command.get("target") == "self":
                descriptions[action] = "空弹时保留行动权，实弹时自己受伤"
            else:
                descriptions[action] = f"向{opponent_name or '对手'}开枪，结果由当前真实弹种决定"
            continue
        item = str(command.get("item") or "")
        stolen = str(command.get("steal_item") or "")
        if item == "adrenaline" and stolen:
            effect = item_effects.get(stolen, "立即获得该道具的原生效果")
            descriptions[action] = (
                f"消耗肾上腺素，偷走并立即使用{opponent_name or '对手'}的"
                f"{item_to_cn(stolen)}；{effect}"
            )
        elif item in item_effects:
            descriptions[action] = item_effects[item]
    return descriptions


def command_from_chinese_action(action_cn: str) -> dict | None:
    action_cn = unicodedata.normalize("NFKC", str(action_cn or "")).strip()
    adrenaline_match = re.fullmatch(
        r"(?:使用\s*)?肾上腺素(?P<target>.+)",
        action_cn,
    )
    if adrenaline_match:
        target_cn = adrenaline_match.group("target").strip()
        target_cn = re.sub(r"^目标道具\s*[:：]?\s*", "", target_cn)
        target_cn = target_cn.strip("()：:,， ")
        target_item = _CN_TO_ITEM.get(target_cn)
        if target_item in DUEL_STEALABLE_ITEMS:
            return {
                "action": "use_item",
                "item": "adrenaline",
                "steal_item": target_item,
            }
        return None
    command = ACTION_TO_COMMAND.get(action_cn)
    if command is None and action_cn.startswith("射击"):
        target = action_cn.removeprefix("射击").strip()
        if target and target != "自己":
            command = {"action": "shoot", "target": "dealer"}
    return dict(command) if command else None


def build_buckshot_duel_decision_prompt(
    *,
    state_text: str,
    actor_name: str,
    opponent_name: str,
    available_actions: list[str],
    action_descriptions: dict[str, str] | None = None,
    strategy_text: str = "",
) -> str:
    """Build the live two-player decision prompt from already-authoritative facts."""
    actor_name = str(actor_name or "").strip()
    opponent_name = str(opponent_name or "").strip()
    if not actor_name or not opponent_name:
        return "【恶魔轮盘·对打】角色信息不完整，本次不要行动。"
    action_descriptions = action_descriptions or {}
    action_lines = []
    for action in available_actions:
        description = str(action_descriptions.get(action) or "").strip()
        action_lines.append(f"- {action}" + (f"：{description}" if description else ""))
    actions_text = "\n".join(action_lines)
    strategy_block = str(strategy_text or "").strip()
    if strategy_block:
        strategy_block = f"\n\n通用策略知识：\n{strategy_block}"
    return (
        "【恶魔轮盘·对打】\n"
        f"对打玩家：{actor_name} 与 {opponent_name}\n"
        f"本回合行动者：{actor_name}\n\n"
        f"{str(state_text or '').strip()}\n\n"
        "当前可选动作：\n"
        f"{actions_text}"
        f"{strategy_block}\n\n"
        "决策要求：\n"
        "必须从上面的中文动作中选择一个，并且只调用一个工具。\n"
        "射击和普通道具使用选择动作工具；肾上腺素必须使用专用工具，并填写目标道具。\n"
        "你说的话必须和最终动作一致，最多一句短话。\n"
        "如果选择使用道具，只能说正在使用该道具，不能提前宣布随后会开枪。\n"
        "不要补写没有发生的射击、命中、弹种或道具结果。"
    )


# 真正的"战斗阶段"——双方轮流开枪，弹匣里有子弹、可以解说膛内情况。
COMBAT_PHASES = {"player_turn", "dealer_turn"}
# 非战斗阶段——还在加载场景、发道具、装新弹匣，这局还没开枪。
LOADING_PHASES = {"round_loading", "item_grabbing", "scene_changed", "death_recovery_main"}
WAITING_PHASES = {"waiting"}


def phase_to_cn(phase: str, controller_name: str = "操作者") -> str:
    """把游戏内部阶段标识翻成模型能直接看懂的中文。
    英文原词（如 round_loading / dealer_turn）直接塞进提示词时模型理解不稳，会乱说。"""
    if phase == "player_turn":
        return f"{controller_name} 的回合"
    return {
        "dealer_turn": "庄家的回合",
        "round_loading": "装弹中",
        "item_grabbing": "发道具阶段",
        "scene_changed": "场景切换中",
        "death_recovery_main": "复活中",
        "waiting": "等待中",
    }.get(phase, phase or "未知")


def build_loading_situation_block(*, speaker_role: str = "操作者", controller_name: str = "操作者") -> str:
    """加载 / 发道具 / 场景切换等非战斗阶段：这局还没开枪，绝对不要解说子弹。"""
    return (
        "游戏阶段：还在加载 / 发道具，这一局还没开始开枪。\n\n"
        "表演方向：\n"
        "可以暖场、活跃气氛、表达期待，或调侃马上要开始的对局。\n\n"
        "输出限制：\n"
        "只说一句短话，最多 30 个字。\n"
        "不要解说子弹、实弹空弹、膛内情况——现在还没开枪。\n"
        "不要说自己开了枪、打了谁，也不要编造任何已经发生的对局动作。\n"
        "不要说工具名、函数名、英文参数。\n"
        "不要编造当前局面里没有写的事件。"
    )


def build_waiting_situation_block(
    *,
    controller_name: str = "操作者",
    safe_state: dict | None = None,
) -> str:
    """动作结算中的 waiting：只在弹匣仍权威时给观众可核实的轻量战况。"""
    if safe_state:
        hp = int(safe_state.get("hp", 0) or 0)
        hp_opp = int(safe_state.get("hp_opp", 0) or 0)
        hp_max = int(safe_state.get("hp_max", 0) or 0)
        live = int(safe_state.get("live", 0) or 0)
        blank = int(safe_state.get("blank", 0) or 0)
        total = live + blank
        phase_block = (
            "游戏阶段：当前动作或动画正在结算，下一步尚未确认。\n\n"
            "可安全回应观众的当前快照：\n"
            f"{controller_name} 的血量：{hp}/{hp_max}\n"
            f"庄家的血量：{hp_opp}/{hp_max}\n"
            f"弹匣剩余：共{total}发（{live}发实弹，{blank}发空弹）\n\n"
            "事实边界：\n"
            "这是当前快照，不代表刚刚发生了新的动作。\n"
            "如果观众问战况，只能使用上面的数字；没有写出的信息就自然说明尚未确认。\n\n"
        )
    else:
        phase_block = (
            "游戏阶段：当前处于回合或阶段过渡，新的有效局面尚未确认。\n"
            "旧血量、旧弹匣和旧道具都不能用于回答现在的战况。\n\n"
            "表演方向：\n"
            "可以自然回应观众；如果观众追问战况，用自己的语气说明新局面还没有确认。\n\n"
        )
    return (
        phase_block
        + "输出限制：\n"
        "只说一句短话，最多 30 个字。\n"
        "不要声称自己或庄家刚刚做了任何未写明的动作。\n"
        "不要宣布下一步动作，不要说自己或庄家将要开枪、使用道具或检查弹膛。\n"
        "不要判断当前膛内是实弹还是空弹。\n"
        "不要编造尚未收到的动作结果。"
    )


def build_unknown_phase_block(*, phase: str = "") -> str:
    """Fail closed for foreign/new/invalid phases."""
    return (
        "游戏阶段：当前阶段暂时无法确认，不能据此判断任何动作。\n\n"
        "表演方向：\n"
        "可以回应观众，或简短表示先等游戏给出明确结果。\n\n"
        "输出限制：\n"
        "只说一句短话，最多 30 个字。\n"
        "不要描述开枪、道具、弹膛、实弹或空弹。\n"
        "不要宣布下一步动作，也不要编造已经发生的游戏事件。"
    )


def build_dealer_turn_block(
    *,
    speaker_role: str,
    controller_name: str,
    player_cuffed: bool = False,
) -> str:
    """庄家回合 / 操作者被铐跳过：现在轮不到你，你只能旁观，别说自己要行动。"""
    if speaker_role == "操作者":
        if player_cuffed:
            lead = "你被庄家铐住了，这一回合你被跳过，只能眼睁睁看着庄家行动。"
        else:
            lead = "现在是庄家在行动，不是你的回合。"
        return (
            "游戏阶段：庄家的回合。\n"
            f"{lead}\n\n"
            "表演方向：\n"
            "你只能旁观庄家这一手，可以紧张、期待、调侃庄家，或预判他会怎么打。\n\n"
            "输出限制：\n"
            "只说一句短话，最多 30 个字。\n"
            "不要说你要开枪、用道具、做决定——现在轮不到你。\n"
            "不要复述游戏规则。\n"
            "不要说工具名、函数名、英文参数。\n"
            "不要说概率数字。\n"
            "不要编造当前局面里没有写的事件。"
        )
    return (
        "游戏阶段：庄家的回合。\n"
        f"现在是庄家在行动，{controller_name} 也只能在旁边看。\n\n"
        "表演方向：\n"
        f"你可以吐槽庄家、替 {controller_name} 紧张、或预判庄家会怎么打。\n\n"
        "输出限制：\n"
        "只说一句短话，最多 30 个字。\n"
        f"不要替 {controller_name} 或自己做决定——现在轮到庄家。\n"
        "不要复述游戏规则。\n"
        "不要说工具名、函数名、英文参数。\n"
        "不要说概率数字。\n"
        "不要复述或心算血量、实弹数、空弹数和弹匣总数；只表达情绪，不把推测说成事实。\n"
        "不要编造当前局面里没有写的事件。"
    )


def build_common_identity_block(
    *,
    controller_name: str,
    spectator_name: str = "",
    speaker_name: str,
    speaker_role: str,
) -> str:
    if spectator_name:
        return (
            "现在正在直播玩恶魔轮盘。\n"
            f"旁观者是：{spectator_name}\n"
            f"操作者是：{controller_name}\n"
            f"你是：{speaker_name}\n"
            f"你现在的身份：{speaker_role}"
        )
    return (
        "现在正在直播玩恶魔轮盘。\n"
        f"操作者是：{controller_name}\n"
        f"你是：{speaker_name}\n"
        "你现在的身份：操作者"
    )


def build_state_block(*, controller_name: str, buckshot_state: dict) -> str:
    live_count = int(buckshot_state.get("live_count", 0) or 0)
    blank_count = int(buckshot_state.get("blank_count", 0) or 0)
    total_count = live_count + blank_count
    wire_cut_line = (
        f"\n{controller_name} 的断线状态：是，香烟和过期药无法回血。"
        if buckshot_state.get("player_wire_cut")
        else ""
    )
    return (
        f"当前阶段：{buckshot_state.get('current_phase', '未知')}\n"
        f"游戏状态：{buckshot_state.get('game_status', '进行中')}\n\n"
        "当前局面：\n"
        f"{controller_name} 的血量：{buckshot_state.get('controller_hp', '?')}\n"
        f"庄家的血量：{buckshot_state.get('dealer_hp', '?')}\n"
        f"弹匣剩余：共{total_count}发（{live_count}发实弹，{blank_count}发空弹）\n"
        f"已知情报：{buckshot_state.get('known_intel') or '当前膛内未知'}\n"
        f"{controller_name} 的道具：{buckshot_state.get('controller_items') or '无'}\n"
        f"庄家的道具：{buckshot_state.get('dealer_items') or '无'}"
        f"{wire_cut_line}"
    )


def build_last_action_block(last_action_result: str = "") -> str:
    last_action_result = (last_action_result or "").strip()
    if not last_action_result:
        return ""
    return f"上一动作结果：\n{last_action_result}"


def build_rules_block() -> str:
    return (
        "游戏规则：\n"
        "实弹射庄家会伤害庄家。\n"
        "空弹射庄家不会造成伤害。\n"
        "空弹射自己不会扣血，并继续由自己行动。\n"
        "实弹射自己会扣自己的血，并结束行动。\n"
        "射庄家无论实弹还是空弹，都会结束当前行动。\n"
        "只能根据上面的当前局面说话，不要补编更早之前发生的事。"
    )


def build_fact_boundary_block() -> str:
    return (
        "事实边界：\n"
        "这里只提供当前局面，不代表刚刚发生了新的动作。\n"
        "只能根据上面的数字和已知情报说话，不要引用旧对话里的枪击、弹种、血量或道具结果。\n"
        "如果要说弹匣数量，必须使用上面明确写出的总数，不得心算成其他数量。"
    )


def build_spectator_diff_block(*, controller_name: str) -> str:
    return (
        "表演方向：\n"
        f"你在旁边看 {controller_name} 玩。你可以吐槽、紧张、幸灾乐祸、提醒风险，"
        f"但不能替 {controller_name} 做决定。\n\n"
        "输出限制：\n"
        "只说一句短话，最多 30 个字。\n"
        "不要复述游戏规则。\n"
        "不要说“要么…要么…”“还是…吧”“不对”“等下”这种纠结、反复、自我纠正式的句子。\n"
        "直接说出你当下的判断、想法或情绪，一句话讲清楚。\n"
        "不要说“我开枪”“我使用道具”“我决定”。\n"
        "不要说工具名、函数名、英文参数。\n"
        "不要说概率数字。\n"
        "不要复述或心算血量、实弹数、空弹数和弹匣总数。\n"
        "不要编造当前局面里没有写的事件。"
    )


def build_controller_diff_block(*, available_actions: list[str] | None) -> str:
    actions_text = "\n".join(f"- {action}" for action in (available_actions or []))
    return (
        f"可选动作：\n{actions_text}\n\n"
        "表演方向：\n"
        "这是你在操作。你可以用第一人称短句说自己的判断，但必须和最终选择的动作一致。\n\n"
        "输出限制：\n"
        "必须从“可选动作”里选择一个动作。\n"
        "必须调用对应工具。\n"
        "说出口的话必须和工具动作一致，最多 30 个字。\n"
        "不要复述游戏规则。\n"
        "不要说“要么…要么…”“还是…吧”“不对”“等下”这种纠结、反复、自我纠正式的句子。\n"
        "直接说出你的判断或想法，一句话讲清楚，不要把推理过程念出来。\n"
        "不要说工具名、函数名、英文参数。\n"
        "不要说概率数字。\n"
        "不要复述或心算血量、实弹数、空弹数和弹匣总数。\n"
        "使用道具时只说使用该道具，不能顺带宣布随后已经开枪或命中。\n"
        "不要编造当前局面里没有写的事件。"
    )


def build_special_situation_block(
    *,
    game_status: str,
    speaker_role: str,
    controller_name: str,
) -> str:
    if game_status == "失败":
        if speaker_role == "操作者":
            return (
                "游戏状态：失败\n"
                "结果：你已经死亡，这局输了。\n\n"
                "表演方向：\n"
                "演出死亡后的反应，可以不甘心、震惊、嘴硬或想复仇。\n\n"
                "输出限制：\n"
                "不要说自己还在操作。\n"
                "不要提射击、道具、下一步决策。\n"
                "不要编造新的游戏动作。"
            )
        return (
            "游戏状态：失败\n"
            f"结果：{controller_name} 已经死亡，这局输了。\n\n"
            "表演方向：\n"
            f"你在旁边看见 {controller_name} 输了。可以吐槽、安慰、嘲笑或补刀。\n\n"
            "输出限制：\n"
            "只说一句短话。\n"
            "不要把死亡说成自己死亡。\n"
            "不要说自己还在操作。\n"
            "不要编造新的游戏动作。"
        )
    if game_status == "胜利":
        if speaker_role == "操作者":
            return (
                "游戏状态：胜利\n"
                "结果：你击败了庄家，这局赢了。\n\n"
                "表演方向：\n"
                "庆祝、得意、嘲讽庄家。\n\n"
                "输出限制：\n"
                "不要继续做游戏决策。\n"
                "不要编造新的射击或道具动作。"
            )
        return (
            "游戏状态：胜利\n"
            f"结果：{controller_name} 击败了庄家，这局赢了。\n\n"
            "表演方向：\n"
            f"你在旁边看见 {controller_name} 赢了。可以欢呼、吐槽、夸张捧场。\n\n"
            "输出限制：\n"
            "不要说成自己赢了。\n"
            "不要继续做游戏决策。"
        )
    return ""


def build_buckshot_prompt_blocks(
    *,
    controller_name: str,
    spectator_name: str = "",
    speaker_name: str,
    speaker_role: str,
    buckshot_state: dict,
    available_actions: list[str] | None = None,
) -> str:
    parts = [
        build_common_identity_block(
            controller_name=controller_name,
            spectator_name=spectator_name,
            speaker_name=speaker_name,
            speaker_role=speaker_role,
        )
    ]
    game_status = buckshot_state.get("game_status", "进行中")
    special = build_special_situation_block(
        game_status=game_status,
        speaker_role=speaker_role,
        controller_name=controller_name,
    )
    if special:
        parts.append(special)
    else:
        parts.append(build_state_block(controller_name=controller_name, buckshot_state=buckshot_state))
        last_action = build_last_action_block(buckshot_state.get("last_action_result", ""))
        if last_action:
            parts.append(last_action)
        parts.append(build_fact_boundary_block())
        if speaker_role == "旁观者":
            parts.append(build_spectator_diff_block(controller_name=controller_name))
        else:
            parts.append(build_controller_diff_block(available_actions=available_actions))
    return "\n\n".join(part for part in parts if part)


def build_buckshot_duel_context(snapshot: dict, speaker: str, slot_provider) -> str:
    """Speaker-relative duel snapshot summary (hotseat).

    snapshot comes from parse_mp_state(): {
        "current_slot", "round_index",
        "players": {slot: {"name", "health", "items"}},
        "shells": {"live", "blank", "remaining"},
    }
    """
    if not snapshot:
        return "【恶魔轮盘·对打】游戏快照暂不可用。"
    players = snapshot.get("players", {})
    own = next((p for p in players.values() if p.get("name") == speaker), None)
    opponent = next((p for p in players.values() if p.get("name") != speaker), None)
    if own is None or opponent is None:
        return "【恶魔轮盘·对打】局面无法确认，不要猜测。"
    current = snapshot.get("current_slot")
    my_turn = (current is not None and players.get(current, {}).get("name") == speaker)
    shells = snapshot.get("shells", {})
    lines = [
        f"【恶魔轮盘·对打 第{snapshot.get('round_index', '?')}回合】你（{speaker}）和 {opponent.get('name', '对方')} 都是实际对局玩家。",
        f"你的血量 {own.get('health', '?')}，道具 {items_to_cn_text(own.get('items')) or '无'}；"
        f"对手 {opponent.get('name', '对方')} 血量 {opponent.get('health', '?')}，道具 {items_to_cn_text(opponent.get('items')) or '无'}。",
        f"弹匣剩余 {shells.get('remaining', '?')}（实弹 {shells.get('live', '?')} / 空弹 {shells.get('blank', '?')}）。",
        "现在是你的回合，可以选择动作并做出决定。" if my_turn
        else f"现在是 {opponent.get('name', '对方')} 的回合，你只能旁观，不要替他做决定。",
        "只根据快照自然说话，不要说技术词，不要编造没有发生的射击或道具结果。",
    ]
    return "\n".join(lines)
