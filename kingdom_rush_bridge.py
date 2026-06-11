"""
Kingdom Rush 游戏桥接模块 - 作为 Lumi 子模块运行
封装 auto_loop 为后台线程，通过 event_callback 推送游戏事件给 lumi.py

可独立测试：python kingdom_rush_bridge.py
"""

import subprocess
import threading
import time

# ==================== 常量 ====================

HOST = "127.0.0.1"
PORT = 9878

KR_GAME_PROMPT_CONTROLLER = """## 王国保卫战 — 你正在直播塔防游戏

你正在直播《王国保卫战》，操控建塔和防御。
只说当前局面里明确列出的塔和怪物，不要凭游戏常识补不存在的防御塔。
如果当前局面没有写某座塔已经在场，就不要说它正在输出、锁怪、救场。
绝对不要提"AI""Bot""脚本""规则引擎""算法""系统"等技术词。
你是玩家本人，所有操作都是你做的。
"""

KR_GAME_PROMPT_SPECTATOR = """## 王国保卫战 — 围观吐槽

操控者正在直播《王国保卫战》，你在旁边看。
你可以评价对方的塔阵布局、嘲讽踩坑、为下一波敌人起哄，但不要替对方做决定。
只说当前局面里明确列出的塔和怪物，不要凭游戏常识补不存在的防御塔。
绝对不要提"AI""Bot""脚本""规则引擎""算法""系统"等技术词。
"""

# 兼容旧引用（如有外部直接 import KR_GAME_PROMPT 的地方）
KR_GAME_PROMPT = KR_GAME_PROMPT_CONTROLLER


class KingdomRushBridge:
    """Lumi ↔ Kingdom Rush 桥接层

    在后台线程运行 auto_loop（自动选关→对战→换关），
    通过 event_callback 推送游戏事件/状态给 lumi.py。
    """

    def __init__(self, event_callback=None, bus=None):
        """
        event_callback: func(event_type: str, data: dict)
            event_type: "game_event" | "game_state"
        bus: 可选事件总线实例，传入后优先用总线通信
        """
        self._bus = bus
        self.running = False
        self._event_callback = self._publish_to_bus if bus else event_callback
        self._thread = None
        self._bot = None
        # 当前是否在一局关卡中（供导演系统判断）
        self.in_round = False

    def start(self, host=HOST, port=PORT):
        """连接游戏 + 启动 auto_loop 线程"""
        self.running = True
        self._host = host
        self._port = port
        self._thread = threading.Thread(
            target=self._loop_wrapper, daemon=True, name="kingdom_rush_auto"
        )
        self._thread.start()

    def stop(self, kill_game=True):
        """停止游戏循环 + 断开 TCP + 可选关闭游戏进程"""
        self.running = False
        if self._bot:
            try:
                self._bot.close()
            except Exception:
                pass
        self._bot = None
        if kill_game:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "Kingdom Rush.exe"],
                    capture_output=True, timeout=5)
            except Exception:
                pass

    def _publish_to_bus(self, event_type: str, data: dict):
        """把桥接器事件转发到总线"""
        self._bus.publish(event_type, data, source="kingdom_rush")

    def _push_event(self, event_type, data):
        if self._event_callback:
            self._event_callback(event_type, data)

    def _loop_wrapper(self):
        """后台线程入口：连接 → auto_loop → 清理"""
        try:
            from kingdom_rush_bot import KingdomRushBot
            from kingdom_rush_ai import auto_loop, launch_game

            bot = KingdomRushBot()
            bot.host = self._host
            bot.port = self._port
            self._bot = bot

            # 尝试连接，连不上就自动启动游戏
            if not bot.connect(retries=3, interval=1, quiet=True):
                self._push_event("game_event",
                    {"text": "游戏未运行，自动启动中...", "event": "launching"})
                if not launch_game():
                    self._push_event("game_event",
                        {"text": "游戏启动失败", "event": "launch_fail"})
                    self.running = False
                    return
                # 重试 15 次 × 2 秒 = 30 秒，和 lumi.py _on_bus_start_activity 的
                # kingdom_rush 等待 timeout 对齐。原来是 60 次（2 分钟）—— bridge
                # 慢慢重试时 director 已经误判 ready 进了 PLAYING_KR，状态卡死，
                # 用户手动切才回退（实测 2026-05-10）。
                if not bot.connect(retries=15, interval=2):
                    self._push_event("game_event",
                        {"text": "游戏启动后仍无法连接", "event": "connect_fail"})
                    self.running = False
                    return

            # 读取欢迎消息
            welcome = bot.receive(timeout=2.0)
            bridge_ver = welcome.get("version", "?") if welcome else "未知"
            self._push_event("game_event",
                {"text": f"已连接 Bridge {bridge_ver}", "event": "connected"})

            # 运行自动循环（传入 bridge 供设置 in_round 属性）
            auto_loop(bot, bridge_ver, event_callback=self._event_callback, bridge=self)

        except Exception as e:
            self._push_event("game_event",
                {"text": f"桥接异常: {e}", "event": "error"})
        finally:
            self.running = False
            if self._bot:
                try:
                    self._bot.close()
                except Exception:
                    pass


# ==================== 独立测试 ====================

if __name__ == "__main__":
    def _test_callback(event_type, data):
        print(f"  [{event_type}] {data}")

    bridge = KingdomRushBridge(event_callback=_test_callback)
    print("启动 Kingdom Rush Bridge...")
    bridge.start()

    try:
        while bridge.running:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止...")
        bridge.stop()
