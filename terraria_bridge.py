"""
泰拉瑞亚游戏桥接模块 - 作为 Lumi 子模块运行
管理游戏连接、运行 survival_loop、事件回调、目标队列

可独立测试：python terraria_bridge.py
"""

import queue
import socket
import time
import threading
from dataclasses import dataclass, field


def _probe_mod_port(host: str, port: int, timeout: float = 3.0) -> bool:
    """快速 TCP 探活：能在 timeout 内连上就认为 mod 服务真在跑。
    窗口检测只看标题字符串，残留进程 / 停在主菜单 / 浏览器误匹配等情况下
    窗口存在但 9877 没人监听，再走原 connect 会白白阻塞到 120s 上限。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass

# ==================== 常量 ====================

HOST = "127.0.0.1"
PORT = 9877

# 泰拉瑞亚环节通用底线：说话风格仍按各角色 persona 走，这里只声明"不要做什么"。
_TERRARIA_COMMON_RULES = """### 表达边界
- 可以自由发挥：评价装备难看、吐槽对方操作、放狠话、表达观点——任何主观立场都行
- 但事实层面必须照状态里列出的内容来，**不要编造**：
  HP、生死、装备、武器、位置、附近的怪/玩家、刚才发生的事
  —— 这些只说局面快照里写了的；没写的就别提，别瞎补"我刚打死了 XX""我背包里有 YY""我在 ZZ 区域"
- 别提"调用工具""Bot""脚本""API"之类技术词
- 简短口语，1-2 句话
"""

TERRARIA_GAME_PROMPT_CONTROLLER = f"""## 泰拉瑞亚 — 操作者身份

你正在玩泰拉瑞亚生存模式。你有一个自动执行的身体：
你决定"做什么"，身体会自动执行"怎么做"——自动寻路、挖矿、战斗、回家合成。

### 你的能力
- 调用 set_terraria_goal 下达目标（采集/合成/探索/Boss 准备）

{_TERRARIA_COMMON_RULES}"""

TERRARIA_GAME_PROMPT_SPECTATOR = f"""## 泰拉瑞亚 — 旁观者身份

操控者正在玩泰拉瑞亚生存模式。你和她同画面，
能看到同一个世界里发生的一切——HP、装备、位置、附近的怪、刚才的事件。
区别只在你没有操作权：不能下达目标，但局面你都看得见，正常评论就行。

