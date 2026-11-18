"""Explicit, disposable local room demo; never merges saved service credentials."""
from public_host.config import default_config, runtime_config


def demo_config(*, with_game=False):
    config = default_config()
    config["mode"] = "demo"
    config["roles"][1]["enabled"] = True
    config["program"]["auto_chat_interval"] = 2
    config["program"]["stages"] = [
        {"id": "opening", "kind": "opening", "title": "欢迎来到直播间", "duration": 6,
         "prompt": "欢迎观众，介绍今天的双人直播。"},
        {"id": "chat", "kind": "chat", "title": "和观众聊一会儿", "duration": 15,
         "prompt": "聊聊今天的小确幸，邀请观众留言。"},
        *([{"id": "gomoku", "kind": "activity", "title": "五子棋对决", "duration": 35,
            "activity": "gomoku"}] if with_game else []),
        {"id": "after", "kind": "chat", "title": "赛后闲聊" if with_game else "继续聊天", "duration": 10,
         "prompt": "分享感想并回应观众。"},
        {"id": "ending", "kind": "ending", "title": "下次见", "duration": 5,
         "prompt": "感谢观众，简短告别。"},
    ]
    result = runtime_config(config)
    result["demo_playback_seconds"] = 2.0
    return result
