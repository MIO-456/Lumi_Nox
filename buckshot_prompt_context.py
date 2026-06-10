"""Buckshot prompt text helpers.

This module is intentionally pure: no LLM, TTS, VTS, or event bus imports.
It converts internal game state into Chinese-only text for model input.
"""

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
}

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
}


def item_to_cn(item: str) -> str:
    return ITEM_NAME_CN.get(item, item)


def items_to_cn_text(items: list[str] | tuple[str, ...] | None) -> str:
    if not items:
        return "无"
    return "、".join(item_to_cn(item) for item in items)


def shell_to_cn(shell: str) -> str:
    return SHELL_NAME_CN.get(shell, "未知")


def build_available_actions(player_items: list[str] | tuple[str, ...] | None) -> list[str]:
    actions = ["射击庄家", "射击自己"]
    for item in player_items or []:
        cn = item_to_cn(item)
        action = f"使用{cn}"
        if action in ACTION_TO_COMMAND and action not in actions:
            actions.append(action)
    return actions


def command_from_chinese_action(action_cn: str) -> dict | None:
    command = ACTION_TO_COMMAND.get((action_cn or "").strip())
    return dict(command) if command else None


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
    return (
        f"当前阶段：{buckshot_state.get('current_phase', '未知')}\n"
        f"游戏状态：{buckshot_state.get('game_status', '进行中')}\n\n"
        "当前局面：\n"
        f"{controller_name} 的血量：{buckshot_state.get('controller_hp', '?')}\n"
        f"庄家的血量：{buckshot_state.get('dealer_hp', '?')}\n"
        f"弹匣剩余：{buckshot_state.get('live_count', 0)}发实弹，"
        f"{buckshot_state.get('blank_count', 0)}发空弹\n"
        f"已知情报：{buckshot_state.get('known_intel') or '当前膛内未知'}\n"
        f"{controller_name} 的道具：{buckshot_state.get('controller_items') or '无'}\n"
        f"庄家的道具：{buckshot_state.get('dealer_items') or '无'}"
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
        # 规则块不再随提示词下发：模型每次拼到 user prompt 里反而会被复述出来。
        # 规则保留在测试和兜底逻辑里（build_rules_block 仍可单独调用），但跑 LLM 时只靠
        # system 人设描述 + diff_block 里的"不要复述规则"约束，避免出现"不对、规则里…"的纠结。
        if speaker_role == "旁观者":
            parts.append(build_spectator_diff_block(controller_name=controller_name))
        else:
            parts.append(build_controller_diff_block(available_actions=available_actions))
    return "\n\n".join(part for part in parts if part)
