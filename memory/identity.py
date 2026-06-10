"""身份键的唯一构造/判定入口。

身份键格式（带命名空间前缀）：
- B站观众：  bili:{uid}
- 主播本人：  creator:mio
- 历史无uid： legacy:{昵称}
"""

BILI_PREFIX = "bili:"
LEGACY_PREFIX = "legacy:"
CREATOR_KEY = "creator:mio"


def bili_identity(uid) -> str:
    return f"{BILI_PREFIX}{uid}"


def creator_identity() -> str:
    return CREATOR_KEY


def legacy_identity(display_name: str) -> str:
    return f"{LEGACY_PREFIX}{display_name}"


def is_legacy(identity_key: str) -> bool:
    return str(identity_key or "").startswith(LEGACY_PREFIX)


def legacy_display_name(identity_key: str) -> str:
    """从 legacy:{name} 取回原昵称（昵称中可含冒号）。"""
    key = str(identity_key or "")
    if not key.startswith(LEGACY_PREFIX):
        return ""
    return key[len(LEGACY_PREFIX):]
