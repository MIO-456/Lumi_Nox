"""
快脑配置模块 — LLM 客户端、模型字典、提示词加载、参数管理。

从 lumi.py 抽出的快脑相关基础设施。本模块只暴露函数和常量，
不封装类（FastBrain 类是 Step 2 双角色工作的事，本次重构不做）。

模块内容：
- _ark_client / _qwen_client：两家供应商的 OpenAI 兼容客户端
- _BRAND_PARAMS：按品牌（doubao / qwen / openai）组织的推理参数（temperature / top_p
  / frequency_penalty / presence_penalty / logit_bias / extra_body）
- LLM_MODELS：模型注册表（key -> (display_name, client, model_id, brand_key)）
- LLM_TEMPERATURE：函数签名默认值，实际调用时被 _brand_params() 覆盖
- _current_model_key / llm_client / LLM_MODEL：当前选中的模型状态（可变全局，
  由 set_llm_model 切换；外部模块通过 fast_brain.<name> 访问以保证拿到最新值）
- set_llm_model：运行时切换模型
- _brand_params：返回当前模型对应品牌的推理参数 dict
- _compat_temp：旧 @property 装饰器（模块级 dead code，保留以兼容历史引用）
- _load_prompt：从 markdown 文件读 system prompt 正文
"""
import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()


# ======= 快脑配置 =======
_ark_client = OpenAI(
    api_key=os.getenv("ARK_API_KEY_FAST"),
    base_url="https://ark.cn-beijing.volces.com/api/v3",
)
_qwen_client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)
# 品牌推理参数配置：每个品牌一套独立参数
_BRAND_PARAMS = {
    "doubao": {
        "temperature": 1.0,
        "top_p": 0.7,
        "frequency_penalty": 0.5,
        "presence_penalty": 0.5,
        "logit_bias": {
            "182": -3,       # W (Wink首token): 适度降低
            "146487": -3,    # 转圈: 适度降低
            "861": -100,     # （: 中文左括号
            "854": -100,     # ）: 中文右括号
            "135": -100,     # (: 英文左括号
            "136": -100,     # ): 英文右括号
            "2426": -100,    # 【: 防模型用【动作】描写动作（配合 lumi_tts 括号过滤双保险）
            "2425": -100,    # 】
            "186": -100,     # [: 防 [动作]/复述 [X说] 前缀
            "188": -100,     # ]
            "2284": -100,    # 系统: 穿帮词
            "16027": -100,   # 算法: 穿帮词
            "33057": -100,   # 候选: "候选词"的首token，穿帮词
            "139338": -100,  # 回城: 王者荣耀常驻按钮文字，兜底防跨轮复读
        },
        "extra_body": {"thinking": {"type": "disabled"}},
    },
    "qwen": {
        "temperature": 1.0,
        "top_p": 0.7,
        "frequency_penalty": 0.5,
        "presence_penalty": 0.5,
        "logit_bias": {},
        "extra_body": {"enable_thinking": False},
    },
    # 豆包角色扮演模型专用品牌：复用 doubao 的采样/惩罚参数，但 logit_bias 留空——
    # doubao 品牌那套括号/穿帮词封禁的 token id 只在 2.0-mini/lite 上实测一致，角色模型
    # 是不同模型、分词器可能不同，套用旧 id 会误封随机 token、污染输出；改靠出声层的
    # 括号过滤兜底（lumi_tts / conversation 的 _output_filtered）。思考默认关压延迟。
    "doubao_character": {
        "temperature": 1.0,
        "top_p": 0.7,
        "frequency_penalty": 0.5,
        "presence_penalty": 0.5,
        "logit_bias": {},
        "extra_body": {"thinking": {"type": "disabled"}},
    },
    "openai": {
        "temperature": 0.8,
        "top_p": 1.0,
        "frequency_penalty": 0.3,
        "presence_penalty": 0.3,
        "logit_bias": {},
        "extra_body": {},
    },
}

