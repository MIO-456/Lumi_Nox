"""
Kingdom Rush 对战历史持久化模块

按关卡+模式分组存储对战记录（胜败都保留），
每关每模式最多 5 条，新记录挤掉最老的。
启动时加载，每局结束后追加保存。
"""

import json
import os

# 绝对路径（基于本模块所在目录），避免不同 cwd 把历史写到不同文件——
# 之前相对 "logs/..." 导致 worktree 与 main 各存一份、学习/刷星基于错误历史。
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(_BASE_DIR, "logs", "kr_battle_history.json")
MAX_RECORDS_PER_KEY = 5


def _make_key(level_idx, level_mode):
    """生成存储 key，如 'level_4_mode_1'"""
    return f"level_{level_idx}_mode_{level_mode}"


def load_history(filepath=HISTORY_FILE):
    """从文件加载对战历史，文件不存在返回空字典"""
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_history(history, filepath=HISTORY_FILE):
    """将对战历史写入文件"""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def add_record(history, record):
    """追加一条对战记录，超过上限时挤掉最老的

    record 需包含 level_idx 和 level_mode 字段。
    返回更新后的 history（原地修改）。
    """
    level_idx = record.get("level_idx")
    level_mode = record.get("level_mode", 1)
    if level_idx is None:
        return history

    key = _make_key(level_idx, level_mode)
    if key not in history:
        history[key] = []

    history[key].append(record)

    # 保留最近 N 条
    if len(history[key]) > MAX_RECORDS_PER_KEY:
        history[key] = history[key][-MAX_RECORDS_PER_KEY:]

    return history


def get_level_history(history, level_idx, level_mode=1):
    """获取某关某模式的历史记录列表"""
    key = _make_key(level_idx, level_mode)
    return history.get(key, [])
