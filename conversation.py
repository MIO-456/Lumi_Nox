"""
对话主循环模块 — 从 lumi.py 抽出的对话核心逻辑。

包含（后续 task 逐个迁入）：
- _stream_llm_text_only：LLM 流式生成 + TTS 流式播放
- _send_warmup_request：豆包隐式缓存预热
- chat_and_speak：用户输入 → 完整回应流程
- proactive_speak：沉默触发的主动说话

依赖通过 ConversationContext 传入，不直接 import lumi 的全局，避免循环依赖。
ConversationContext 字段在每次实际迁移函数（Task 8/9/10/11）时按需扩充。
"""
import json
import threading
import time
from dataclasses import dataclass, field
from threading import Lock, Event
from typing import Any, Callable

import fast_brain
from fast_brain import LLM_TEMPERATURE
import lumi_tts
import speech_output_arbiter
import web_search
from viewer_name import normalize_viewer_name
from buckshot_prompt_context import (
    COMBAT_PHASES,
    LOADING_PHASES,
    build_available_actions,
    build_buckshot_prompt_blocks,
    build_common_identity_block,
    build_dealer_turn_block,
    build_last_action_block,
    build_loading_situation_block,
    build_rules_block,
    build_spectator_diff_block,
    build_state_block,
    items_to_cn_text,
    phase_to_cn,
)
from lumi_tts import _rescue_tool_call_json

# 终端颜色（与 lumi.py 保持一致，在此独立定义避免循环导入）
C_FAST = "\033[36m"          # 青色 — 快脑
C_EMOTION = "\033[38;5;213m" # 粉色 — 情感标签
C_ERR = "\033[31m"           # 红色 — 错误
C_RESET = "\033[0m"


def _speech_policy_for_turn(source: str, *, game_request=None,
                            pending_commentary_request=None) -> str:
    if source == "proactive" and not game_request and not pending_commentary_request:
        return speech_output_arbiter.POLICY_DROP
    return speech_output_arbiter.POLICY_INTERRUPT


def mirror_speech_to_partner(active_speakers: list,
                              fast_brains: dict,
                              speaker: str,
                              text: str) -> None:
    """把 speaker 刚才说的话以 [speaker说] xxx 形式镜像到其他角色的 history。

    单角色场景（active_speakers 只 1 项）：no-op。
    双角色场景：把 [Lumi说] xxx 加到 Nox 的 user history（反之亦然）。
    多角色（>2，未来扩展）：广播到除自己之外的所有角色。

    与提示词约定一致（Lumi V4 / Nox V1 都说"对方发言以 [对方说] 形式出现"）。
    """
    if len(active_speakers) <= 1:
        return
    mirrored = f"[{speaker}说] {text}"
    for other in active_speakers:
        if other != speaker and other in fast_brains:
            fast_brains[other].append_user(mirrored)


def build_realtime_mirror_qa(partner_name: str,
                              user_query: str,
                              partner_reply: str) -> list:
    """构造跨角色端到端镜像 QA 对（双层包装格式）。

    用于 realtime_chat.sync_history(next_speaker, qa) 的 qa 参数。

    背景：直接把 [{user: 用户原话}, {assistant: 搭档回应}] 注入会触发"身份错位"
    —— 端到端 session 把 assistant 字段当成"自己说过的话"，搭档的偏好被吞进
    自己的人设，被问"搭档说了什么"时编造（dtd_test 实测确认，2026-05-08）。

    解法：把整段对话装进 user 字段（按 [直播提示] 旁白格式，端到端 SC2.0 训练
    过的提示模式），assistant 字段填占位承接话。模型把它理解成"导演告诉我搭档
    刚才和用户的对话内容"，不会和自己人设混淆。

    与提示词协作段落配套（各角色的人设提示词 / Nox 同款里要求识别
    [直播提示] 前缀作为旁白信号）。
    """
    user_text = (
        f"[直播提示] 你的搭档 {partner_name} 刚才和用户聊了：\n"
        f"  用户问：{user_query}\n"
        f"  {partner_name} 答：{partner_reply}"
    )
    return [
        {"role": "user", "text": user_text},
        {"role": "assistant", "text": "好的，我知道了。"},
    ]


def resolve_game_controller(ctx) -> str:
    ctrl = (ctx.get_current_game_controller() or "").strip()
    if ctrl and ctrl in ctx.active_speakers:
        return ctrl
    return ""


def resolve_game_spectator(ctx) -> str:
    ctrl = resolve_game_controller(ctx)
    if len(ctx.active_speakers) <= 1 or not ctrl:
        return ""
    return next((name for name in ctx.active_speakers if name != ctrl), "")


def should_stage_game_commentary(ctx, game_request) -> bool:
    if not game_request or len(ctx.active_speakers) <= 1:
        return False
    if not resolve_game_controller(ctx) or not resolve_game_spectator(ctx):
        return False
    # 不再用 6 秒冷却限制围观频率——冷却会把"操作者不说话时连走的那几手"的围观全挤掉，
    # 退化成"围观→操作操作操作→围观"。改成每个决策请求都先安排一次围观（per-request 的
    # _spectator_commentary_done 标志保证每手只围观一次），形成严格"围观→操作"每手轮次。
    # 单角色由上面 active_speakers<=1 拦掉，退化为"操作者一手一句"，不受影响。
    return not getattr(game_request, "_spectator_commentary_done", False)


def mark_game_commentary_done(game_request) -> None:
    if game_request:
        setattr(game_request, "_spectator_commentary_done", True)


def append_game_role_addon(ctx, system_content: str, ai_speaker: str) -> str:
    if len(ctx.active_speakers) <= 1:
        return system_content
    ctrl = resolve_game_controller(ctx)
    if not ctrl:
        return system_content
    cfg = ctx.speaker_configs[ai_speaker]
    if ctrl == ai_speaker:
        return system_content + "\n" + cfg.game_role_addon_controller
    return system_content + "\n" + cfg.game_role_addon_spectator


def is_game_controller(ctx, ai_speaker: str) -> bool:
    """ai_speaker 在当前游戏环节是否是操控者。
    单角色场景永远 True；双角色场景按 director 配置的 controller 判断；
    双角色但 controller 未解析时默认 False（围观更安全，避免误调工具）。
    """
    if len(ctx.active_speakers) <= 1:
        return True
    return resolve_game_controller(ctx) == ai_speaker


def build_role_reminder_suffix(ctx, ai_speaker: str) -> str:
    """游戏环节双角色场景下，给 user message 末尾追加身份硬提醒。

    用 user 消息末尾位置（last instruction）压制 V1/V4 人设的代入冲动；
    单角色 / 非游戏 / controller 未配置时返回空字符串，不污染普通对话。
    """
    ctrl = resolve_game_controller(ctx)
    if not ctrl:
        return ""
    if ctrl == ai_speaker:
        return "\n\n（提醒：本轮你是操控者，所有动作都是你做的，用第一人称说话。）"
    return "\n\n（提醒：本轮你是围观者，操作都是对方做的；用第二人称指对方，不要把自己当玩家。）"


def build_spectator_game_context(game_label: str, game_request) -> str:
    # 旁观者优先用不剧透选词的 spectator_text；没有则退回 state_text（操作者视角，含选词）。
    # 这避免旁观者抢在操作者出招前把对方要选的词念出来（穿帮）。
    text = getattr(game_request, "spectator_text", "") or game_request.state_text
    return f"\n\n[{game_label} 当前局面]\n{text}"


def is_buckshot_game_request(game_label: str, game_request) -> bool:
    return bool(game_request and game_label == "恶魔轮盘")


def _available_actions_from_tools(game_request) -> list[str]:
    try:
        props = game_request.tools[0]["function"]["parameters"]["properties"]
        actions = props["动作"].get("enum", [])
        return list(actions)
    except Exception:
        return build_available_actions(getattr(game_request.state, "player_items", []))


def _known_intel_text(game_request) -> str:
    lines = []
    for line in (getattr(game_request, "intel_text", "") or "").splitlines():
        line = line.strip()
        if not line or "必须" in line or "工具" in line:
            continue
        for _prefix in ("★ 已知情报：", "☑ 已知情报："):
            if line.startswith(_prefix):
                line = line.removeprefix(_prefix).strip()
                break
        lines.append(line)
    return "；".join(lines) if lines else "当前膛内未知"


def build_buckshot_game_context(ctx, game_request, ai_speaker: str, speaker_role: str) -> str:
    state = game_request.state
    controller = getattr(game_request, "controller_name", "")
    if not controller or controller == "操作者":
        controller = resolve_game_controller(ctx) or ai_speaker
    spectator = resolve_game_spectator(ctx) or ""
    buckshot_state = {
        "game_status": getattr(game_request, "game_status", "进行中"),
        "current_phase": phase_to_cn(getattr(state, "phase", ""), controller),
        "controller_hp": f"{state.health_player}/{state.max_health}",
        "dealer_hp": f"{state.health_opponent}/{state.max_health}",
        "live_count": state.live_remaining,
        "blank_count": state.blank_remaining,
        "known_intel": _known_intel_text(game_request),
        "controller_items": items_to_cn_text(state.player_items),
        "dealer_items": items_to_cn_text(state.dealer_items),
        "last_action_result": getattr(game_request, "last_action_result", ""),
    }
    available_actions = _available_actions_from_tools(game_request) if speaker_role == "操作者" else []
    return build_buckshot_prompt_blocks(
        controller_name=controller,
        spectator_name=spectator,
        speaker_name=ai_speaker,
        speaker_role=speaker_role,
        buckshot_state=buckshot_state,
        available_actions=available_actions,
    )


