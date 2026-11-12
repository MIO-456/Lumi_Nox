"""
Kingdom Rush 游戏桥接模块 - 作为 Lumi 子模块运行
封装 auto_loop 为后台线程，通过 event_callback 推送游戏事件给 lumi.py

可独立测试：python -m games.kingdom_rush.bridge
"""

import threading
import time
from types import SimpleNamespace

from .startup import StartupCancelled, StartupControl, StartupTimeout

# ==================== 常量 ====================

HOST = "127.0.0.1"
PORT = 9878

KR_GAME_PROMPT_CONTROLLER = """## 王国保卫战 — 你正在直播塔防游戏

你正在直播《王国保卫战》，操控建塔和防御。
只说当前局面里明确列出的塔和怪物，不要凭游戏常识补不存在的防御塔。
如果当前局面没有写某座塔已经在场，就不要说它正在输出、锁怪、救场。
你不直接控制建塔程序，所以不要承诺“接下来我会造/升级某座塔”。
只有“最近发生的事”明确写了已建成或已升级，才能用完成时描述该操作。
绝对不要提"AI""Bot""脚本""规则引擎""算法""系统"等技术词。
你是玩家本人，所有操作都是你做的。
"""

KR_GAME_PROMPT_SPECTATOR = """## 王国保卫战 — 围观吐槽

操控者正在直播《王国保卫战》，你在旁边看。
你可以评价对方的塔阵布局、嘲讽踩坑、为下一波敌人起哄，但不要替对方做决定。
只说当前局面里明确列出的塔和怪物，不要凭游戏常识补不存在的防御塔。
不要替操控者承诺下一步会造什么塔；只有明确发生的建造/升级事件才能说成事实。
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
        self._startup = None
        self._lifecycle_lock = threading.RLock()
        self._round = SimpleNamespace(in_round=False)

    @property
    def in_round(self):
        return self.running and self._round.in_round

    @property
    def session_id(self):
        return self._startup.session_id if self._startup else ""

    def accepts_event(self, data):
        session = self._startup
        return bool(session and self.running and not session.cancel_event.is_set()
                    and data.get("session_id") == session.session_id)

    def start(self, host=HOST, port=PORT):
        """连接游戏 + 启动 auto_loop 线程"""
        with self._lifecycle_lock:
            if self.running:
                return
            session = StartupControl()
            self._startup = session
            self.running = True
            self._host = host
            self._port = port
            self._round = SimpleNamespace(in_round=False, _event_callback=lambda kind, data:
                self._push_event(kind, data, session))
            self._thread = threading.Thread(
                target=self._loop_wrapper, args=(session, self._round),
                daemon=True, name="kingdom_rush_auto",
            )
            self._thread.start()

    def wait_ready(self, is_active=None):
        """Wait for this session's real handshake or its specific startup error."""
        session = self._startup
        if session is None:
            raise RuntimeError("王国保卫战尚未启动")
        try:
            while True:
                if is_active is not None and not is_active():
                    raise StartupCancelled("导演已取消王国保卫战启动请求")
                session.check()
                if session.error:
                    raise RuntimeError(session.error)
                if session.ready:
                    if not self.running or self._startup is not session:
                        raise RuntimeError("王国保卫战连接已结束")
                    return session.session_id
                session.finished.wait(session.remaining(0.1))
        except Exception:
            # Do not leave a launcher running after the waiter has given up.
            if self._startup is session:
                self.stop()
            raise

    def stop(self, kill_game=True):
        """停止游戏循环 + 断开 TCP + 可选关闭游戏进程"""
        with self._lifecycle_lock:
            self.running = False
            self._round.in_round = False
            session, bot, worker = self._startup, self._bot, self._thread
            if session:
                session.cancel(kill_game=kill_game)
            if bot:
                bot.close()
            self._bot = None
        # A playing worker can itself be synchronously waiting for this stop via
        # round_complete. Only join startup workers; cancellation already blocks
        # their future actions and the closed socket stops the playing loop.
        if (worker and worker is not threading.current_thread()
                and session is not None and not session.ready):
            worker.join(timeout=2)

    def _publish_to_bus(self, event_type: str, data: dict):
        """把桥接器事件转发到总线"""
        self._bus.publish(event_type, data, source="kingdom_rush")

    def _push_event(self, event_type, data, session=None):
        with self._lifecycle_lock:
            session = session or self._startup
            if (session is not self._startup or session is None or not self.running
                    or session.cancel_event.is_set()):
                return
            if self._event_callback:
                callback = self._event_callback
            else:
                return
        # Subscribers can synchronously request stop on another thread. Do not
        # hold a lifecycle lock during dispatch; consumers also check session_id.
        callback(event_type, {**data, "session_id": session.session_id})

    def _loop_wrapper(self, session, round_context):
        """后台线程入口：连接 → auto_loop → 清理"""
        bot = None
        try:
            session.check()
            from .bot import KingdomRushBot
            from .ai import auto_loop, launch_game

            session.check()
            bot = KingdomRushBot()
            bot.host = self._host
            bot.port = self._port
            with self._lifecycle_lock:
                session.check()
                if self._startup is not session:
                    return
                self._bot = bot

            def emit(kind, data):
                self._push_event(kind, data, session)

            # 尝试连接，连不上就自动启动游戏
            if not bot.connect(retries=3, interval=1, quiet=True, startup=session):
                session.check()
                emit("game_event",
                    {"text": "游戏未运行，自动启动中...", "event": "launching"})
                from .ai import trigger_launcher_start
                if not launch_game(startup=session):
                    raise RuntimeError("找不到王国保卫战启动器窗口，游戏未能启动")
                # Every stage consumes the same deadline, including socket.connect.
                connected = False
                for attempt in range(3):
                    session.check()
                    if attempt > 0:  # 首次触发已在 launch_game 内完成
                        emit("game_event",
                            {"text": f"启动器未进入游戏，第{attempt}/2次重试触发开始",
                             "event": "relaunch_retry"})
                        trigger_launcher_start(startup=session)
                    if bot.connect(retries=5, interval=2, quiet=True, startup=session):
                        connected = True
                        break
                if not connected:
                    raise RuntimeError("游戏启动后仍无法连接 Bridge（127.0.0.1:9878）")

            # 读取欢迎消息
            session.check()
            welcome = bot.receive(timeout=session.remaining(2.0))
            session.check()
            if not isinstance(welcome, dict) or welcome.get("type") != "connected" or not welcome.get("version"):
                raise RuntimeError("已连接端口，但未收到有效的 Kingdom Rush Bridge 欢迎消息")
            bridge_ver = welcome["version"]
            with self._lifecycle_lock:
                session.mark_ready()
            emit("game_event",
                {"text": f"已连接 Bridge {bridge_ver}", "event": "connected"})

            # 运行自动循环（传入 bridge 供设置 in_round 属性）
            session.check()
            auto_loop(bot, bridge_ver, event_callback=emit, bridge=round_context)

        except StartupCancelled:
            pass
        except Exception as e:
            if not session.cancel_event.is_set():
                session.fail(e)
                self._push_event("game_event", {"text": f"桥接异常: {e}", "event": "error"}, session)
                session.cancel()
        finally:
            if bot:
                bot.close()
            with self._lifecycle_lock:
                if self._startup is session:
                    self.running = False
                    self._bot = None
                    round_context.in_round = False
                    if not session.ready and not session.finished.is_set():
                        session.fail("王国保卫战启动线程已退出")


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