# 模型注册表：key → (显示名, client, model_id, 品牌key)
LLM_MODELS = {
    "2.0-mini":     ("Doubao 2.0 Mini",   _ark_client,   "doubao-seed-2-0-mini-260215", "doubao"),
    "2.0-lite":     ("Doubao 2.0 Lite",   _ark_client,   "doubao-seed-2-0-lite-260428", "doubao"),
    "1.6-flash":    ("Doubao 1.6 Flash",  _ark_client,   "doubao-seed-1-6-flash-250828", "doubao"),
    "qwen3.5-flash":("Qwen 3.5 Flash",    _qwen_client,  "qwen3.5-flash", "qwen"),
    # 角色扮演专调模型（同方舟接口，复用 _ark_client）。仅用于聊天环节：不支持工具调用，
    # 游戏决策的带工具请求会经 resolve_call_target 自动回退到支持工具的模型。
    "character":    ("Doubao 角色扮演",   _ark_client,   "doubao-seed-character-251128", "doubao_character"),
}

# 不支持工具调用（function calling）的模型 key。带工具的请求（游戏决策）落到这些模型上
# 会失效，需回退到支持工具的模型来发那一次请求。
_NO_TOOL_MODELS = {"character"}
# 选中模型不支持工具时，带工具的请求回退到这个模型（仍走方舟、参数干净）。
TOOL_FALLBACK_MODEL_KEY = "2.0-lite"

# 仅用作函数签名默认值，实际调用时被 _brand_params() 覆盖
LLM_TEMPERATURE = 1.0

# 当前选中的模型（由 --model 参数覆盖）
_current_model_key = "2.0-lite"   # 默认快脑模型（2026-06-07 起 mini→lite：lite 条理更好、速度相当）
llm_client = LLM_MODELS[_current_model_key][1]
LLM_MODEL = LLM_MODELS[_current_model_key][2]

def set_llm_model(key: str):
    """切换快脑模型"""
    global llm_client, LLM_MODEL, _current_model_key
    if key not in LLM_MODELS:
        raise ValueError(f"未知模型: {key}，可选: {list(LLM_MODELS.keys())}")
    _current_model_key = key
    _, llm_client, LLM_MODEL, _ = LLM_MODELS[key]

# 模型级 extra_body 覆盖：同 doubao 品牌下"不思考"的写法因模型而异——2.0-mini 用
# thinking:disabled（品牌默认），2.0-lite 用 reasoning_effort:minimal。其余参数（括号封禁
# logit_bias / penalty 等）仍继承品牌，所以 2.0-lite 仍挂 doubao 品牌、只覆盖 extra_body。
_MODEL_EXTRA_BODY = {
    "2.0-lite": {"reasoning_effort": "minimal"},
}


def _brand_params(model_key: str = None) -> dict:
    """返回指定模型对应品牌的推理参数（省略则用当前选中模型）；该模型若有 extra_body 覆盖则用覆盖的。"""
    key = model_key or _current_model_key
    brand = LLM_MODELS[key][3]
    params = _BRAND_PARAMS[brand]
    override = _MODEL_EXTRA_BODY.get(key)
    if override is not None:
        params = {**params, "extra_body": override}  # 新 dict，不污染 _BRAND_PARAMS
    return params


def current_supports_tools() -> bool:
    """当前选中模型是否支持工具调用（function calling）。"""
    return _current_model_key not in _NO_TOOL_MODELS


def resolve_call_target(needs_tools: bool = False):
    """返回本次请求该用的 (client, model_id, brand_params)。

    普通请求用当前选中模型；当请求带工具、但当前模型不支持工具时，回退到
    TOOL_FALLBACK_MODEL_KEY——让游戏决策这类带工具的请求仍能正常工作，而聊天环节
    仍用当前选中模型（如角色扮演模型）。
    """
    if needs_tools and not current_supports_tools():
        key = TOOL_FALLBACK_MODEL_KEY
        return LLM_MODELS[key][1], LLM_MODELS[key][2], _brand_params(key)
    return llm_client, LLM_MODEL, _brand_params()

# 兼容旧引用：指向当前品牌参数（用于 temperature 默认值等）
@property
def _compat_temp():
    return _brand_params()["temperature"]