def build_buckshot_passive_context(ctx, ai_speaker: str) -> tuple[str, str]:
    """无决策但游戏进行中：确定性策略接管 / 庄家回合 / 加载阶段时给模型解说视角的提示词。

    返回 (prompt, speaker_role)。按当前阶段分流：
    - 加载 / 发道具 / 场景切换：只给"准备中"块，不显示弹匣、上一动作、膛内情报（防开局乱说打了什么）
    - 庄家回合 / 自己被铐：给"你只能旁观"块 + 轻量局面（不诱导模型说自己要行动）
    - 玩家回合（确定性策略接管那一手）：完整局面 + 真实膛内情报 + 上一动作 + 规则
    """
    with ctx.slot_lock:
        if ctx.context_slot.get("activity_type") != "buckshot_roulette":
            return "", ""
        snap = dict(ctx.context_slot.get("game_state_snapshot") or {})
    game_status = snap.get("game_status", "")
    if game_status not in ("", "进行中"):
        return "", ""
    phase_text = snap.get("phase", "")
    if not phase_text:
        return "", ""

    # 角色信息一律走 ctx（背后是 director 真值），不读 snap.controller —— 那个字段
    # 已经下线（2026-05-17 重构：去掉 bridge/lumi/snap 三层缓存，全部即时取真值）
    controller = resolve_game_controller(ctx) or ai_speaker
    spectator = resolve_game_spectator(ctx) or ""
    if spectator == controller:
        spectator = ""
    speaker_role = "操作者" if ai_speaker == controller else "旁观者"

    identity = build_common_identity_block(
        controller_name=controller,
        spectator_name=spectator,
        speaker_name=ai_speaker,
        speaker_role=speaker_role,
    )

    # ① 加载 / 发道具 / 场景切换：还没开枪，绝对不要解说子弹
    if phase_text in LOADING_PHASES:
        loading = build_loading_situation_block(
            speaker_role=speaker_role, controller_name=controller
        )
        return "\n\n".join([identity, loading]), speaker_role

    # ② 庄家回合 / 自己被铐：轮不到你，只能旁观
    player_cuffed = bool(snap.get("player_cuffed"))
    if phase_text == "dealer_turn" or player_cuffed:
        dealer = build_dealer_turn_block(
            speaker_role=speaker_role,
            controller_name=controller,
            player_cuffed=player_cuffed,
        )
        return "\n\n".join([identity, dealer]), speaker_role

    # ③ 玩家回合（含 waiting 等中性阶段）：完整局面 + 真实膛内情报
    hp_max = snap.get("hp_max") or 0
    controller_hp = f"{snap.get('hp', '?')}/{hp_max}" if hp_max else f"{snap.get('hp', '?')}"
    dealer_hp = f"{snap.get('hp_opp', '?')}/{hp_max}" if hp_max else f"{snap.get('hp_opp', '?')}"
    buckshot_state = {
        "game_status": "进行中",
        "current_phase": phase_to_cn(phase_text, controller),
        "controller_hp": controller_hp,
        "dealer_hp": dealer_hp,
        "live_count": snap.get("live", 0),
        "blank_count": snap.get("blank", 0),
        # 放大镜 / 手机 / 逆转器看到的真实膛内情报（B 方案：操作者和围观者都给）
        "known_intel": snap.get("known_intel") or "当前膛内未知",
        "controller_items": items_to_cn_text(snap.get("player_items", [])),
        "dealer_items": items_to_cn_text(snap.get("dealer_items", [])),
        "last_action_result": snap.get("last_action_result", ""),
    }

    parts = [
        identity,
        build_state_block(controller_name=controller, buckshot_state=buckshot_state),
    ]
    last_action = build_last_action_block(buckshot_state["last_action_result"])
    if last_action:
        parts.append(last_action)
    parts.append(build_rules_block())
    if speaker_role == "旁观者":
        parts.append(build_spectator_diff_block(controller_name=controller))
    return "\n\n".join(part for part in parts if part), speaker_role


def build_buckshot_result_context(ctx, ai_speaker: str) -> tuple[str, str]:
    with ctx.slot_lock:
        if ctx.context_slot.get("activity_type") != "buckshot_roulette":
            return "", ""
        snap = dict(ctx.context_slot.get("game_state_snapshot") or {})
    game_status = snap.get("game_status", "")
    if game_status not in ("胜利", "失败"):
        return "", ""

    controller = resolve_game_controller(ctx) or ai_speaker
    spectator = resolve_game_spectator(ctx) or ""
    if spectator == controller:
        spectator = ""
    speaker_role = "操作者" if ai_speaker == controller else "旁观者"
    buckshot_state = {
        "game_status": game_status,
        "current_phase": "对局已结束",
        "controller_hp": snap.get("hp", "?"),
        "dealer_hp": snap.get("hp_opp", "?"),
        "live_count": snap.get("live", 0),
        "blank_count": snap.get("blank", 0),
        "known_intel": "对局已结束",
        "controller_items": items_to_cn_text(snap.get("player_items", [])),
        "dealer_items": items_to_cn_text(snap.get("dealer_items", [])),
        "last_action_result": "",
    }
    prompt = build_buckshot_prompt_blocks(
        controller_name=controller,
        spectator_name=spectator,
        speaker_name=ai_speaker,
        speaker_role=speaker_role,
        buckshot_state=buckshot_state,
        available_actions=[],
    )
    return prompt, speaker_role


def log_buckshot_fast_prompt(ctx, origin: str, ai_speaker: str, speaker_role: str, game_prompt: str) -> None:
    controller = resolve_game_controller(ctx) or "-"
    spectator = resolve_game_spectator(ctx) or "-"
    ctx.log_event(
        f"{C_FAST}[快脑·恶魔轮盘提示词] {origin} "
        f"speaker={ai_speaker} role={speaker_role} "
        f"controller={controller} spectator={spectator}\n"
        f"----- 游戏提示词开始 -----\n"
        f"{game_prompt.strip()}\n"
        f"----- 游戏提示词结束 -----{C_RESET}"
    )


def is_cancelled_game_request(game_request) -> bool:
    return bool(game_request and getattr(game_request, "cancelled", False))


def log_game_speaker_routing(ctx, origin: str, game_label: str, game_request, pending_commentary_request, ai_speaker: str) -> None:
    if not game_label:
        return
    ctrl = resolve_game_controller(ctx)
    spectator = resolve_game_spectator(ctx)
    ctx.log_event(
        f"{C_FAST}[双角色调度] {origin} game={game_label} "
        f"controller={ctrl or '-'} spectator={spectator or '-'} "
        f"has_request={bool(game_request)} staged={bool(pending_commentary_request)} "
        f"speaker={ai_speaker}{C_RESET}"
    )


@dataclass
class ConversationContext:
    """对话主循环依赖的运行时状态和句柄集合。

    Step 2 双角色工作时这会扩展为 SpeakerContext（每个角色一个），
    本次重构先建出基础形状，字段在 Task 8/9/10/11 实际迁移函数时按需补充。
    """
    # 可变全局（引用语义传递）
    turn_metrics: dict
    history: list
    context_slot: dict
    slot_lock: Any              # Lock
    speaking_lock: Any          # Lock
    slow_brain_trigger: Any     # Event
    recent_lumi_outputs: Any    # deque

    # 句柄（引用）
    bus: Any                    # EventBus

    # 回调（lazy-bound：bridges 在程序启动后才赋值，需要每次实时读 lumi 模块全局）
    touch_last_message: Callable[[], None]
    get_buckshot_bridge: Callable[[], Any]
    get_buckshot_game_ready: Callable[[], bool]
    get_wordle_bridge: Callable[[], Any]
    get_wordle_game_ready: Callable[[], bool]
    get_handle_bridge: Callable[[], Any]
    get_handle_game_ready: Callable[[], bool]
    get_terraria_bridge: Callable[[], Any]
    get_terraria_game_ready: Callable[[], bool]
    get_kr_bridge: Callable[[], Any]
    get_kr_game_ready: Callable[[], bool]

    # boot-time 常量（运行时不变）
    fast_draw_tool: dict
    proactive_prompt_opening: str
    proactive_prompt_gaming: str
    spectator_prompt_gaming: str
    proactive_prompt_continue: str

    # 运行时可变开关（argparse 在 main() 里改它们；用 lambda 实时读 lumi 模块全局）
    get_enable_drawing: Callable[[], bool]
    get_enable_tts: Callable[[], bool]

    # 函数回调
    log_event: Callable[[str], None]
    parse_emotion: Callable
    strip_stage_directions: Callable
    build_slot_prompt: Callable
    capture_screen: Callable
    log_turn: Callable
    detect_activity_switch: Callable
    extract_draw_subject: Callable
    mark_drawing_started: Callable
    on_draw_complete: Callable
    get_draw_stage_offer: Callable
    execute_fast_brain_tools: Callable
    interrupt_monitor: Callable
    kr_build_anchor_msg: Callable
    terraria_build_anchor_msg: Callable

    # ─── Step 2 双角色字段（带默认值，单角色场景下为占位） ──────────────
    active_speakers: list = field(default_factory=lambda: ["Lumi"])
    fast_brains: dict = field(default_factory=dict)        # {"Lumi": FastBrain, "Nox": FastBrain}
    scheduler: Any = None                                  # SpeakerScheduler 实例
    speaker_configs: dict = field(default_factory=dict)    # {"Lumi": SpeakerConfig, "Nox": SpeakerConfig}
    cable_indices: dict = field(default_factory=dict)      # {"Lumi": cable_idx, "Nox": cable_idx} TTS 输出声卡 index
    memory_runtime: Any = None
    get_session_id: Callable[[], str] = field(default_factory=lambda: lambda: None)
    # 游戏操控权回调（lazy-bound，从 director 实时读当前 game segment 的 controller）
    get_current_game_controller: Callable[[], str] = field(default_factory=lambda: lambda: "")


