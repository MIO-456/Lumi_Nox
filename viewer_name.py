"""喂给模型前的观众名字归一化 + 多人进房折叠（纯函数，零项目依赖，可单测）。

记忆系统不经过这里——记忆按 uid 存、显示名用真实昵称；这里只处理"拼进模型输入
文本"的那一份名字，避免 bili_ 长数字默认名被照读、多人进房被逐个念名字。
"""
import re

_BILI_DEFAULT_NAME = re.compile(r"^bili_\d+$")


def normalize_viewer_name(name: str) -> str:
    """B 站默认用户名 bili_<纯数字> → "B站用户"；其余昵称原样返回。"""
    if name and _BILI_DEFAULT_NAME.match(name):
        return "B站用户"
    return name


def is_enter_room_item(item) -> bool:
    """判定一条观众输入是不是"进入直播间"事件。

    B 站直播真实形态：进房走 `interact` 事件、display_text 是"进入直播间"
    （`bilibili_danmaku` 从不分发 enter_room）；同时兼容遗留/overlay 的 enter_room source。
    其它 interact（点赞/关注/分享）display_text 不是"进入直播间"，不算进房。
    """
    if item.get("source") == "enter_room":
        return True
    if item.get("source") == "interact":
        return (item.get("display_text") or "").strip() == "进入直播间"
    return False


def fold_enter_room_inputs(items, *, is_recognized, normalize=normalize_viewer_name,
                           max_named=2, is_enter=is_enter_room_item):
    """多个进房事件折叠成一条，避免模型逐个念陌生名字。

    items: 观众输入 dict 列表（含 source/speaker/uid/text/...）。
    is_recognized(item)->bool: 判定该进房者是否老粉（有记忆事实）。
    is_enter(item)->bool: 判定该条是否进房事件（默认认 interact+进入直播间 与遗留 enter_room）。
    折叠规则：当进房条目 ≥2 时，替换成一条合成条目，其余条目原序保留；
    老粉单独点名（最多 max_named 位、名字经 normalize），其余只报总数。
    单个进房（<2）原样返回不折叠。
    """
    enters = [it for it in items if is_enter(it)]
    if len(enters) < 2:
        return items

    total = len(enters)
    old_fan_names = []
    for it in enters:
        if len(old_fan_names) >= max_named:
            break
        if is_recognized(it):
            old_fan_names.append(normalize(it.get("speaker", "")))

    if old_fan_names:
        names = "、".join(f"「{n}」" for n in old_fan_names)
        text = f"进房：刚刚来了 {total} 位观众，其中老朋友{names}也来了"
    else:
        text = f"进房：刚刚来了 {total} 位新观众"

    synthetic = {
        "text": text,
        "display_text": text,
        "speaker": "未知",
        "label": "进房",
        "source": "enter_room",
        "uid": 0,
        "_prebuilt_line": True,
    }
    others = [it for it in items if not is_enter(it)]
    return others + [synthetic]