{_TERRARIA_COMMON_RULES}"""

# 兼容旧引用
TERRARIA_GAME_PROMPT = TERRARIA_GAME_PROMPT_CONTROLLER

# 快脑工具定义（OpenAI chat/completions 格式）
TERRARIA_GOAL_TOOL = {
    "type": "function",
    "function": {
        "name": "set_terraria_goal",
        "description": "给泰拉瑞亚下达战略目标",
        "parameters": {
            "type": "object",
            "properties": {
                "goal_type": {
                    "type": "string",
                    "enum": ["gather", "craft", "explore", "boss_prep"],
                },
                "target": {"type": "string", "description": "目标物品/Boss英文名"},
                "reason": {"type": "string", "description": "1句话理由"},
                "direction": {
                    "type": "integer", "enum": [-1, 1],
                    "description": "探索方向：-1=左, 1=右（仅 explore 类型需要）",
                },
                "quantity": {
                    "type": "integer",
                    "description": "目标数量（仅 gather 类型需要，默认按合成需求自动计算）",
                },
            },
            "required": ["goal_type", "target", "reason"],
        },
    },
}


class TerrariaBridge:
    """Lumi ↔ 泰拉瑞亚桥接层

    管理游戏连接生命周期，在后台线程运行 survival_loop，
    通过 event_callback 推送游戏事件/状态给 lumi.py。
    """

    def __init__(self, event_callback=None, bus=None):
        """
        event_callback: func(event_type: str, data: dict)
            event_type: "game_event" | "game_state"
        bus: 可选事件总线实例，传入后优先用总线通信
        """
        self._bus = bus
        self.goal_queue = queue.Queue(maxsize=1)
        self.goal_interrupt = threading.Event()
        self.running = False
        self.multiplayer_mode = False  # 联机模式标志（survival_loop 内更新）
        self._event_callback = self._publish_to_bus if bus else event_callback
        self._conn = None       # TerrariaConnection (terraria_bot.TerrariaBridge)
        self._runner = None     # TaskRunner
        self._engine = None     # BehaviorEngine
        self._thread = None
        self._last_state_push = 0
        self._save_complete = threading.Event()  # 存档完成信号，由 mod 回复触发

    @staticmethod
    def _launch_game_process():
        """启动泰拉瑞亚联机环境（服务器 + 两个客户端）"""
        import subprocess, os
        bat_path = os.path.join(os.path.dirname(__file__), "泰拉启动联机.bat")
        if not os.path.exists(bat_path):
            print(f"  [terraria_bridge] 联机启动脚本不存在: {bat_path}")
            raise FileNotFoundError(f"联机启动脚本不存在: {bat_path}")

        print(f"  [terraria_bridge] 启动联机脚本: {bat_path}")
        try:
            # 用 start 命令弹出新窗口运行 bat（空引号是窗口标题）
            ret = os.system(f'start "" "{bat_path}"')
            print(f"  [terraria_bridge] start 命令返回码: {ret}")
            if ret != 0:
                raise RuntimeError(f"start 命令失败, 返回码={ret}")
        except Exception as e:
            print(f"  [terraria_bridge] 启动失败: {type(e).__name__}: {e}")
            raise

    def start(self, host=HOST, port=PORT, auto_launch=True):
        """连接游戏 + 启动 survival_loop 线程

        auto_launch: 游戏没运行时自动启动联机环境（服务器+两个客户端）
        """
        from terraria_bot import TerrariaBridge as TerrariaConnection
        from terraria_bot import BehaviorEngine, TaskRunner

        if auto_launch:
            from terraria_bot import find_and_activate_window
            print("  [terraria_bridge] 检测游戏窗口...")
            game_running = (find_and_activate_window("泰拉瑞亚")
                            or find_and_activate_window("Terraria"))
            print(f"  [terraria_bridge] 游戏窗口检测结果: {'已运行' if game_running else '未找到'}")
            if game_running:
                print(f"  [terraria_bridge] TCP 探活 {host}:{port} (3s)...")
                if not _probe_mod_port(host, port, timeout=3.0):
                    print(f"  [terraria_bridge] 探活失败，按未启动处理，重新拉 bat")
                    game_running = False
            if not game_running:
                self._launch_game_process()

        print(f"  [terraria_bridge] 开始 TCP 连接 {host}:{port} (最多等120秒)...")
        self._conn = TerrariaConnection(host=host, port=port)
        if not self._conn.connect(timeout=120):
            raise ConnectionError(f"无法连接泰拉瑞亚 {host}:{port} (120秒超时)")

        self._engine = BehaviorEngine(self._conn)
        self._runner = TaskRunner(self._engine)
        self.running = True

        self._thread = threading.Thread(
            target=self._loop_wrapper, daemon=True, name="terraria_survival"
        )
        self._thread.start()

    def stop(self):
        """停止 survival_loop + 断开 TCP"""
        self.running = False
        self.goal_interrupt.set()  # 让循环尽快退出
        if self._conn:
            try:
                self._conn.set_auto_mode(False)
            except Exception:
                pass
            try:
                self._conn.disconnect()
            except Exception:
                pass
        self._conn = None
        self._runner = None
        self._engine = None

    def save_and_quit(self):
        """发送存档指令，等待 mod 确认后再断开连接。

        流程：
          1. 通过 TCP 向 mod 发送 {"action": "save"} 指令
          2. 最多等待 30 秒，直到收到存档完成的回复
          3. 若超时或发送失败，降级为直接 stop()
          4. 最终无论如何都调用 stop() 确保清理完毕

        注意：mod 端需要响应 action=save，并回复 {"type": "save_complete"}
        才能正常触发完成信号；若 mod 尚未实现该消息，将自动超时降级。
        """
        save_sent = False

        if self._conn and self._conn.connected:
            # 清除上一次可能残留的信号
            self._save_complete.clear()

            # 注册一次性回调：监听 mod 回复的存档完成消息
            def _on_save_reply(msg: dict):
                if msg.get("type") == "save_complete":
                    self._save_complete.set()

            try:
                self._conn.on_message(_on_save_reply)
            except Exception as e:
                print(f"  [terraria_bridge] 注册存档回调失败: {e}")

            # 向 mod 发送存档指令（mod 端需支持 action=save）
            try:
                self._conn.send({"action": "save"})
                save_sent = True
                print("  [terraria_bridge] 已发送存档指令，等待 mod 确认（最多 30 秒）...")
            except Exception as e:
                print(f"  [terraria_bridge] 发送存档指令失败: {e}，直接降级退出")

        if save_sent:
            # 等待存档完成信号，超时则降级
            confirmed = self._save_complete.wait(timeout=30)
            if confirmed:
                print("  [terraria_bridge] 存档完成，正常退出")
            else:
                print("  [terraria_bridge] 等待存档超时（30s），强制退出")

        # 无论存档是否成功，始终执行 stop() 清理连接
        self.stop()

    def set_goal(self, goal_type, target, reason="", params=None):
        """慢脑/快脑下达目标 → 清空旧目标 → 入队"""
        from terraria_bot import StrategicGoal

        # 清空旧目标
        while not self.goal_queue.empty():
            try:
                self.goal_queue.get_nowait()
            except queue.Empty:
                break

        # 设置中断标志
        self.goal_interrupt.set()

        goal = StrategicGoal(
            goal_type=goal_type,
            target=target,
            reason=reason,
            params=params or {},
        )
        self.goal_queue.put(goal)
        goal_label = {"gather": "收集", "craft": "合成", "explore": "探索",
                       "go_to": "前往", "boss_prep": "准备打Boss"}.get(goal_type, "")
        self._push_event("game_event", {"text": f"想去{goal_label} {target}（{reason}）"})

    def get_state_summary(self) -> str:
        """返回当前游戏状态摘要文本"""
        if not self._runner or not self._conn or not self._conn.connected:
            return "游戏未连接"
        try:
            return self._runner.build_status_summary()
        except Exception as e:
            return f"获取状态失败: {e}"

    # ---- 内部方法 ----

    def _publish_to_bus(self, event_type: str, data: dict):
        """把桥接器事件转发到总线"""
        self._bus.publish(event_type, data, source="terraria")

    def _push_event(self, event_type: str, data: dict):
        """推送事件给 lumi.py"""
        if self._event_callback:
            try:
                self._event_callback(event_type, data)
            except Exception as e:
                print(f"  [terraria_bridge] 事件回调异常: {e}")

    def _push_game_state(self):
        """推送结构化游戏状态快照"""
        if not self._conn or not self._conn.connected:
            return
        state = self._conn.state
        p = state.player

        # 当前目标信息
        current_goal = ""
        goal_reason = ""
        # (由外部循环维护)

        # 附近玩家
        nearby_player_names = [pl.get("name", "?") for pl in state.nearby_players] if state.nearby_players else []

        # 时间描述
        if state.day_time:
            time_desc = "白天"
        else:
            time_desc = "夜晚"

        # 武器信息
        weapon_name = "空手"
        for h in state.hotbar:
            if h.get("slot", -1) == 0 and h.get("damage", 0) > 0:
                weapon_name = f"{h.get('name', '?')} (伤害={h.get('damage', 0)})"

        snap = {
            "hp": p.hp, "max_hp": p.max_hp,
            "defense": 0,
            "breath": p.breath, "breath_max": p.breath_max,
            "current_goal": getattr(self, '_current_goal_text', '无'),
            "goal_reason": getattr(self, '_current_goal_reason', ''),
            "weapon": weapon_name,
            "pickaxe": "未知",
            "location": f"({p.tile_x}, {p.tile_y})",
            "time_of_day": time_desc,
            "nearby_enemies": len([n for n in state.nearby_npcs
                                   if n.get("damage", 0) > 0 and n.get("life", 0) > 0]),
            "nearby_players": nearby_player_names,
            "is_dead": getattr(self, '_is_dead', False),
            "death_message": getattr(self, '_last_death_message', ''),
        }

        # 从 hotbar 提取装备信息
        for h in state.hotbar:
            s = h.get("slot", -1)
            if s == 1:  # 镐
                snap["pickaxe"] = f"{h.get('name', '?')} (镐力={h.get('pick', 0)})"

        # 防御力
        inv = self._engine.get_inventory() if self._engine else []
        for item in inv:
            if item.get("defense", 0) > 0 and item.get("slot", 99) >= 50:
                snap["defense"] += item.get("defense", 0)

        # 背包摘要
        key_items = []
        for h in state.hotbar:
            name = h.get("name", "")
            stack = h.get("stack", 1)
            if name:
                key_items.append(f"{name} x{stack}" if stack > 1 else name)
        snap["inventory_summary"] = ", ".join(key_items[:8]) if key_items else "空"

        self._push_event("game_state", snap)
        self._last_state_push = time.time()

    def _loop_wrapper(self):
        """survival_loop 线程入口，带独立日志文件 + 错误摘要"""
        import os, sys
        from datetime import datetime
        from terraria_bot import ErrorSummaryWriter, TeeWriterWithSummary

        os.makedirs("logs", exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_path = os.path.join("logs", f"terraria_lumi_{timestamp}.log")
        summary_path = os.path.join("logs", f"terraria_lumi_{timestamp}_errors.log")
        log_file = open(log_path, "w", encoding="utf-8")
        summary = ErrorSummaryWriter(summary_path)

        # 劫持当前线程的 stdout/stderr → 同时写文件+错误摘要
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = TeeWriterWithSummary(old_stdout, log_file, summary)
        sys.stderr = TeeWriterWithSummary(old_stderr, log_file, summary)

        try:
            self._run_survival_loop()
        except Exception as e:
            self._push_event("game_event", {"text": f"生存循环异常退出: {e}"})
            print(f"  [terraria_bridge] 生存循环异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.running = False
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            log_file.close()
            summary.close()
            print(f"  [terraria_bridge] 日志: {log_path}")
            print(f"  [terraria_bridge] 错误摘要: {summary_path}")

    def _run_survival_loop(self):
        """从 terraria_bot.survival_loop 改造，用外部 goal_queue + 事件回调"""
        from terraria_bot import _is_hostile, StrategicGoal

        conn = self._conn
        engine = self._engine
        runner = self._runner

        print("\n" + "=" * 50)
        print("  泰拉瑞亚·Lumi 模式")
        print("=" * 50)

        # --- 初始化 ---
        conn.set_auto_mode(True)
        print("  [初始化] auto_mode 已开启")
        time.sleep(0.2)

        # 等待玩家进入世界（和独立版 terraria_bot 一致）
        print("  [初始化] 等待玩家进入游戏世界...")
        wait_start = time.time()
        while self.running and conn.connected:
            conn.get_state()
            time.sleep(0.5)
            if conn.state.player.hp > 0:
                break
            elapsed = time.time() - wait_start
            if elapsed > 120:
                print("  [初始化] 等待超时(120s)，玩家未进入世界")
                self._push_event("game_event", {"text": "等待玩家进入世界超时"})
                return
            if int(elapsed) % 10 == 0 and int(elapsed) > 0:
                print(f"  [初始化] 仍在等待... ({int(elapsed)}s)")

        if not self.running or not conn.connected:
            return

        p = conn.state.player
        print(f"  [初始化] 玩家已就绪: {p.name} HP={p.hp}/{p.max_hp} pos=({p.tile_x},{p.tile_y})")

        if not runner.init_base():
            self._push_event("game_event", {"text": "初始化基地失败"})
            return

        engine.equip_wings()
        runner.auto_equip()

        # --- 注册 C# combat event 回调 → 转发给 Lumi ---
        self._last_death_message = ""  # 保存最近的死亡描述（供场景分类用）
        self._is_dead = False          # 死亡状态标志

        def _on_combat_event(msg):
            if msg.get("type") != "event":
                return
            evt = msg.get("event", "")
            data = msg.get("data", {})
            if evt == "player_hurt":
                dmg = data.get("damage", 0)
                hp = data.get("hp", 0)
                max_hp = data.get("maxHp", 100)
                source = data.get("source", "未知")
                self._push_event("game_event", {
                    "event": "player_hurt",
                    "text": f"被{source}打了一下，掉了{dmg}点血（剩余{hp}/{max_hp}）",
                })
            elif evt == "player_died":
                death_msg = data.get("deathMessage", "")
                self._last_death_message = death_msg
                self._is_dead = True
                self._push_event("game_event", {
                    "event": "player_died",
                    "text": f"死了！「{death_msg}」" if death_msg else "死了！",
                })
            elif evt == "npc_killed":
                name = data.get("name", "未知")
                boss = data.get("boss", False)
                if boss:
                    self._push_event("game_event", {
                        "event": "boss_killed",
                        "text": f"击败了Boss {name}！",
                    })
                # 普通怪击杀由 fight_nearest_enemy 处产生更详细的事件

        conn.on_message(_on_combat_event)

        # 状态追踪
        current_goal = None
        completed_goals = []
        recent_actions = []
        patrol_dir = 1
        patrol_range = 20
        last_explore_time = time.time()
        explore_cooldown = 180
        patrol_cycles = 0

        # 跟随逻辑（迟滞带防反复横跳）
        FOLLOW_TRIGGER_DIST = 60   # 超过此距离开始跟随（格）
        FOLLOW_STOP_DIST = 15     # 回到此距离内停止跟随（格）
        COMBAT_MAX_DIST_MP = 100   # 联机模式下，只打距宿主这么远内的怪
        MINE_MAX_DIST_HOST = 35    # 联机挖矿时离宿主最远距离（格）
        MINE_SCAN_INTERVAL = 5     # 每隔几个巡逻周期扫描一次矿石
        UPGRADE_CHECK_INTERVAL = 30  # 每隔多少巡逻周期检查装备升级
        RESUPPLY_CHECK_INTERVAL = 15  # 每隔多少巡逻周期检查背包/补给
        following_host = False      # 当前是否在跟随状态
        follow_nav_fails = 0       # 跟随导航连续失败次数
        multiplayer_mode = False    # 联机模式（检测到其他玩家时自动开启）
        last_torch_x = 0           # 上次放置火把的 x 坐标

        # 等待首个目标的宽限期（给慢脑时间响应）
        waiting_first_goal = True
        first_goal_deadline = time.time() + 30  # 最多等 30 秒

        # 推送首次状态
        self._push_game_state()
        self._push_event("game_event", {
            "text": f"[状态报告] {runner.build_status_summary()}"
        })
        recent_actions.append("游戏连接成功，等待目标")

        print("\n=== 进入 Lumi 生存循环 ===")

        while self.running and conn.connected:
            conn.get_state()
            time.sleep(1.0)

            hp = conn.state.player.hp
            max_hp = conn.state.player.max_hp
            hp_ratio = hp / max_hp if max_hp > 0 else 1.0

            # 坐标合理性检查：tile_x 为 0 说明状态未刷新，跳过本轮
            if conn.state.player.tile_x == 0 and conn.state.player.tile_y == 0:
                continue

            # --- 死亡后复活检测 ---
            if self._is_dead and hp > 0:
                self._is_dead = False
                self._push_event("game_event", {
                    "event": "respawned",
                    "text": "复活了！回到了出生点",
                })
                # 重置当前目标，复活后重新规划
                current_goal = None
                self._current_goal_text = "无"
                self._current_goal_reason = ""

            # --- 定期推送状态 (每30秒) ---
            if time.time() - self._last_state_push > 30:
                self._push_game_state()

            # --- Priority 0: 自保 ---
            if hp_ratio < 0.5:
                runner._auto_heal_check()

                if hp_ratio < 0.3:
                    enemies = [npc for npc in conn.state.nearby_npcs
                               if _is_hostile(npc) and npc.get("life", 0) > 0]
                    if enemies:
                        nearest = min(enemies, key=lambda e: abs(e["x"] - conn.state.player.x))
                        flee_dir = -1 if nearest["x"] > conn.state.player.x else 1
                        self._push_event("game_event", {"text": f"血量危险({hp}/{max_hp})，赶紧跑！"})
                        flee_x = conn.state.player.tile_x + flee_dir * 5
                        engine.nav_to(flee_x, timeout=3)
                        continue

            # --- Priority 0.1: 溺水逃离 ---
            breath = conn.state.player.breath
            breath_max = conn.state.player.breath_max
            if breath < breath_max * 0.5:
                # 呼吸值低于一半，赶紧往上走
                my_tx = conn.state.player.tile_x
                my_ty = conn.state.player.tile_y
                escape_y = my_ty - 10  # 向上逃离10格
                print(f"  [溺水] 呼吸值过低 ({breath}/{breath_max})，紧急上浮!")
                self._push_event("game_event", {"text": "水好深，快上去！"})
                engine.nav_to(my_tx, target_y=escape_y, allow_dig=True, timeout=8)
                continue

            # --- 联机模式检测 ---
            host_players = conn.state.nearby_players
            if host_players:
                if not multiplayer_mode:
                    multiplayer_mode = True
                    self.multiplayer_mode = True
                    print("  [模式] 检测到其他玩家，切换为联机模式（跟随优先）")
                host = host_players[0]
                host_tx = host.get("tileX", 0)
                host_ty = host.get("tileY", 0)
                my_tx = conn.state.player.tile_x
                dist_to_host = abs(host_tx - my_tx)
            else:
                if multiplayer_mode:
                    multiplayer_mode = False
                    self.multiplayer_mode = False
                    following_host = False
                    print("  [模式] 其他玩家离开，切换为单人模式（自由决策）")
                host = None
                host_tx = 0
                host_ty = 0
                dist_to_host = 0

            # --- Priority 0.5: 联机模式跟随（最高优先级，仅次于自保）---
            if multiplayer_mode and host:
                if following_host:
                    if dist_to_host <= FOLLOW_STOP_DIST:
                        following_host = False
                        follow_nav_fails = 0
                        print(f"  [跟随] 已靠近宿主 (距离={dist_to_host})，恢复任务")
                    else:
                        move_dir = 1 if host_tx > my_tx else -1
                        # 导航到宿主旁边，允许挖掘穿墙
                        target_x = host_tx - move_dir * 3
                        arrived = engine.nav_to(target_x, target_y=host_ty + 2,
                                                allow_dig=True, timeout=10)
                        if not arrived:
                            follow_nav_fails += 1
                            if follow_nav_fails >= 2:
                                # 导航连续失败，改用直接移动
                                print(f"  [跟随] 导航失败{follow_nav_fails}次，直走靠近")
                                conn.move("right" if move_dir > 0 else "left")
                                time.sleep(2.0)
                                conn.move("stop")
                                follow_nav_fails = 0
                        else:
                            follow_nav_fails = 0
                        continue
                else:
                    if dist_to_host > FOLLOW_TRIGGER_DIST:
                        following_host = True
                        follow_nav_fails = 0
                        self._push_event("game_event", {
                            "text": f"发现同伴走远了，赶紧跟上去"
                        })
                        print(f"  [跟随] 触发跟随 (距离={dist_to_host})")
                        continue

            # --- Priority 1: 打怪 ---
            if hp_ratio >= 0.5:
                enemies = [npc for npc in conn.state.nearby_npcs
                           if _is_hostile(npc) and npc.get("life", 0) > 0]
                # 联机模式下，只打宿主附近的怪（避免追怪跑太远）
                if multiplayer_mode and host:
                    enemies = [e for e in enemies
                               if abs(e["x"] // 16 - host_tx) < COMBAT_MAX_DIST_MP]
                if enemies:
                    # 记录最近的怪物名（用于事件描述）
                    nearest_enemy_name = enemies[0].get("name", "怪物")
                    runner.unhold_torch()  # 战斗前切回武器
                    killed = engine.fight_nearest_enemy(timeout=8)
                    if killed:
                        engine.collect_nearby_items()
                        self._push_event("game_event", {
                            "event": "enemy_killed",
                            "text": f"打死了{nearest_enemy_name}！"
                        })
                    runner._ensure_light()  # 战斗后恢复火把
                    if killed:
                        continue

            # --- 检查目标中断 ---
            if self.goal_interrupt.is_set():
                self.goal_interrupt.clear()
                if current_goal is not None:
                    self._push_event("game_event", {
                        "text": f"目标被中断: {current_goal.goal_type} → {current_goal.target}"
                    })
                    current_goal = None

            # --- Priority 2: 检查新目标 ---
            if current_goal is None:
                try:
                    new_goal = self.goal_queue.get_nowait()
                    # 联机模式下跳过远距离目标（探索、长距离移动）
                    skip_types = {"explore", "go_to"}
                    if multiplayer_mode and new_goal.goal_type in skip_types:
                        print(f"  [联机] 跳过远距离目标: {new_goal.goal_type} → {new_goal.target}")
                        self._push_event("game_event", {
                            "text": f"现在跟同伴在一起，先不去远处冒险了"
                        })
                    else:
                        current_goal = new_goal
                        waiting_first_goal = False
                        self._current_goal_text = f"{new_goal.goal_type}: {new_goal.target}"
                        self._current_goal_reason = new_goal.reason
                        goal_label = {"gather": "去收集", "craft": "去合成", "explore": "去探索",
                                       "go_to": "去", "boss_prep": "准备打Boss"}.get(new_goal.goal_type, "开始")
                        self._push_event("game_event", {
                            "text": f"决定{goal_label} {new_goal.target}（{new_goal.reason}）"
                        })
                        self._push_game_state()
                        print(f"\n  [Lumi生存] 收到新目标: {new_goal.goal_type} → {new_goal.target}")
                except queue.Empty:
                    pass

            # --- Priority 3: 执行当前目标 ---
            if current_goal is not None:
                success, result_desc = runner.execute_goal(current_goal)
                if not conn.connected:
                    break

                goal_desc = f"{current_goal.goal_type}:{current_goal.target}"
                if success:
                    completed_goals.append(goal_desc)
                    recent_actions.append(f"完成目标: {goal_desc}")
                    self._push_event("game_event", {
                        "text": f"搞定了！{current_goal.target} 完成"
                    })
                    print(f"\n  [Lumi生存] 目标完成: {result_desc}")
                else:
                    recent_actions.append(f"目标失败: {goal_desc} — {result_desc}")
                    self._push_event("game_event", {
                        "text": f"{current_goal.target} 没搞成（{result_desc}）"
                    })
                    print(f"\n  [Lumi生存] 目标失败: {result_desc}")

                current_goal = None
                self._current_goal_text = "无"
                self._current_goal_reason = ""

                # 推送状态报告，触发慢脑决策
                summary = runner.build_status_summary(recent_actions, completed_goals)
                self._push_event("game_event", {"text": f"[状态报告] {summary}"})
                self._push_game_state()
                continue

            # --- Priority 4: Fallback ---
            # 等待首个目标时不巡逻（给慢脑时间响应）
            if waiting_first_goal:
                if time.time() < first_goal_deadline:
                    time.sleep(1)
                    continue
                else:
                    waiting_first_goal = False
                    print("  [Lumi生存] 首个目标等待超时，进入 Fallback 模式")

            patrol_cycles += 1

            if patrol_cycles % 10 == 0:
                runner.auto_equip()
                if runner.count_empty_slots() <= 5:
                    runner.clean_inventory()

            # --- 联机模式 Fallback：在宿主周围巡逻 + 顺手挖矿/开箱/放火把 ---
            if multiplayer_mode and host:
                # 亮度检查：暗处自动手持/放置火把
                runner._ensure_light()
                px = conn.state.player.tile_x
                py = conn.state.player.tile_y

                # --- 定期检查：背包满→回家补给 ---
                if patrol_cycles % RESUPPLY_CHECK_INTERVAL == 0 and patrol_cycles > 0:
                    if runner._inventory_nearly_full(threshold=5):
                        print("  [联机] 背包快满了，回家存东西补给")
                        self._push_event("game_event", {
                            "text": "背包快满了，先回家存一下东西再回来"
                        })
                        runner.unhold_torch()
                        runner.go_home_and_resupply()
                        self._push_event("game_event", {
                            "text": "补给完毕，回来继续跟上"
                        })
                        # 回家后传送回宿主身边（用传送会回出生点，需要走回去）
                        # 跟随逻辑会自动把 Lumi 拉回宿主身边
                        continue

                # --- 定期检查：装备升级 ---
                if patrol_cycles % UPGRADE_CHECK_INTERVAL == 0 and patrol_cycles > 0:
                    upgraded = runner.check_upgrade()
                    if upgraded:
                        self._push_event("game_event", {
                            "text": "合成了更好的装备！"
                        })

                # 如果跟宿主垂直距离太大（>15格），说明不在同一层，挖过去
                dy_to_host = abs(py - host_ty)
                if dy_to_host > 15:
                    arrived = engine.nav_to(host_tx, target_y=host_ty + 2,
                                            allow_dig=True, timeout=12)
                    if not arrived:
                        move_dir = 1 if host_tx > px else -1
                        conn.move("right" if move_dir > 0 else "left")
                        time.sleep(2.0)
                        conn.move("stop")
                else:
                    # 同一层：每隔几轮扫描周围，挖矿/开箱/放火把
                    did_something = False
                    if patrol_cycles % MINE_SCAN_INTERVAL == 0:
                        scan = runner.scan_surroundings(radius=15)
                        ores = scan.get("ores", [])
                        chests = scan.get("chests", [])

                        # 优先开宝箱（比挖矿更有价值）
                        for chest_x, chest_y in chests:
                            chest_dist_host = abs(chest_x - host_tx) + abs(chest_y - host_ty)
                            if chest_dist_host > MINE_MAX_DIST_HOST:
                                continue
                            # 排除基地箱子
                            if runner.base_x and abs(chest_x - runner.base_x) < 30:
                                continue
                            print(f"  [联机] 发现宝箱 @({chest_x},{chest_y})，去开！")
                            self._push_event("game_event", {
                                "text": "发现一个宝箱！去看看里面有什么"
                            })
                            runner.unhold_torch()
                            looted = runner.loot_nearby_chest(chest_x, chest_y)
                            if looted:
                                self._push_event("game_event", {
                                    "text": "开箱成功，看看捡到了什么好东西"
                                })
                            runner._ensure_light()
                            did_something = True
                            break

                        # 其次挖矿
                        if not did_something:
                            for ore_x, ore_y, ore_type, ore_name in ores:
                                ore_dist_host = abs(ore_x - host_tx) + abs(ore_y - host_ty)
                                if ore_dist_host > MINE_MAX_DIST_HOST:
                                    continue
                                print(f"  [联机挖矿] 发现{ore_name} @({ore_x},{ore_y})，去挖")
                                self._push_event("game_event", {
                                    "text": f"咦，旁边有{ore_name}，挖一下"
                                })
                                runner.unhold_torch()
                                mined = runner.mine_ore_vein(ore_x, ore_y, ore_type)
                                if mined > 0:
                                    self._push_event("game_event", {
                                        "text": f"挖到了{mined}块{ore_name}"
                                    })
                                    engine.collect_nearby_items()
                                runner._ensure_light()
                                did_something = True
                                break

                    if not did_something:
                        # 正常巡逻 + 放火把
                        last_torch_x = runner.maybe_place_torch(last_torch_x,
                                                                 underground=(py > 400))
                        mp_patrol_range = 40
                        if patrol_dir == 1 and px >= host_tx + mp_patrol_range:
                            patrol_dir = -1
                        elif patrol_dir == -1 and px <= host_tx - mp_patrol_range:
                            patrol_dir = 1
                        target_x = px + patrol_dir * 8
                        arrived = engine.nav_to(target_x, timeout=5)
                        if not arrived:
                            patrol_dir = -patrol_dir
                continue

            # --- 单人模式 Fallback：自主探索 + 基地巡逻 ---
            if time.time() - last_explore_time > explore_cooldown and hp_ratio >= 0.8:
                self._push_event("game_event", {"text": "闲着没事，去地下逛逛看看有什么好东西"})
                explore_dir = patrol_dir if patrol_dir is not None else 1
                runner.explore_underground(direction=explore_dir, max_time=300)
                last_explore_time = time.time()
                if not conn.connected:
                    break
                runner.go_home_and_resupply()
                recent_actions.append(f"Fallback探索 (方向={'右' if explore_dir > 0 else '左'})")

                summary = runner.build_status_summary(recent_actions, completed_goals)
                self._push_event("game_event", {"text": f"[状态报告] {summary}"})
                self._push_game_state()
                continue

            # 基地巡逻
            px = conn.state.player.tile_x
            home_x = runner.base_x or px
            if home_x == 0:
                continue  # 坐标异常，跳过
            if patrol_dir == 1 and px >= home_x + patrol_range:
                patrol_dir = -1
            elif patrol_dir == -1 and px <= home_x - patrol_range:
                patrol_dir = 1
            target_x = px + patrol_dir * 5
            engine.nav_to(target_x, timeout=5)

        # 关闭 auto mode，归还控制权
        try:
            conn.set_auto_mode(False)
        except Exception:
            pass
        print("  [Lumi生存] 生存循环结束")


# ==================== 独立测试入口 ====================

if __name__ == "__main__":
    print("=== terraria_bridge 独立测试 ===")

    events = []
    def test_callback(event_type, data):
        events.append((event_type, data))
        print(f"  [事件] {event_type}: {data}")

    bridge = TerrariaBridge(event_callback=test_callback)
    try:
        bridge.start()
        print("连接成功，按 Ctrl+C 退出")
        while bridge.running:
            time.sleep(1)
    except ConnectionError as e:
        print(f"连接失败: {e}")
    except KeyboardInterrupt:
        print("\n收到中断")
    finally:
        bridge.stop()
        print(f"共收到 {len(events)} 个事件")