def _stream_llm_text_only(ctx: ConversationContext,
                          messages, max_tokens=200, temperature=LLM_TEMPERATURE,
                          tools=None, tool_result_holder=None) -> str:
    """纯文本模式：LLM 流式生成，直接打印，无 TTS/VTS/打断"""

    start = time.time()
    full_reply = ""
    first_token = True
    bracket_buf = ""       # 括号缓冲：遇到左括号开始攒，遇到右括号整段丢弃
    in_bracket = False

    # 选中模型不支持工具时，带工具的请求自动回退到支持工具的模型；聊天请求仍用选中模型。
    _client, _model, bp = fast_brain.resolve_call_target(needs_tools=bool(tools))
    llm_kwargs = dict(
        model=_model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=max_tokens,
        temperature=temperature if temperature != LLM_TEMPERATURE else bp["temperature"],
        top_p=bp["top_p"],
        frequency_penalty=bp["frequency_penalty"],
        presence_penalty=bp["presence_penalty"],
    )
    if bp["logit_bias"]:
        llm_kwargs["logit_bias"] = bp["logit_bias"]
    if bp["extra_body"]:
        llm_kwargs["extra_body"] = bp["extra_body"]
    if tools:
        llm_kwargs["tools"] = tools
        llm_kwargs["tool_choice"] = "auto"
    response = _client.chat.completions.create(**llm_kwargs)

    def _print_filtered(text):
        """流式打印，实时过滤括号内容"""
        nonlocal in_bracket, bracket_buf
        for ch in text:
            if ch in '（(':
                in_bracket = True
                bracket_buf = ch
            elif ch in '）)' and in_bracket:
                in_bracket = False
                bracket_buf = ""
            elif in_bracket:
                bracket_buf += ch
            else:
                print(ch, end="", flush=True)

    _fb_usage = None
    _tc_chunks = {}
    for chunk in response:
        # 捕获流式 usage（最后一个 chunk 携带）
        if hasattr(chunk, "usage") and chunk.usage is not None:
            _fb_usage = chunk.usage
        if not chunk.choices:
            continue
        delta_obj = chunk.choices[0].delta
        # 累积 tool_call chunks
        if hasattr(delta_obj, "tool_calls") and delta_obj.tool_calls:
            for tc in delta_obj.tool_calls:
                idx = tc.index if hasattr(tc, "index") else 0
                if idx not in _tc_chunks:
                    _tc_chunks[idx] = {"name": "", "arguments": ""}
                if hasattr(tc.function, "name") and tc.function.name:
                    _tc_chunks[idx]["name"] = tc.function.name
                if hasattr(tc.function, "arguments") and tc.function.arguments:
                    _tc_chunks[idx]["arguments"] += tc.function.arguments
        delta = delta_obj.content
        if delta:
            if first_token:
                ttft = time.time() - start
                first_token = False
                ctx.turn_metrics["fast_brain_ttft_ms"] = round(ttft * 1000, 1)
                if "e2e_start" in ctx.turn_metrics:
                    ctx.turn_metrics["e2e_ms"] = round((time.time() - ctx.turn_metrics["e2e_start"]) * 1000, 1)
            full_reply += delta
            # 文本从第一个字就直接过滤打印。早期"等情感标签 [开心]/攒够 20 字才放行"的门已废弃
            # （表情统一交 emotion_sidecar，快脑不再输出标签），留着会卡死 ≤20 字短回复。
            _print_filtered(delta)

    # 解析 tool_call 结果
    if _tc_chunks and tool_result_holder is not None:
        for idx in sorted(_tc_chunks.keys()):
            tc = _tc_chunks[idx]
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = _rescue_tool_call_json(tc["arguments"])
            tool_result_holder.append({"name": tc["name"], "arguments": args})
            ctx.log_event(f"{C_FAST}[快脑·tool_call] {tc['name']}({args}){C_RESET}")

    # 记录快脑 token 用量
    if _fb_usage:
        ctx.turn_metrics["fast_brain_input_tokens"] = getattr(_fb_usage, "prompt_tokens", 0) or getattr(_fb_usage, "input_tokens", 0)
        ctx.turn_metrics["fast_brain_output_tokens"] = getattr(_fb_usage, "completion_tokens", 0) or getattr(_fb_usage, "output_tokens", 0)
        # 缓存命中验证（豆包 2.0 系列自动隐式缓存）
        _ptd = getattr(_fb_usage, "prompt_tokens_details", None)
        _cached = 0
        if _ptd is not None:
            if isinstance(_ptd, dict):
                _cached = _ptd.get("cached_tokens", 0) or 0
            else:
                _cached = getattr(_ptd, "cached_tokens", 0) or 0
        ctx.turn_metrics["fast_brain_cached_tokens"] = _cached

    # 括号未闭合时（模型输出被截断），把缓冲内容打印出来
    if in_bracket and bracket_buf:
        print(bracket_buf, end="", flush=True)

    total = time.time() - start
    ctx.turn_metrics["fast_brain_total_ms"] = round(total * 1000, 1)
    ctx.turn_metrics["reply_text_done_at"] = time.time()

    lumi_tts.tts_state.last_speak_done_time = time.time()
    return full_reply


