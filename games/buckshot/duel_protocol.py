"""Pure functions for the Buckshot Roulette hotseat duel protocol.

No game process or LLM dependency — these normalize BridgeMod MP game_state
payloads and translate duel events into role-named text. The slot->role
mapping is provided by a slot_provider callable (default fixed mapping for
phase 1; director becomes the source of truth later).
"""

from __future__ import annotations


DUEL_SLOTS = (1, 3)


def _slot(value) -> int | None:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return None
    return resolved if resolved in DUEL_SLOTS else None


def opponent_slot(slot) -> int | None:
    resolved = _slot(slot)
    if resolved is None:
        return None
    return 3 if resolved == 1 else 1


def slot_name(slot, slot_provider) -> str:
    resolved = _slot(slot)
    if resolved is None:
        return ""
    try:
        return str(slot_provider(resolved) or "").strip()
    except (KeyError, TypeError, ValueError):
        return ""


def parse_mp_state(raw: dict) -> dict:
    """Normalize a BridgeMod mp_main game_state payload into a dual-player snapshot.

    Returns: {
        "current_slot": int | None,
        "round_index": int | None,
        "players": {slot: {"name", "health", "items", "jammed"}},
        "shells": {"live", "blank", "remaining"},
    }
    """
    players = {
        int(p["slot"]): {
            "name": str(p.get("name", "")),
            "health": int(p.get("health", 0)),
            "items": list(p.get("items", [])),
            "jammed": bool(p.get("jammed", False)),
        }
        for p in raw.get("players", [])
    }
    return {
        "current_slot": raw.get("current_slot"),
        "round_index": raw.get("round_index"),
        "total_rounds": raw.get("total_rounds"),
        "ready": bool(raw.get("ready", False)),
        "barrel_sawed_off": bool(raw.get("barrel_sawed_off", False)),
        "shotgun_damage": int(raw.get("shotgun_damage") or 1),
        "players": players,
        "shells": dict(raw.get("shells", {})),
    }


def make_default_slot_provider():
    """Phase-1 fixed mapping; later overridden by director-provided players."""
    default = {1: "Lumi", 3: "Nox"}
    return lambda slot: default.get(int(slot), "") if str(slot).lstrip("-").isdigit() else ""


def build_duel_event_metadata(msg: dict, slot_provider) -> dict:
    """Return role metadata for a confirmed hotseat event.

    Unknown/placeholder slots deliberately resolve to empty names so downstream
    speech fails closed instead of inventing ``玩家5``.
    """
    event = str(msg.get("event") or "")
    actor_slot = None
    target_slot = None
    preferred_slot = None

    if event == "turn_changed":
        actor_slot = _slot(msg.get("current_slot"))
        target_slot = opponent_slot(actor_slot)
        preferred_slot = actor_slot
    elif event == "shot_result":
        actor_slot = _slot(msg.get("from_slot"))
        target_slot = _slot(msg.get("to_slot"))
        preferred_slot = target_slot
    elif event == "item_used":
        actor_slot = _slot(msg.get("slot"))
        target_slot = opponent_slot(actor_slot)
        preferred_slot = actor_slot
    elif event == "player_won":
        actor_slot = _slot(msg.get("slot"))
        target_slot = opponent_slot(actor_slot)
        preferred_slot = target_slot

    return {
        "actor_slot": actor_slot,
        "actor_name": slot_name(actor_slot, slot_provider),
        "target_slot": target_slot,
        "target_name": slot_name(target_slot, slot_provider),
        "preferred_speaker": slot_name(preferred_slot, slot_provider),
    }


def format_duel_game_event(msg: dict, slot_provider) -> str:
    """Translate a duel game_event into role-named Chinese commentary text."""
    event = msg.get("event")
    name = lambda slot: slot_name(slot, slot_provider)
    if event == "shot_result":
        from_name = "无法确认" if str(msg.get("from_slot")) == "unknown" else name(msg.get("from_slot"))
        to_name = name(msg.get("to_slot"))
        if not from_name or not to_name:
            return ""
        shell = "实弹" if msg.get("shell") == "live" else "空弹"
        damage = int(msg.get("damage", 0))
        if damage > 0:
            return f"{from_name} 射击 {to_name}，是{shell}，扣{damage}点血（{to_name}剩余{msg.get('hp_remaining', '?')}点血）"
        return f"{from_name} 射击 {to_name}，是{shell}，没有造成伤害"
    if event == "turn_changed":
        current_name = name(msg.get("current_slot"))
        return f"轮到 {current_name} 行动" if current_name else ""
    if event == "round_started":
        return f"新一轮开始（第{msg.get('round_index', '?')}轮）"
    if event == "player_won":
        winner = name(msg.get("slot"))
        return f"{winner} 赢得了本局！" if winner else ""
    if event == "round_complete":
        return "本局结束，进入结算。"
    if event == "item_used":
        actor = name(msg.get("slot"))
        if not actor:
            return ""
        raw_item = str(msg.get("item") or "道具")
        item = raw_item
        try:
            from .bridge import item_to_cn
            item = item_to_cn(item)
        except ImportError:
            pass
        if raw_item == "handsaw":
            return f"{actor} 用手锯锯短了枪管，下一发射击伤害提高"
        if raw_item == "inverter":
            return f"{actor} 用逆转器翻转了当前子弹的性质，具体弹种未公开"
        if raw_item == "magnifying glass":
            return f"{actor} 用放大镜查看了当前子弹，结果只有 {actor} 知道"
        if raw_item == "burner phone":
            if msg.get("intel_revealed") is False:
                return f"{actor} 用了一次性手机，但没有获得有效弹匣情报"
            return f"{actor} 用一次性手机获得了一条私密弹匣情报，内容只有 {actor} 知道"
        if raw_item == "jammer":
            target = name(opponent_slot(msg.get("slot")))
            return f"{actor} 用干扰器干扰了 {target}，{target} 将跳过下一次行动" if target else f"{actor} 使用了{item}"
        if raw_item == "adrenaline":
            stolen = str(msg.get("stolen_item") or "").strip()
            if stolen:
                try:
                    from .bridge import item_to_cn
                    stolen = item_to_cn(stolen)
                except ImportError:
                    pass
                return f"{actor} 用肾上腺素偷用了对手的{stolen}"
        if raw_item == "beer":
            return f"{actor} 喝啤酒退出了当前一发子弹，弹种未公开"
        if raw_item in {"cigarettes", "expired medicine"}:
            before = int(msg.get("health_before") or 0)
            after = int(msg.get("health_after") or 0)
            if after > before:
                return f"{actor} 使用了{item}，恢复了{after - before}点血"
            if after < before:
                return f"{actor} 使用了{item}，反而失去了{before - after}点血"
        return f"{actor} 使用了{item}"
    return ""
