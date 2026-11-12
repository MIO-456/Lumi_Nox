"""整场直播运行架构的单一真相源。

开播时由 lumi.py 从 --chat-arch 设定一次，运行中不再改变。所有"此刻该不该
走端到端"的判断都查这里，不再各自看状态机。
"""

_VALID = ("text", "realtime")
_architecture = "text"  # 改造后默认：文本架构


def set_architecture(mode: str) -> None:
    if mode not in _VALID:
        raise ValueError(f"未知运行架构: {mode}，可选: {_VALID}")
    global _architecture
    _architecture = mode


def get_architecture() -> str:
    return _architecture


def reset() -> None:
    """复位到默认（仅测试用）。"""
    global _architecture
    _architecture = "text"


def is_realtime_active(state_name: str) -> bool:
    """端到端聊天链路此刻是否生效：端到端架构 且 处于聊天状态。"""
    return _architecture == "realtime" and state_name == "CHATTING"


def use_independent_tts() -> bool:
    """发声是否走独立 TTS（文本架构）而非借端到端。"""
    return _architecture == "text"