def chat_and_speak(
    ctx: ConversationContext,
    user_input: str,
    vad_model=None,
    cable_index=None,
    speaker="未知",
    input_type="text",
    memory_text=None,
    viewer_identity_key="",
    memory_aside="",
    batch_items=None,
):
    """用户说话 → 快脑回复（含双脑状态 + RAG + VTS + 打断）

    speaker 参数是"当前发言用户"的标签（"Mio"、"未知"、弹幕名），不是 AI 角色名。
    AI 角色由 ctx.scheduler.pick_speaker(user_input) 决定。
    """
    ctx.touch_last_message()
    ctx.slow_brain_trigger.set()
    now = time.time()
    ctx.turn_metrics.setdefault("mode", "pipeline")
    ctx.turn_metrics.setdefault("source", input_type)
    ctx.turn_metrics.setdefault("input_ready_at", ctx.turn_metrics.get("e2e_start", now))
    memory_text = user_input if memory_text is None else memory_text

    # 在函数入口缓存当前 bridges 实例（lazy-bound 全局，可能在程序启动后才赋值）
    _buckshot_bridge = ctx.get_buckshot_bridge()
    _wordle_bridge = ctx.get_wordle_bridge()
    _handle_bridge = ctx.get_handle_bridge()
    _terraria_bridge = ctx.get_terraria_bridge()
    _kr_bridge = ctx.get_kr_bridge()
    _terraria_game_ready = ctx.get_terraria_game_ready()
    _kr_game_ready = ctx.get_kr_game_ready()

    # peek 是否有 pending 游戏决策（双角色场景下决定是否按 controller override 调度）
    _peek_pending_game = None
    for _b in (_buckshot_bridge, _wordle_bridge, _handle_bridge):
        if _b:
            _r = _b.get_pending_decision()
            if _r:
                _peek_pending_game = _r
                break

    # 调度器决定本轮哪个 AI 角色回应（@ 名字优先 / 否则 next_speaker）
    ai_speaker = ctx.scheduler.pick_speaker(user_input)
    fb = ctx.fast_brains[ai_speaker]

    # 画画关键词检测：合并批次里每条原始弹幕单独判断+排队，避免多条画画请求被压成一个主题
    # （"画爱心""画苹果""画栗子" 合并后曾只画了第一个爱心）。用合并函数保留的 batch_items
    # 原始单条，而不是去拆合并文本——不依赖"弹幕不含换行"的隐含假设。
    _draw_results = []  # [(subject, result), ...]
    if ctx.get_enable_drawing():
        import re as _re
        import lumi_draw
        # 合并批次 → 逐条原始弹幕的内容；单条/语音 → 就它本身
        if batch_items:
            _draw_inputs = [(it.get("text") or it.get("display_text") or "") for it in batch_items]
        else:
            _draw_inputs = [user_input]
        _seen_subjects = set()
        for _one in _draw_inputs:
            _one = (_one or "").strip()
            if not _one:
                continue
            # 粗筛：这条出现「画」或 draw 才送小模型判断主题
            if not (("画" in _one) or _re.search(r'draw\b', _one, _re.IGNORECASE)):
                continue
            _subj = ctx.extract_draw_subject(_one)
            if not _subj or _subj in _seen_subjects:
                continue
            _seen_subjects.add(_subj)
            _res = lumi_draw.request_draw(_subj, ctx.on_draw_complete)
            _draw_results.append((_subj, _res))
            if _res in ("drawing", "queued"):
                ctx.mark_drawing_started(_subj)
            elif _res == "full":
                break  # 队列已满，后面的也排不上，不必再调模型

    # 后台检测活动切换意图（不阻塞主流程，下一轮生效）
    with ctx.slot_lock:
        current_activity = ctx.context_slot["activity_type"]
    threading.Thread(
        target=ctx.detect_activity_switch,
        args=(user_input, current_activity),
        daemon=True,
    ).start()

    # 注入说话人标签到历史消息
    msg_content = user_input
    # 画画请求结果的本轮话术（让主播的口头回应和系统排队一致，别空头承诺）
    if _draw_results:
        _accepted = [s for s, r in _draw_results if r in ("drawing", "queued")]
        _full = [s for s, r in _draw_results if r == "full"]
        _note = ""
        if _accepted:
            _note += "你接下了观众点的画：" + "、".join(f"「{s}」" for s in _accepted) + "，会按顺序一幅幅画。"
        if _full:
            _note += "另外 " + "、".join(f"「{s}」" for s in _full) + " 没排上（排队满了），让他们等下一轮再点。"
        if _note:
            msg_content += f"\n[系统：{_note} 回复时自然知道这事，别说自己不会画、也别说已经画好了。]"
    current_user_content = f"[{normalize_viewer_name(speaker)}说] {msg_content}" if speaker != "未知" else msg_content
    # 用户输入广播到所有 active speakers 的 history（双角色场景下两人都看到这一轮用户说什么）
    for _s in ctx.active_speakers:
        ctx.fast_brains[_s].append_user(current_user_content)
    if ctx.memory_runtime and viewer_identity_key and speaker and speaker != "未知":
        try:
            ctx.memory_runtime.on_viewer_message(
                identity_key=viewer_identity_key,
                display_name=speaker,
                source_type=input_type,
                text=memory_text,
                session_id=ctx.get_session_id(),
            )
        except Exception as e:
            ctx.log_event(f"{C_ERR}[记忆·观众写入失败] {e}{C_RESET}")

    system_content = fb.system_prompt + ctx.build_slot_prompt()

    # 快脑实时检索相关素材（暂时关闭，知识库内容不足，待完善后重新启用）
    materials = []
    # materials = memory_store.search(user_input, top_k=3)
    # if materials:
    #     rag_text = "\n".join([f"- {m['text']}" for m in materials])
    #     system_content += f"\n## 相关知识（如与用户问题相关可自然融入回答）\n{rag_text}\n"


    # 检查是否有待处理的游戏决策（恶魔轮盘 or Wordle）
    game_request = None
    game_tools = None
    game_prompt = None
    game_label = ""
    if _buckshot_bridge:
        game_request = _buckshot_bridge.get_pending_decision()
        if is_cancelled_game_request(game_request):
            game_request = None
        if game_request:
            from buckshot_bridge import BUCKSHOT_GAME_PROMPT
            game_prompt = BUCKSHOT_GAME_PROMPT
            game_label = "恶魔轮盘"
    if not game_request and _wordle_bridge:
        game_request = _wordle_bridge.get_pending_decision()
        if is_cancelled_game_request(game_request):
            game_request = None
        if game_request:
            from wordle_bridge import WORDLE_GAME_PROMPT
            game_prompt = WORDLE_GAME_PROMPT
            game_label = "Wordle"
    if not game_request and _handle_bridge:
        game_request = _handle_bridge.get_pending_decision()
        if is_cancelled_game_request(game_request):
            game_request = None
        if game_request:
            from handle_bridge import HANDLE_GAME_PROMPT
            game_prompt = HANDLE_GAME_PROMPT
            game_label = "汉兜"

    pending_commentary_request = None
    if should_stage_game_commentary(ctx, game_request):
        pending_commentary_request = game_request
        _spectator = resolve_game_spectator(ctx)
        if _spectator:
            ai_speaker = _spectator
        game_request = None
    elif len(ctx.active_speakers) > 1 and game_request:
        _ctrl = resolve_game_controller(ctx)
        if _ctrl:
            ai_speaker = _ctrl

    log_game_speaker_routing(ctx, "chat_and_speak", game_label, game_request, pending_commentary_request, ai_speaker)

    fb = ctx.fast_brains[ai_speaker]
    system_content = fb.system_prompt + ctx.build_slot_prompt()
    if ctx.memory_runtime:
        try:
            # memory_aside 已按每条 uid 注入观众事实/摘要（弹幕路径），这里就只查 agent 自我记忆，
            # 避免观众事实双重注入；没有 memory_aside（语音/新观众无事实）时正常查观众（含原始发言兜底）。
            memory_prompt = ctx.memory_runtime.build_fast_brain_memory_prompt(
                identity_key="" if (speaker == "未知" or memory_aside) else viewer_identity_key,
                agent_name=ai_speaker,
                current_input=memory_text,
            )
            if memory_prompt:
                system_content += "\n\n" + memory_prompt
                # 可见性：把这次给快脑注入了哪些观众记忆打出来（否则查询是静默的，
                # 看起来像"没触发"）。事实/摘要是会话结束批量提取的；都没有时回退原始发言。
                if speaker and speaker != "未知":
                    _hits = []
                    if "这个观众的已知信息" in memory_prompt:
                        _hits.append("事实")
                    if "你和这个观众之前聊过" in memory_prompt:
                        _hits.append("历史摘要")
                    if "最近原始发言摘录" in memory_prompt:
                        _hits.append("原始发言(无提取事实时的回退)")
                    if _hits:
                        ctx.log_event(
                            f"{C_FAST}[记忆·注入] 用户 {speaker}: {' / '.join(_hits)}{C_RESET}"
                        )
        except Exception as e:
            ctx.log_event(f"{C_ERR}[记忆·提示词注入失败] {e}{C_RESET}")
    # 合并批次弹幕的观众记忆旁注（调用方按每条 uid 收集好传入）；单条弹幕已由上面的
    # build_fast_brain_memory_prompt 按真实 identity 注入，不重复。
    if memory_aside:
        system_content += "\n\n" + memory_aside
    system_content = append_game_role_addon(ctx, system_content, ai_speaker)
    # 泰拉瑞亚：注入游戏提示词 + 可选工具（不走 game_request 模式）
    _terraria_tools_injected = False
    if not game_request and _terraria_bridge and _terraria_bridge.running and _terraria_game_ready:
        from terraria_bridge import (
            TERRARIA_GAME_PROMPT_CONTROLLER,
            TERRARIA_GAME_PROMPT_SPECTATOR,
            TERRARIA_GOAL_TOOL,
        )
        if is_game_controller(ctx, ai_speaker):
            system_content += "\n" + TERRARIA_GAME_PROMPT_CONTROLLER
            game_tools = [TERRARIA_GOAL_TOOL]
            _terraria_tools_injected = True
        else:
            system_content += "\n" + TERRARIA_GAME_PROMPT_SPECTATOR
    # Kingdom Rush：纯解说，不需要工具
    if not game_request and _kr_bridge and _kr_bridge.running and _kr_game_ready:
        from kingdom_rush_bridge import KR_GAME_PROMPT_CONTROLLER, KR_GAME_PROMPT_SPECTATOR
        if is_game_controller(ctx, ai_speaker):
            system_content += "\n" + KR_GAME_PROMPT_CONTROLLER
        else:
            system_content += "\n" + KR_GAME_PROMPT_SPECTATOR
    _draw_stage_offer = None
    if not game_request and not _terraria_tools_injected:
        _draw_stage_offer = ctx.get_draw_stage_offer()
        if _draw_stage_offer:
            system_content += "\n" + _draw_stage_offer["prompt"]
            game_tools = [ctx.fast_draw_tool]
            ctx.log_event(f"{C_FAST}[快脑·注入画画] chat_and_speak 画画环节工具注入{C_RESET}")

    # 游戏环节标志：用于字数硬截、紧化输出策略等。命中任一分支即为游戏环节。
    _in_game_segment = False
    if pending_commentary_request:
        _in_game_segment = True
        _ctrl = resolve_game_controller(ctx)
        if is_buckshot_game_request(game_label, pending_commentary_request):
            _buckshot_context = build_buckshot_game_context(
                ctx, pending_commentary_request, ai_speaker, "旁观者"
            )
            log_buckshot_fast_prompt(ctx, "chat_and_speak", ai_speaker, "旁观者", _buckshot_context)
            _game_context = "\n\n" + _buckshot_context
        else:
            _game_context = build_spectator_game_context(game_label, pending_commentary_request)
            system_content += "\n" + ctx.spectator_prompt_gaming
            system_content += (
                f"\n## {game_label} 当前分工\n"
                f"当前轮到 {_ctrl} 操作，你不是操作者。"
            )
        user_input_for_llm = current_user_content + _game_context
        ctx.log_event(f"{C_FAST}[快脑·游戏解说] chat_and_speak 先给旁观者一轮，再进入决策{C_RESET}")
    elif game_request:
        _in_game_segment = True
        game_tools = game_request.tools
        if is_buckshot_game_request(game_label, game_request):
            _buckshot_context = build_buckshot_game_context(
                ctx, game_request, ai_speaker, "操作者"
            )
            log_buckshot_fast_prompt(ctx, "chat_and_speak", ai_speaker, "操作者", _buckshot_context)
            game_context = "\n\n" + _buckshot_context
        else:
            system_content += "\n" + game_prompt
            game_context = f"\n\n{game_request.state_text}"
            if game_request.intel_text:
                game_context += f"\n{game_request.intel_text}"
        user_input_for_llm = current_user_content + game_context
        ctx.log_event(f"{C_FAST}[快脑·注入游戏] chat_and_speak 携带决策请求, tools={len(game_tools)}个{C_RESET}")
    else:
        _buckshot_result_context, _buckshot_result_role = build_buckshot_result_context(ctx, ai_speaker)
        if _buckshot_result_context:
            _in_game_segment = True
            log_buckshot_fast_prompt(
                ctx, "chat_and_speak", ai_speaker, _buckshot_result_role, _buckshot_result_context
            )
            user_input_for_llm = current_user_content + "\n\n" + _buckshot_result_context
        else:
            _buckshot_passive_context, _buckshot_passive_role = build_buckshot_passive_context(ctx, ai_speaker)
            if _buckshot_passive_context:
                _in_game_segment = True
                log_buckshot_fast_prompt(
                    ctx, "chat_and_speak", ai_speaker, _buckshot_passive_role, _buckshot_passive_context
                )
                user_input_for_llm = current_user_content + "\n\n" + _buckshot_passive_context
            else:
                user_input_for_llm = current_user_content
        # 泰拉环节：把当前游戏状态作为锚点喂给 LLM，避免模型凭空编造死活/装备/位置
        if _terraria_bridge and _terraria_bridge.running and _terraria_game_ready:
            _in_game_segment = True
            _terraria_anchor = ctx.terraria_build_anchor_msg()
            if _terraria_anchor:
                user_input_for_llm += "\n\n" + _terraria_anchor
        if _kr_bridge and _kr_bridge.running and _kr_game_ready:
            _in_game_segment = True

    # 双角色游戏环节末尾追加身份硬提醒（last instruction 压制人设代入）
    user_input_for_llm += build_role_reminder_suffix(ctx, ai_speaker)

    # 联网搜索增强（仅闲聊轮、开关开启、闸门命中实时事实/明确要查时）：
    # 调火山联网搜索拿结果摘要，作为参考块临时拼进 system_content（不进 history、不进正文），
    # 快脑照常流式回答。搜索失败一律静默降级为无搜索回答，绝不拖垮发声。
    if (not _in_game_segment and web_search.is_enabled()
            and web_search.needs_search(memory_text)):
        try:
            _results = web_search.search_web(memory_text, count=5, log_fn=ctx.log_event)
            if _results:
                system_content += "\n\n" + web_search.build_search_context(_results)
                ctx.log_event(
                    f"{C_FAST}[联网搜索] 命中 {len(_results)} 条，已注入参考"
                    f"（query={memory_text[:40]}）{C_RESET}"
                )
            else:
                ctx.log_event(f"{C_FAST}[联网搜索] 无结果，按无搜索回答（query={memory_text[:40]}）{C_RESET}")
        except Exception as e:
            ctx.log_event(f"{C_ERR}[联网搜索] 异常降级：{e}{C_RESET}")

    # 构建 messages：历史消息纯文本，当前轮带截图
    # 不再打印 "说话人: " 前缀 + 流式正文（旧逻辑行缓冲延迟、且和按句 [X·说] 日志重复）；
    # 本轮发言统一由 lumi_tts._flush_tts 的按句 [X·说] 日志输出。
    messages = [{"role": "system", "content": system_content}] + fb.history[:-1]
    screen_b64 = ctx.capture_screen()
    if screen_b64:
        messages.append({
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screen_b64}"}},
                {"type": "text", "text": user_input_for_llm},
            ],
        })
    else:
        messages.append({"role": "user", "content": user_input_for_llm})
    tool_results = []
    _game_decision_fired = [False]

    def _fire_game_decision(results):
        if game_request and results and not _game_decision_fired[0]:
            if is_cancelled_game_request(game_request):
                _game_decision_fired[0] = True
                return
            _game_decision_fired[0] = True
            tc = results[0]
            decision = {"action": tc["name"], **tc["arguments"]}
            game_request.result = decision
            game_request.result_event.set()
            ctx.log_event(f"{C_FAST}[快脑·游戏决策·提前触发] {decision}{C_RESET}")

    _chat_max_tokens = 80 if pending_commentary_request else 150   # 回弹幕：压短到 ~2-3 句（之前 200 偏长）
    # 游戏环节给 TTS/字幕加字数硬截。max_tokens 限服务端 token 上限，max_chars 限客户端
    # 累积有效中文字数；模型一旦输出超过阈值就 break 流，避免"要么…要么…不对…"反复纠结的
    # 超长复读跑出去（2026-05-17 长直播观察到单句 200+ 字）。
    # 聊天回复也设字数硬截（之前只游戏设、聊天为 None，导致退化复读无人拦）。160 容得下
    # 1~3 句正常回复，又能兜住跑飞的超长复读；配合 speak() 里的复读检测双保险。
    _chat_max_chars: int | None = 80 if _in_game_segment else 160
    _ai_cfg = ctx.speaker_configs[ai_speaker]
    _ai_voice_name = _ai_cfg.voice_name
    _ai_subtitle_meta = {
        "speaker": ai_speaker,
        "color": _ai_cfg.subtitle_color,
        "label": _ai_cfg.subtitle_label,
    }
    # TTS 输出虚拟声卡按 ai_speaker 路由（双角色场景每人独立声卡，VTS 嘴型不互相串）
    _ai_cable_index = ctx.cable_indices.get(ai_speaker, cable_index)
    _speech_source = ctx.turn_metrics.get("source", input_type)
    _speech_output = speech_output_arbiter.arbiter.request_start(
        speaker=ai_speaker,
        source=_speech_source,
        policy=_speech_policy_for_turn(
            _speech_source,
            game_request=game_request,
            pending_commentary_request=pending_commentary_request,
        ),
        reason=f"{_speech_source}_started",
    )
    if _speech_output is None:
        ctx.log_event(f"{C_FAST}[表现调度] {ai_speaker} {_speech_source} 被跳过：当前已有输出{C_RESET}")
        return ""

    # 把本次解说的 output_id 写进决策请求，让游戏 bridge 精确等"这手操作者这次解说"播完再
    # 推进下一手（在 speak 之前写，覆盖提前触发/兜底两条 fire 路径，不依赖闭包、不漏）。
    if game_request is not None:
        game_request.output_id = _speech_output.output_id

    with ctx.speaking_lock:
        if ctx.get_enable_tts():
            full_reply = lumi_tts.speak(
                messages, vad_model=vad_model, cable_index=_ai_cable_index,
                max_tokens=_chat_max_tokens,
                tools=game_tools, tool_result_holder=tool_results,
                on_tool_calls_parsed=_fire_game_decision if game_request else None,
                interrupt_monitor_fn=ctx.interrupt_monitor,
                turn_metrics=ctx.turn_metrics,
                voice_name=_ai_voice_name,
                subtitle_meta=_ai_subtitle_meta,
                output_id=_speech_output.output_id,
                is_output_current_fn=speech_output_arbiter.arbiter.is_current,
                max_chars=_chat_max_chars,
            )
            if speech_output_arbiter.arbiter.is_current(_speech_output.output_id):
                ctx.bus.publish("tts_done", {
                    "speaker": ai_speaker,
                    "output_id": _speech_output.output_id,
                }, source="execution")
                # reply 事件已移到 lumi_tts.speak() 内部（LLM 文本就绪即发，早于 TTS 播完），
                # 让 emotion_sidecar 在播音时就切表情。这里不再重复发。
        else:
            full_reply = _stream_llm_text_only(
                ctx, messages, max_tokens=_chat_max_tokens,
                tools=game_tools, tool_result_holder=tool_results,
            )
    _speech_valid = speech_output_arbiter.arbiter.is_current(_speech_output.output_id)
    speech_output_arbiter.arbiter.mark_done(_speech_output.output_id)
    if not _speech_valid:
        ctx.log_event(f"{C_FAST}[表现调度] {ai_speaker} {_speech_source} 已被新输出取消，跳过副作用{C_RESET}")
        return ""

    # 兜底：如果回调没触发（如text_only模式），在这里处理
    if game_request and tool_results and not _game_decision_fired[0]:
        if is_cancelled_game_request(game_request):
            _game_decision_fired[0] = True
        else:
            tc = tool_results[0]
            decision = {"action": tc["name"], **tc["arguments"]}
            game_request.result = decision
            game_request.result_event.set()
            ctx.log_event(f"{C_FAST}[快脑·游戏决策] {decision}{C_RESET}")

    # 快脑没调工具 → 标记已尝试，防止同一轮反复重试
    if game_request and not tool_results and not _game_decision_fired[0]:
        if is_cancelled_game_request(game_request):
            _game_decision_fired[0] = True
        else:
            ctx.log_event(f"{C_ERR}[快脑·游戏决策] chat_and_speak 未返回工具调用，标记已尝试{C_RESET}")
            game_request._proactive_attempted = True

    # 泰拉瑞亚快脑工具调用 → 路由到 bridge.set_goal()
    if _terraria_tools_injected and tool_results:
        for tc in tool_results:
            if tc["name"] == "set_terraria_goal" and _terraria_bridge and _terraria_bridge.running:
                # 联机模式下不接受目标，纯跟随
                if _terraria_bridge.multiplayer_mode:
                    ctx.log_event(f"{C_FAST}[快脑·泰拉瑞亚] 联机模式，忽略目标指令{C_RESET}")
                    continue
                args = tc["arguments"]
                _terraria_bridge.set_goal(
                    args["goal_type"], args["target"], args.get("reason", ""),
                    params={"direction": args.get("direction"), "quantity": args.get("quantity")}
                )
                ctx.log_event(f"{C_FAST}[快脑·泰拉瑞亚] 临时指令: {args['goal_type']} → {args['target']}{C_RESET}")
    if not game_request:
        ctx.execute_fast_brain_tools(tool_results, "chat_and_speak")

    if pending_commentary_request:
        setattr(ctx, "_last_game_spectator_commentary_at", time.time())
        mark_game_commentary_done(pending_commentary_request)

    if full_reply:
        emotion_tag, text_reply = ctx.parse_emotion(full_reply)
        text_reply = ctx.strip_stage_directions(text_reply)
        ctx.recent_lumi_outputs.append(text_reply)
        fb.append_assistant(text_reply)
        fb.trim_history(20)   # 40→20：缩短历史窗口，让旧梗（如某场咬死的"曲奇砖"）尽快滚出视野
        if ctx.memory_runtime:
            try:
                ctx.memory_runtime.on_agent_reply(
                    agent_name=ai_speaker,
                    text=text_reply,
                    session_id=ctx.get_session_id(),
                    user_input=memory_text,
                )
            except Exception as e:
                ctx.log_event(f"{C_ERR}[记忆·Agent写入失败] {e}{C_RESET}")
        # 跨角色镜像：把 ai_speaker 这条发言以 [X说] 形式 append 到其他角色 history
        mirror_speech_to_partner(ctx.active_speakers, ctx.fast_brains, ai_speaker, text_reply)
        # 翻转调度器游标到下一个角色
        ctx.scheduler.advance()
        with ctx.slot_lock:
            slot_snap = dict(ctx.context_slot)
        ctx.log_turn(
            user=user_input, lumi=text_reply, emotion=emotion_tag,
            rag_hits=materials, slot_snapshot=slot_snap, turn_type="user",
        )

        # 异步预热：让豆包对"系统提示 + 已发生历史"这个前缀提前建立隐式缓存
        try:
            _warmup_history_snap = list(fb.history)
            _warmup_system = fb.system_prompt + ctx.build_slot_prompt()
            _warmup_msgs = [{"role": "system", "content": _warmup_system}]
            _warmup_msgs.extend(_warmup_history_snap)
            _warmup_msgs.append({"role": "user", "content": "."})
            threading.Thread(
                target=_send_warmup_request,
                args=(ctx, _warmup_msgs),
                daemon=True,
            ).start()
        except Exception as e:
            ctx.log_event(f"{C_ERR}[快脑·预热触发失败] {e}{C_RESET}")

    return full_reply