def _load_prompt(filename: str):
    """从指定提示词文件加载角色 system prompt，取 ``` 代码块中的内容"""
    path = os.path.join(os.path.dirname(__file__), filename)
    text = open(path, encoding="utf-8").read()
    # 提取第一个 ``` 代码块
    import re
    m = re.search(r"```\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # fallback: 去掉 markdown 元信息，返回全文
    return text.strip()


def _apply_persona_mode(manifest: str, is_dual: bool) -> str:
    """按本场是独播还是双角色同台，裁剪人设里用标记框起来的差异段。

    人设正文里：
    - [[DUAL_ONLY]]...[[/DUAL_ONLY]]：只有双角色同台才保留（提到搭档、把历史里的
      assistant 当搭档发言、结尾“正在和搭档一起直播”）。
    - [[SOLO_ONLY]]...[[/SOLO_ONLY]]：只有独播才保留（结尾“正在直播”，不提搭档）。

    无论哪种模式，标记本身一律剥掉，绝不能进入最终送模型的 character_manifest，
    否则端到端会把方括号标记当文字念出来。
    """
    import re
    dual_block = r"\[\[DUAL_ONLY\]\](.*?)\[\[/DUAL_ONLY\]\]"
    solo_block = r"\[\[SOLO_ONLY\]\](.*?)\[\[/SOLO_ONLY\]\]"
    if is_dual:
        manifest = re.sub(solo_block, "", manifest, flags=re.DOTALL)
        manifest = re.sub(dual_block, lambda m: m.group(1), manifest, flags=re.DOTALL)
    else:
        manifest = re.sub(dual_block, "", manifest, flags=re.DOTALL)
        manifest = re.sub(solo_block, lambda m: m.group(1), manifest, flags=re.DOTALL)
    # 标记/块删除后可能留下多余空行，压平
    manifest = re.sub(r"\n{3,}", "\n\n", manifest)
    return manifest.strip()


class FastBrain:
    """每个角色一个实例，持有自己的 history / system_prompt / logit_bias / 推理参数。

    Step 2 双角色场景：speaker_scheduler 选定 speaker 后，conversation 调用
    fast_brains[speaker] 获取当前角色的 history / system_prompt，跨角色互不干扰。

    单角色场景：active_speakers 只一项时只创建一个 FastBrain 实例，行为跟 Step 1 单角色一样。

    本类不包装 LLM 调用本身（仍然走模块级 llm_client + 当前 _current_model_key），只把
    "每角色一份的可变状态"封装起来。这是 Step 1 重构留下的 ConversationContext 字段
    的进一步聚合：history / system_prompt 这些原本散在 ctx 顶层的字段，现在归属于角色实例。
    """

    def __init__(self, speaker_name: str, speaker_config, initial_history: list = None):
        """
        speaker_name: "Lumi" / "Nox"
        speaker_config: SpeakerConfig 实例
        initial_history: 可选，传入一个已存在的 list 引用作为本实例的 history。
                         单角色场景下 caller 可以传入 lumi.history 全局，让 fast_brain
                         的 history 和 lumi 模块全局共享同一 list 引用（保持兼容性，
                         lumi.py 别处读写 history 全局仍然反映在 fast_brain 这边）。
                         双角色场景下每个角色传 None（独立 list）。
        """
        self.speaker_name = speaker_name
        self.config = speaker_config
        self.history: list = initial_history if initial_history is not None else []
        # 加载角色专属提示词（V4 / V1 / V3 fallback 等）
        self.system_prompt = _load_prompt(filename=speaker_config.prompt_file)

    def append_user(self, content: str):
        """追加 user 消息（含跨角色镜像的 [对方说] 前缀）。"""
        self.history.append({"role": "user", "content": content})

    def append_assistant(self, content: str):
        """追加自己的回复。"""
        self.history.append({"role": "assistant", "content": content})

    def trim_history(self, max_turns: int = 20):
        if len(self.history) > max_turns:
            self.history[:] = self.history[-max_turns:]

    def get_brand_params(self) -> dict:
        """复用模块级 _brand_params，但叠加角色专属 logit_bias。
        始终拷贝嵌套 dict（logit_bias / extra_body），保证调用方修改返回值不污染模块级常量。"""
        src = _brand_params()
        bp = {
            **src,
            "logit_bias": dict(src.get("logit_bias", {})),
            "extra_body": dict(src.get("extra_body", {})),
        }
        if self.config.logit_bias:
            bp["logit_bias"].update(self.config.logit_bias)
        return bp