def proactive_speak(ctx: ConversationContext, vad_model=None, cable_index=None, director_msg: str = None):
    """Lumi/Nox 主动说话（用户沉默时触发）或处理待决游戏请求。

    AI 角色由 ctx.scheduler.pick_speaker(None) 决定（next_speaker 轮换）；
    单角色场景下永远是同一个角色，双角色场景下两个角色交替主动说话。
    """
    now = time.time()
    ctx.turn_metrics.setdefault("mode", "pipeline")
    ctx.turn_metrics.setdefault("source", "proactive")
    ctx.turn_metrics.setdefault("input_ready_at", ctx.turn_metrics.get("e2e_start", now))
    # 在函数入口缓存当前 bridges 实例（lazy-bound 全局）
    _buckshot_bridge = ctx.get_buckshot_bridge()
    _buckshot_game_ready = ctx.get_buckshot_game_ready()
    _wordle_bridge = ctx.get_wordle_bridge()
    _wordle_game_ready = ctx.get_wordle_game_ready()
    _handle_bridge = ctx.get_handle_bridge()
    _handle_game_ready = ctx.get_handle_game_ready()
    _terraria_bridge = ctx.get_terraria_bridge()
    _terraria_game_ready = ctx.get_terraria_game_ready()
    _kr_bridge = ctx.get_kr_bridge()
    _kr_game_ready = ctx.get_kr_game_ready()

    # peek 是否有 pending 游戏决策（双角色场景下决定是否按 controller override 调度）
    _peek_pending_game = None
    for _b in (_buckshot_bridge, _wordle_bridge, _handle_bridge):
        if _b:
            _r = _b.get_pending_decision()
            if _r:
                _peek_pending_game = _r
                break

    # 调度器空转模式下选 next_speaker
    ai_speaker = ctx.scheduler.pick_speaker(None)
    fb = ctx.fast_brains[ai_speaker]

    # 根据当前活动选择主动说话指令
    # game_ready 为 False 时（加载阶段）不用 GAMING 解说指令，避免幻觉
    is_buckshot = _buckshot_bridge and _buckshot_bridge.running and _buckshot_game_ready if _buckshot_bridge else False
    is_wordle = _wordle_bridge and _wordle_bridge.running and _wordle_game_ready if _wordle_bridge else False
    is_handle = _handle_bridge and _handle_bridge.running and _handle_game_ready if _handle_bridge else False
    is_terraria = _terraria_bridge and _terraria_bridge.running and _terraria_game_ready
    is_kr = _kr_bridge and _kr_bridge.running and _kr_game_ready
    is_gaming = is_buckshot or is_wordle or is_handle or is_terraria or is_kr
    if len(fb.history) == 0:
        proactive_instruction = ctx.proactive_prompt_opening
    elif is_gaming:
        proactive_instruction = ctx.proactive_prompt_gaming
    else:
        proactive_instruction = ctx.proactive_prompt_continue

    system_content = fb.system_prompt + ctx.build_slot_prompt() + proactive_instruction

    # 检查是否有待处理的游戏决策（恶魔轮盘 or Wordle）
    game_request = None
    game_tools = None
    game_prompt = None
    game_label = ""
    silence_msg = ""
    if _buckshot_bridge:
        game_request = _buckshot_bridge.get_pending_decision()
        if is_cancelled_game_request(game_request):
            game_request = None
        if game_request:
            from buckshot_bridge import BUCKSHOT_GAME_PROMPT
            game_prompt = BUCKSHOT_GAME_PROMPT
            game_label = "恶魔轮盘"
    if not game_request and _wordle_bridge:
        game_request = _wordle_bridge.get_pending_decision()
        if is_cancelled_game_request(game_request):
            game_request = None
        if game_request:
            from wordle_bridge import WORDLE_GAME_PROMPT
            game_prompt = WORDLE_GAME_PROMPT
            game_label = "Wordle"
    if not game_request and _handle_bridge:
        game_request = _handle_bridge.get_pending_decision()
        if is_cancelled_game_request(game_request):
            game_request = None
        if game_request:
            from handle_bridge import HANDLE_GAME_PROMPT
            game_prompt = HANDLE_GAME_PROMPT
            game_label = "汉兜"

    pending_commentary_request = None
    if should_stage_game_commentary(ctx, game_request):
        pending_commentary_request = game_request
        _spectator = resolve_game_spectator(ctx)
        if _spectator:
            ai_speaker = _spectator
        game_request = None
    elif len(ctx.active_speakers) > 1 and game_request:
        _ctrl = resolve_game_controller(ctx)
        if _ctrl:
            ai_speaker = _ctrl

    log_game_speaker_routing(ctx, "proactive_speak", game_label, game_request, pending_commentary_request, ai_speaker)

    fb = ctx.fast_brains[ai_speaker]
    if director_msg:
        # 定向轮次：导演现场指示（环节切换/下播等）。换掉"主动找话题继续聊"的框架，
        # 让角色模型把它当"立刻照做"的指令，而不是又起一段闲聊（角色模型对埋藏的弱
        # 元指令不敏感，必须强位置 + 明确框架）。指令正文走 silence_msg 放到最后一条。
        # 可观测性：这条只有走新"定向轮次"路径才会打印；ai_speaker 是调度器 pick_speaker
        # 轮换选出来的（多次下播/切环节能看出 Lumi/Nox 轮着接，而非永远 Lumi）。
        ctx.log_event(
            f"{C_FAST}[导演·定向轮次] 调度器轮换选中 {ai_speaker} 接导演指令"
            f"（强位置投递、非闲聊框架）：{director_msg[:30]}{C_RESET}"
        )
        proactive_instruction = (
            "\n现在有一条导演的现场指示（见最后一条消息），请立刻照做："
            "用你自己的人设口吻自然地说出来，不要继续之前的话题、也不要另起新话题。\n"
        )
    elif len(fb.history) == 0:
        proactive_instruction = ctx.proactive_prompt_opening
    elif pending_commentary_request:
        proactive_instruction = ctx.spectator_prompt_gaming
    elif is_gaming:
        proactive_instruction = ctx.proactive_prompt_gaming
    else:
        proactive_instruction = ctx.proactive_prompt_continue
    system_content = fb.system_prompt + ctx.build_slot_prompt() + proactive_instruction
    if ctx.memory_runtime:
        try:
            # proactive（主动说话）传空 current_input → 只注入自我事实、不注入历史摘要。
            # 不能把"最近对话/自己刚说的话"当上下文喂回来——否则会形成反馈回环：她说了某个
            # 旧梗(如五花肉)→进最近对话→下轮又匹配到那条摘要→又注入→又说，咬着一个梗不放
            # （实测一场说了 128 次五花肉）。历史摘要的召回只发生在观众弹幕真的聊到时
            # （chat_and_speak 用观众输入当 current_input，是观众驱动、无回环）。
            memory_prompt = ctx.memory_runtime.build_fast_brain_memory_prompt(
                identity_key="",
                agent_name=ai_speaker,
                current_input="",
            )
            if memory_prompt:
                system_content += "\n\n" + memory_prompt
        except Exception as e:
            ctx.log_event(f"{C_ERR}[记忆·主动提示词注入失败] {e}{C_RESET}")
    system_content = append_game_role_addon(ctx, system_content, ai_speaker)

    # 泰拉瑞亚：注入游戏提示词（proactive_speak 不强制注入工具，只解说）
    _terraria_tools_injected_p = False
    if not game_request and is_terraria:
        from terraria_bridge import (
            TERRARIA_GAME_PROMPT_CONTROLLER,
            TERRARIA_GAME_PROMPT_SPECTATOR,
            TERRARIA_GOAL_TOOL,
        )
        if is_game_controller(ctx, ai_speaker):
            system_content += "\n" + TERRARIA_GAME_PROMPT_CONTROLLER
            # proactive_speak 也注入工具，以防操控者主动想切换目标
            game_tools = [TERRARIA_GOAL_TOOL]
            _terraria_tools_injected_p = True
        else:
            system_content += "\n" + TERRARIA_GAME_PROMPT_SPECTATOR
    # Kingdom Rush：纯解说，不需要工具
    if not game_request and is_kr:
        from kingdom_rush_bridge import KR_GAME_PROMPT_CONTROLLER, KR_GAME_PROMPT_SPECTATOR
        if is_game_controller(ctx, ai_speaker):
            system_content += "\n" + KR_GAME_PROMPT_CONTROLLER
        else:
            system_content += "\n" + KR_GAME_PROMPT_SPECTATOR
    _draw_stage_offer = None
    if not game_request and not _terraria_tools_injected_p:
        _draw_stage_offer = ctx.get_draw_stage_offer()
        if _draw_stage_offer:
            system_content += "\n" + _draw_stage_offer["prompt"]
            game_tools = [ctx.fast_draw_tool]
            silence_msg = _draw_stage_offer["silence_msg"]
            ctx.log_event(f"{C_FAST}[快脑·注入画画] proactive_speak 画画环节工具注入{C_RESET}")

    if pending_commentary_request:
        _ctrl = resolve_game_controller(ctx)
        if is_buckshot_game_request(game_label, pending_commentary_request):
            game_context = build_buckshot_game_context(
                ctx, pending_commentary_request, ai_speaker, "旁观者"
            )
            log_buckshot_fast_prompt(ctx, "proactive_speak", ai_speaker, "旁观者", game_context)
        else:
            game_context = build_spectator_game_context(game_label, pending_commentary_request)
            system_content += (
                f"\n## {game_label} 当前分工\n"
                f"当前轮到 {_ctrl} 操作，你不是操作者。"
            )
        silence_msg = f"[{game_label}：先由旁观者接一句]\n{game_context}"
        ctx.log_event(f"{C_FAST}[快脑·游戏解说] proactive_speak 先给旁观者一轮，再进入决策{C_RESET}")
    elif game_request:
        game_tools = game_request.tools
        if is_buckshot_game_request(game_label, game_request):
            game_context = build_buckshot_game_context(
                ctx, game_request, ai_speaker, "操作者"
            )
            log_buckshot_fast_prompt(ctx, "proactive_speak", ai_speaker, "操作者", game_context)
        else:
            system_content += "\n" + game_prompt
            game_context = game_request.state_text
            if game_request.intel_text:
                game_context += f"\n{game_request.intel_text}"
        silence_msg = f"[{game_label}：轮到你决策]\n{game_context}"
        ctx.log_event(f"{C_FAST}[快脑·注入游戏] proactive_speak 携带决策请求, tools={len(game_tools)}个{C_RESET}")
    elif is_gaming:
        _buckshot_result_context, _buckshot_result_role = build_buckshot_result_context(ctx, ai_speaker)
        if _buckshot_result_context:
            log_buckshot_fast_prompt(
                ctx, "proactive_speak", ai_speaker, _buckshot_result_role, _buckshot_result_context
            )
            silence_msg = f"[恶魔轮盘：对局结果]\n{_buckshot_result_context}"
        else:
            _buckshot_passive_context, _buckshot_passive_role = build_buckshot_passive_context(ctx, ai_speaker)
            if _buckshot_passive_context:
                log_buckshot_fast_prompt(
                    ctx, "proactive_speak", ai_speaker, _buckshot_passive_role, _buckshot_passive_context
                )
                silence_msg = f"[恶魔轮盘：解说视角]\n{_buckshot_passive_context}"
        # KR 纯解说模式：隔几轮用 user 消息把注意力拉回游戏局面
        if is_kr and not silence_msg:
            silence_msg = ctx.kr_build_anchor_msg()
        # 泰拉同理：bridge 自动操控，AI 只解说，每次都喂当前局面避免幻觉
        if is_terraria and not silence_msg:
            silence_msg = ctx.terraria_build_anchor_msg()
        ctx.log_event(f"{C_FAST}[快脑·无待决] 游戏进行中但无需决策，解说模式{C_RESET}")

    # 双角色游戏环节末尾追加身份硬提醒（last instruction 压制人设代入）
    # 定向轮次：导演指示正文作为本轮驱动消息（最后一条 user 消息，最强位置）。
    if director_msg:
        silence_msg = director_msg
    silence_msg = (silence_msg or "") + build_role_reminder_suffix(ctx, ai_speaker)

    _proactive_entry_at = time.time()
    _game_tag = ("terraria" if is_terraria else "buckshot" if is_buckshot
                 else "kr" if is_kr else "wordle" if is_wordle
                 else "handle" if is_handle else "chat")
    ctx.log_event(
        f"[时序·proactive入口] {time.strftime('%H:%M:%S')}."
        f"{int(_proactive_entry_at*1000)%1000:03d} "
        f"game={_game_tag} speaker={ai_speaker} silence_msg_len={len(silence_msg or '')}"
    )
    # 不再打印 "说话人: " 前缀 + 流式正文（旧逻辑行缓冲延迟、且和按句 [X·说] 日志重复）；
    # 本轮发言统一由 lumi_tts._flush_tts 的按句 [X·说] 日志输出。
    screen_b64 = ctx.capture_screen()
    if silence_msg:
        if screen_b64:
            messages = (
                [{"role": "system", "content": system_content}]
                + fb.history
                + [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screen_b64}"}},
                    {"type": "text", "text": silence_msg},
                ]}]
            )
        else:
            messages = (
                [{"role": "system", "content": system_content}]
                + fb.history
                + [{"role": "user", "content": silence_msg}]
            )
    else:
        if screen_b64:
            # 有截图时切换为 user 消息（多模态 API 限制），文字框定为屏幕上下文而非用户发言
            messages = (
                [{"role": "system", "content": system_content}]
                + fb.history
                + [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screen_b64}"}},
                    {"type": "text", "text": "[当前屏幕画面，请参考后按主动说话指令继续。]"},
                ]}]
            )
        else:
            # 独播闲聊：用 system 角色代替 user，模型不会感知到"沉默的用户"
            messages = (
                [{"role": "system", "content": system_content}]
                + fb.history
                + [{"role": "system", "content": "请按主动说话指令继续。"}]
            )
    tool_results = []
    _game_decision_fired = [False]  # 用list包装以便闭包修改

    def _fire_game_decision(results):
        """tool_call解析完毕后立即触发游戏操作（不等TTS播完）"""
        if game_request and results and not _game_decision_fired[0]:
            if is_cancelled_game_request(game_request):
                _game_decision_fired[0] = True
                return
            _game_decision_fired[0] = True
            tc = results[0]
            decision = {"action": tc["name"], **tc["arguments"]}
            game_request.result = decision
            game_request.result_event.set()
            ctx.log_event(f"{C_FAST}[快脑·游戏决策·提前触发] {decision}{C_RESET}")

    # 击败/获胜时放开 token 上限，让 Lumi 有空间演绎
    with ctx.slot_lock:
        _bs_situation = ctx.context_slot.get("_buckshot_situation", "")
    if _bs_situation in ("defeated", "victory"):
        _proactive_max_tokens = 400
    elif game_request:
        _proactive_max_tokens = 200
    else:
        _proactive_max_tokens = 120   # 普通聊天主动说话：压短到 ~1-2 句（之前 150 偏长）
    if pending_commentary_request:
        _proactive_max_tokens = 80
    # 游戏环节字数硬截（同 chat_and_speak）。
    _proactive_in_game = bool(pending_commentary_request or game_request or is_gaming)
    _proactive_max_chars: int | None = 80 if _proactive_in_game else 160
    _ai_cfg = ctx.speaker_configs[ai_speaker]
    _ai_voice_name = _ai_cfg.voice_name
    _ai_subtitle_meta = {
        "speaker": ai_speaker,
        "color": _ai_cfg.subtitle_color,
        "label": _ai_cfg.subtitle_label,
    }
    # TTS 输出虚拟声卡按 ai_speaker 路由
    _ai_cable_index = ctx.cable_indices.get(ai_speaker, cable_index)
    _speech_source = ctx.turn_metrics.get("source", "proactive")
    _speech_output = speech_output_arbiter.arbiter.request_start(
        speaker=ai_speaker,
        source=_speech_source,
        policy=_speech_policy_for_turn(
            _speech_source,
            game_request=game_request,
            pending_commentary_request=pending_commentary_request,
        ),
        reason=f"{_speech_source}_started",
    )
    if _speech_output is None:
        ctx.log_event(f"{C_FAST}[表现调度] {ai_speaker} {_speech_source} 被跳过：当前已有输出{C_RESET}")
        return ""

    # 把本次解说的 output_id 写进决策请求，让游戏 bridge 精确等"这手操作者这次解说"播完再
    # 推进下一手（在 speak 之前写，覆盖提前触发/兜底两条 fire 路径，不依赖闭包、不漏）。
    if game_request is not None:
        game_request.output_id = _speech_output.output_id

    with ctx.speaking_lock:
        if ctx.get_enable_tts():
            full_reply = lumi_tts.speak(
                messages, vad_model=vad_model, cable_index=_ai_cable_index,
                max_tokens=_proactive_max_tokens,
                tools=game_tools, tool_result_holder=tool_results,
                on_tool_calls_parsed=_fire_game_decision if game_request else None,
                interrupt_monitor_fn=ctx.interrupt_monitor,
                turn_metrics=ctx.turn_metrics,
                voice_name=_ai_voice_name,
                subtitle_meta=_ai_subtitle_meta,
                output_id=_speech_output.output_id,
                is_output_current_fn=speech_output_arbiter.arbiter.is_current,
                max_chars=_proactive_max_chars,
            )
            if speech_output_arbiter.arbiter.is_current(_speech_output.output_id):
                ctx.bus.publish("tts_done", {
                    "speaker": ai_speaker,
                    "output_id": _speech_output.output_id,
                }, source="execution")
                # reply 事件已移到 lumi_tts.speak() 内部（LLM 文本就绪即发，早于 TTS 播完），
                # 让 emotion_sidecar 在播音时就切表情。这里不再重复发。
        else:
            full_reply = _stream_llm_text_only(
                ctx, messages, max_tokens=_proactive_max_tokens,
                tools=game_tools, tool_result_holder=tool_results,
            )
    _speech_valid = speech_output_arbiter.arbiter.is_current(_speech_output.output_id)
    speech_output_arbiter.arbiter.mark_done(_speech_output.output_id)
    if not _speech_valid:
        ctx.log_event(f"{C_FAST}[表现调度] {ai_speaker} {_speech_source} 已被新输出取消，跳过副作用{C_RESET}")
        return ""

    # 兜底：如果回调没触发（如text_only模式），在这里处理
    if game_request and tool_results and not _game_decision_fired[0]:
        if is_cancelled_game_request(game_request):
            _game_decision_fired[0] = True
        else:
            tc = tool_results[0]
            decision = {"action": tc["name"], **tc["arguments"]}
            game_request.result = decision
            game_request.result_event.set()
            ctx.log_event(f"{C_FAST}[快脑·游戏决策] {decision}{C_RESET}")

    # 快脑没调工具 → 标记已尝试，防止同一轮反复重试
    if game_request and not tool_results and not _game_decision_fired[0]:
        if is_cancelled_game_request(game_request):
            _game_decision_fired[0] = True
        else:
            ctx.log_event(f"{C_ERR}[快脑·游戏决策] proactive_speak 未返回工具调用，标记已尝试{C_RESET}")
            game_request._proactive_attempted = True

    # 泰拉瑞亚快脑工具调用 → 路由到 bridge.set_goal()
    if _terraria_tools_injected_p and tool_results:
        for tc in tool_results:
            if tc["name"] == "set_terraria_goal" and _terraria_bridge and _terraria_bridge.running:
                if _terraria_bridge.multiplayer_mode:
                    ctx.log_event(f"{C_FAST}[快脑·泰拉瑞亚] 联机模式，忽略目标指令{C_RESET}")
                    continue
                args = tc["arguments"]
                _terraria_bridge.set_goal(
                    args["goal_type"], args["target"], args.get("reason", ""),
                    params={"direction": args.get("direction"), "quantity": args.get("quantity")}
                )
                ctx.log_event(f"{C_FAST}[快脑·泰拉瑞亚] proactive 指令: {args['goal_type']} → {args['target']}{C_RESET}")
    if not game_request:
        ctx.execute_fast_brain_tools(tool_results, "proactive_speak")

    if pending_commentary_request:
        setattr(ctx, "_last_game_spectator_commentary_at", time.time())
        mark_game_commentary_done(pending_commentary_request)

    if full_reply:
        emotion_tag, text_reply = ctx.parse_emotion(full_reply)
        text_reply = ctx.strip_stage_directions(text_reply)
        ctx.recent_lumi_outputs.append(text_reply)
        fb.append_assistant(text_reply)
        fb.trim_history(20)   # 40→20：缩短历史窗口，让旧梗（如某场咬死的"曲奇砖"）尽快滚出视野
        if ctx.memory_runtime:
            try:
                ctx.memory_runtime.on_agent_reply(
                    agent_name=ai_speaker,
                    text=text_reply,
                    session_id=ctx.get_session_id(),
                    user_input=silence_msg,
                )
            except Exception as e:
                ctx.log_event(f"{C_ERR}[记忆·Agent写入失败] {e}{C_RESET}")
        # 跨角色镜像 + 翻转调度器
        mirror_speech_to_partner(ctx.active_speakers, ctx.fast_brains, ai_speaker, text_reply)
        ctx.scheduler.advance()
        with ctx.slot_lock:
            slot_snap = dict(ctx.context_slot)
        turn_type = "game_decision" if game_request else "proactive"
        ctx.log_turn(
            user=silence_msg[:50], lumi=text_reply, emotion=emotion_tag,
            rag_hits=[], slot_snapshot=slot_snap, turn_type=turn_type,
        )


def _send_warmup_request(ctx: ConversationContext, messages):
    """异步预热请求：让豆包后台对当前前缀建立隐式缓存，下一轮真请求才有机会命中。
    失败吞掉不影响主流程。"""
    try:
        bp = fast_brain._brand_params()
        kwargs = dict(
            model=fast_brain.LLM_MODEL,
            messages=messages,
            stream=False,
            max_tokens=1,
            temperature=bp["temperature"],
            top_p=bp["top_p"],
            frequency_penalty=bp["frequency_penalty"],
            presence_penalty=bp["presence_penalty"],
        )
        if bp["logit_bias"]:
            kwargs["logit_bias"] = bp["logit_bias"]
        if bp["extra_body"]:
            kwargs["extra_body"] = bp["extra_body"]
        t0 = time.time()
        resp = fast_brain.llm_client.chat.completions.create(**kwargs)
        elapsed_ms = round((time.time() - t0) * 1000, 1)
        cached = 0
        if resp.usage is not None:
            ptd = getattr(resp.usage, "prompt_tokens_details", None)
            if ptd is not None:
                if isinstance(ptd, dict):
                    cached = ptd.get("cached_tokens", 0) or 0
                else:
                    cached = getattr(ptd, "cached_tokens", 0) or 0
        ctx.log_event(f"{C_FAST}[快脑·预热] {elapsed_ms}ms | 缓存命中 {cached} tokens{C_RESET}")
    except Exception as e:
        ctx.log_event(f"{C_ERR}[快脑·预热失败] {e}{C_RESET}")
