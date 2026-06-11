"""
Kingdom Rush AI Bot - TCP 客户端
连接到游戏内注入的 BridgeMod TCP 服务端

用法:
    python kingdom_rush_bot.py              # 交互模式
    python kingdom_rush_bot.py --watch      # 观察模式
    python kingdom_rush_bot.py --explore    # 深度探索
"""

import socket
import json
import time
import sys
import argparse
from datetime import datetime

HOST = "127.0.0.1"
PORT = 9878


class KingdomRushBot:
    def __init__(self):
        self.sock = None
        self.connected = False
        self.recv_buffer = ""
        self.running = True
        self.game_state = {}
        self.message_count = 0
        self.host = HOST
        self.port = PORT

    def connect(self, retries=30, interval=2, quiet=False):
        for i in range(retries):
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.connect((self.host, self.port))
                self.sock.settimeout(0.1)
                self.connected = True
                print(f"[连接成功] {self.host}:{self.port}")
                return True
            except ConnectionRefusedError:
                if not quiet:
                    if i == 0:
                        print(f"等待游戏启动... (端口 {self.port})")
                    print(f"  重试 {i+1}/{retries}...", end='\r')
                time.sleep(interval)
            except Exception as e:
                if not quiet:
                    print(f"连接错误: {e}")
                time.sleep(interval)
        if not quiet:
            print("\n连接超时，请确认游戏已启动且 BridgeMod 已注入")
        return False

    def send(self, data):
        if not self.connected:
            return
        try:
            payload = json.dumps(data, ensure_ascii=False) + "\n"
            self.sock.sendall(payload.encode('utf-8'))
        except Exception as e:
            print(f"发送错误: {e}")
            self.connected = False

    def receive(self, timeout=5.0):
        start = time.time()
        while time.time() - start < timeout:
            if '\n' in self.recv_buffer:
                line, self.recv_buffer = self.recv_buffer.split('\n', 1)
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            try:
                data = self.sock.recv(65536).decode('utf-8')
                if not data:
                    self.connected = False
                    return None
                self.recv_buffer += data
            except socket.timeout:
                continue
            except Exception:
                self.connected = False
                return None
        return None

    def send_and_receive(self, data, timeout=5.0):
        self.drain_messages()
        self.send(data)
        return self.receive(timeout)

    def drain_messages(self, duration=0.3):
        end_time = time.time() + duration
        messages = []
        while time.time() < end_time:
            msg = self.receive(timeout=0.1)
            if msg:
                messages.append(msg)
            else:
                break
        return messages

    # ========== 探测命令 ==========

    def ping(self):
        result = self.send_and_receive({"action": "ping"})
        if result and result.get("type") == "pong":
            print("[PONG] Bridge 正常响应")
            return True
        print("[ERROR] 无响应")
        return False

    def find_game_objects(self):
        result = self.send_and_receive({"action": "find_game"})
        if result and result.get("type") == "game_objects":
            print("\n=== 游戏核心对象 ===")
            for name, type_name in sorted(result["data"].items()):
                print(f"  {name}: {type_name}")
            return result["data"]
        return None

    def inspect_globals(self):
        result = self.send_and_receive({"action": "inspect_globals"})
        if result and result.get("type") == "globals":
            print("\n=== 全局变量 ===")
            by_type = {}
            for name, type_name in sorted(result["data"].items()):
                by_type.setdefault(type_name, []).append(name)
            for type_name in ['table', 'function', 'number', 'string', 'boolean', 'userdata']:
                names = by_type.get(type_name, [])
                if names:
                    print(f"\n  [{type_name}] ({len(names)})")
                    for name in sorted(names):
                        print(f"    {name}")
            return result["data"]
        return None

    def inspect(self, path, depth=2):
        result = self.send_and_receive({"action": "inspect", "path": path, "depth": depth})
        if result and result.get("type") == "inspect_result":
            if "error" in result:
                print(f"\n  [{path}] 未找到")
                return None
            print(f"\n=== {path} ({result.get('value_type', '?')}) ===")
            self._print_tree(result["data"], indent=2)
            return result["data"]
        return None

    def eval_code(self, code):
        result = self.send_and_receive({"action": "eval", "code": code})
        if result:
            if result.get("type") == "eval_result":
                print(f"  结果: {result.get('result', 'nil')}")
            elif result.get("type") == "eval_error":
                print(f"  错误: {result.get('error', '?')}")
            return result
        return None

    def get_state(self):
        result = self.send_and_receive({"action": "get_state"})
        if result and result.get("type") == "game_state":
            self._print_game_state(result)
            return result
        return None

    def _print_game_state(self, state):
        print(f"\n=== {state.get('level_name', '?')} (Lv{state.get('level_idx', '?')}) ===")
        print(f"  金币: {state.get('gold', '?')}  生命: {state.get('lives', '?')}  波次: {state.get('wave', '?')}/{state.get('wave_total', '?')}")
        paused = state.get('paused', False)
        finished = state.get('waves_finished', False)
        flags = []
        if paused: flags.append("暂停中")
        if finished: flags.append("已完成")
        if flags:
            print(f"  状态: {', '.join(flags)}")

        towers = state.get('towers', [])
        if towers:
            print(f"\n  塔 ({len(towers)}):")
            for t in towers:
                rng = f" 射程={t.get('range', '?')}" if t.get('range') else ""
                print(f"    [{t['id']}] {t.get('template', '?')} @ ({t.get('x', '?')},{t.get('y', '?')}) spent={t.get('spent', 0)}{rng}")

        holders = state.get('holders', [])
        empty = [h for h in holders if not h.get('blocked')]
        blocked = [h for h in holders if h.get('blocked')]
        if empty:
            print(f"\n  空塔位 ({len(empty)}):")
            for h in empty:
                print(f"    [{h['id']}] mesh={h.get('mesh_id', '?')} @ ({h.get('x', '?')},{h.get('y', '?')})")
        if blocked:
            print(f"\n  锁定塔位 ({len(blocked)}):")
            for h in blocked:
                print(f"    [{h['id']}] mesh={h.get('mesh_id', '?')} 解锁={h.get('unblock_price', '?')}金")

        enemies = state.get('enemies', [])
        if enemies:
            print(f"\n  敌人 ({len(enemies)}):")
            for e in sorted(enemies, key=lambda x: x.get('path_ni', 0), reverse=True)[:10]:
                print(f"    [{e['id']}] {e.get('template', '?')} HP={e.get('hp', '?')}/{e.get('hp_max', '?')} @ ({e.get('x', '?'):.0f},{e.get('y', '?'):.0f})")
            if len(enemies) > 10:
                print(f"    ... 还有 {len(enemies) - 10} 个")

        heroes = state.get('heroes', [])
        if heroes:
            print(f"\n  英雄 ({len(heroes)}):")
            for h in heroes:
                dead = " [阵亡]" if h.get('dead') else ""
                print(f"    [{h['id']}] {h.get('template', '?')} Lv{h.get('level', '?')} HP={h.get('hp', '?')}/{h.get('hp_max', '?')}{dead}")

    # ========== 游戏操作 ==========

    def build_tower(self, holder_id, tower_type):
        """建塔: holder_id=塔位实体id, tower_type=archer/barrack/mage/engineer"""
        result = self.send_and_receive({
            "action": "build_tower",
            "holder_id": int(holder_id),
            "tower_type": tower_type
        })
        if result:
            if result.get("type") == "ok":
                print(f"  建塔成功: {tower_type} 花费={result.get('cost')} 余额={result.get('gold')}")
            else:
                print(f"  建塔失败: {result.get('message', '?')}")
        return result

    def sell_tower(self, tower_id):
        """卖塔: tower_id=塔实体id"""
        result = self.send_and_receive({
            "action": "sell_tower",
            "tower_id": int(tower_id)
        })
        if result:
            if result.get("type") == "ok":
                print(f"  卖塔成功: 退款={result.get('refund')} 余额={result.get('gold')}")
            else:
                print(f"  卖塔失败: {result.get('message', '?')}")
        return result

    def upgrade_tower(self, tower_id, target):
        """升级塔: tower_id=塔实体id, target=目标模板名"""
        result = self.send_and_receive({
            "action": "upgrade_tower",
            "tower_id": int(tower_id),
            "target": target
        })
        if result:
            if result.get("type") == "ok":
                print(f"  升级成功: {target} 花费={result.get('cost')} 余额={result.get('gold')}")
            else:
                print(f"  升级失败: {result.get('message', '?')}")
        return result

    def send_wave(self):
        """提前出波"""
        result = self.send_and_receive({"action": "send_wave"})
        if result:
            if result.get("type") == "ok":
                print(f"  出波: 当前第 {result.get('wave', '?')} 波")
            else:
                print(f"  出波失败: {result.get('message', '?')}")
        return result

    def use_power(self, power, x, y):
        """释放技能: power=1(火雨)/2(增援), x,y=目标坐标"""
        result = self.send_and_receive({
            "action": "use_power",
            "power": int(power),
            "x": float(x),
            "y": float(y)
        })
        # CD/locked 时静默，成功时由 AI 层打印
        return result

    def move_hero(self, x, y, hero_id=None):
        """移动英雄到指定位置"""
        cmd = {"action": "move_hero", "x": float(x), "y": float(y)}
        if hero_id:
            cmd["hero_id"] = int(hero_id)
        result = self.send_and_receive(cmd)
        if result:
            if result.get("type") == "ok":
                print(f"  英雄移动到 ({x}, {y})")
            else:
                print(f"  移动失败: {result.get('message', '?')}")
        return result

    def set_rally_point(self, tower_id, x, y):
        """设置兵营集结点"""
        result = self.send_and_receive({
            "action": "set_rally_point",
            "tower_id": int(tower_id),
            "x": float(x),
            "y": float(y),
        })
        if result:
            if result.get("type") == "ok":
                ids = result.get("soldier_ids", [])
                moved = result.get("moved", 0)
                skipped = result.get("skipped", 0)
                print(f"  集结点 塔{tower_id} → ({x:.0f},{y:.0f}) ids={ids} moved={moved} skip={skipped}")
            else:
                print(f"  集结点失败: {result.get('message', '?')}")
        return result

    def get_path_points(self, x, y, radius):
        """获取指定范围内的路径点（用于集结点选择等）"""
        return self.send_and_receive({
            "action": "get_path_points",
            "x": float(x),
            "y": float(y),
            "radius": float(radius),
        })

    def dump_barrack(self):
        """诊断：转储所有兵营的 barrack 组件结构"""
        return self.send_and_receive({"action": "dump_barrack"})

    def restart_level(self):
        """关卡内重试"""
        result = self.send_and_receive({"action": "restart_level"})
        if result:
            if result.get("type") == "ok":
                print(f"  重试关卡")
            else:
                print(f"  重试失败: {result.get('message', '?')}")
        return result

    def go_to_map(self):
        """关卡结束后返回地图"""
        result = self.send_and_receive({"action": "go_to_map"})
        if result:
            if result.get("type") == "ok":
                print(f"  返回地图")
            else:
                print(f"  返回失败: {result.get('message', '?')}")
        return result

    def load_slot(self, slot=1):
        """在主菜单选择存档槽进入地图"""
        result = self.send_and_receive({"action": "load_slot", "slot": int(slot)})
        if result:
            if result.get("type") == "ok":
                print(f"  加载存档槽 {slot}")
            else:
                print(f"  加载失败: {result.get('message', '?')}")
        return result

    def detect_screen(self):
        """检测当前界面状态（主菜单/地图/关卡内）"""
        result = self.send_and_receive({"action": "detect_screen"})
        if result and result.get("type") == "screen_info":
            print(f"\n=== 界面检测 ===")
            for k, v in sorted(result.items()):
                if k != "type":
                    print(f"  {k}: {v}")
        return result

    def start_level(self, level_idx, level_mode=1):
        """在地图界面启动关卡 (mode: 1=normal, 2=iron, 3=heroic)"""
        result = self.send_and_receive({
            "action": "start_level",
            "level_idx": int(level_idx),
            "level_mode": int(level_mode),
        })
        if result:
            if result.get("type") == "ok":
                print(f"  启动关卡 {level_idx} 模式 {level_mode}")
            else:
                print(f"  启动失败: {result.get('message', '?')}")
        return result

    def get_level_list(self):
        """获取关卡列表和星级信息"""
        result = self.send_and_receive({"action": "get_level_list"})
        if result and result.get("type") == "level_list":
            levels = result.get("levels", [])
            print(f"\n=== 关卡列表 ({result.get('count', 0)} 关) ===")
            for lv in levels:
                stars = "★" * lv.get("stars", 0) + "☆" * (3 - lv.get("stars", 0))
                name = lv.get("name", f"Level {lv['idx']}")
                print(f"  [{lv['idx']:2d}] {stars} {name}")
        return result

    def dump_waves(self):
        """探测波次数据结构"""
        result = self.send_and_receive({"action": "dump_waves"}, timeout=10.0)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return result

    # ========== UI 辅助 ==========

    def _print_tree(self, data, indent=0):
        if isinstance(data, dict):
            for k, v in sorted(data.items()):
                if isinstance(v, dict):
                    print(f"{'  ' * indent}{k}:")
                    self._print_tree(v, indent + 1)
                else:
                    print(f"{'  ' * indent}{k}: {v}")
        else:
            print(f"{'  ' * indent}{data}")

    # ========== 观察模式 ==========

    def watch_loop(self):
        print("\n[观察模式] 按 Ctrl+C 退出")
        self.send_and_receive({"action": "set_push_interval", "interval": 1.0})

        while self.running and self.connected:
            msg = self.receive(timeout=2.0)
            if msg:
                self.message_count += 1
                if msg.get("type") == "game_state":
                    self.game_state = msg
                    gold = msg.get("gold", "?")
                    lives = msg.get("lives", "?")
                    wave = msg.get("wave", "?")
                    total = msg.get("wave_total", "?")
                    enemies = msg.get("enemy_count", 0)
                    towers = msg.get("tower_count", 0)
                    print(f"\r  [#{self.message_count}] 金:{gold} 命:{lives} 波:{wave}/{total} 塔:{towers} 怪:{enemies}   ", end='')

    # ========== 深度探索 ==========

    def deep_explore(self):
        print("\n" + "=" * 60)
        print("  Kingdom Rush 内部结构探索")
        print("=" * 60)

        print("\n[步骤 1] 查找游戏核心对象...")
        self.find_game_objects()

        print("\n[步骤 2] 获取完整游戏状态...")
        self.get_state()

        print("\n" + "=" * 60)
        print("  探索完成！用 state/inspect/eval 继续探索")
        print("=" * 60)

    # ========== 交互模式 ==========

    def interactive(self):
        print("\n[交互模式] 可用命令:")
        print("  --- 查询 ---")
        print("  state                     - 获取完整游戏状态")
        print("  ping                      - 心跳测试")
        print("  inspect <path> [depth]    - 探索对象")
        print("  eval <lua code>           - 执行 Lua 代码")
        print("  globals                   - 列出全局变量")
        print("  --- 操作 ---")
        print("  build <holder_id> <type>  - 建塔 (type: archer/barrack/mage/engineer)")
        print("  sell <tower_id>           - 卖塔")
        print("  upgrade <tower_id> <tgt>  - 升级 (tgt: tower_archer_2, tower_ranger 等)")
        print("  wave                      - 提前出波")
        print("  hero <x> <y>              - 移动英雄")
        print("  power <1|2> <x> <y>       - 技能 (1=火雨 2=增援)")
        print("  --- 其他 ---")
        print("  watch                     - 观察模式")
        print("  explore                   - 深度探索")
        print("  quit                      - 退出")
        print()

        while self.running and self.connected:
            try:
                line = input("KR> ").strip()
                if not line:
                    continue

                parts = line.split(None, 1)
                cmd = parts[0].lower()
                rest = parts[1] if len(parts) > 1 else ""

                if cmd in ('quit', 'exit', 'q'):
                    break
                elif cmd == 'ping':
                    self.ping()
                elif cmd == 'state':
                    self.get_state()
                elif cmd == 'find':
                    self.find_game_objects()
                elif cmd == 'globals':
                    self.inspect_globals()
                elif cmd == 'inspect':
                    if rest:
                        args = rest.split()
                        path = args[0]
                        depth = int(args[1]) if len(args) > 1 else 2
                        self.inspect(path, depth)
                    else:
                        print("用法: inspect <path> [depth]")
                elif cmd == 'eval':
                    if rest:
                        self.eval_code(rest)
                    else:
                        print("用法: eval <lua code>")
                elif cmd == 'build':
                    args = rest.split()
                    if len(args) >= 2:
                        try:
                            self.build_tower(args[0], args[1])
                        except ValueError:
                            print("holder_id 必须是数字")
                    else:
                        print("用法: build <holder_id> <tower_type>")
                        print("  tower_type: archer, barrack, mage, engineer")
                elif cmd == 'sell':
                    if rest:
                        try:
                            self.sell_tower(rest.strip())
                        except ValueError:
                            print("tower_id 必须是数字")
                    else:
                        print("用法: sell <tower_id>")
                elif cmd == 'upgrade':
                    args = rest.split()
                    if len(args) >= 2:
                        try:
                            self.upgrade_tower(args[0], args[1])
                        except ValueError:
                            print("tower_id 必须是数字")
                    else:
                        print("用法: upgrade <tower_id> <target_template>")
                        print("  例: upgrade 25 tower_archer_2")
                elif cmd == 'wave':
                    self.send_wave()
                elif cmd == 'hero':
                    args = rest.split()
                    if len(args) >= 2:
                        try:
                            hero_id = int(args[2]) if len(args) > 2 else None
                            self.move_hero(args[0], args[1], hero_id)
                        except ValueError:
                            print("坐标必须是数字")
                    else:
                        print("用法: hero <x> <y> [hero_id]")
                elif cmd == 'power':
                    args = rest.split()
                    if len(args) >= 3:
                        try:
                            self.use_power(args[0], args[1], args[2])
                        except ValueError:
                            print("参数必须是数字")
                    else:
                        print("用法: power <1|2> <x> <y>")
                        print("  1=火雨  2=增援")
                elif cmd == 'waves':
                    self.dump_waves()
                elif cmd == 'dump_enemy':
                    result = self.send_and_receive({"action": "dump_enemy"}, timeout=5.0)
                    if result:
                        print(json.dumps(result, indent=2, ensure_ascii=False))
                elif cmd == 'watch':
                    self.watch_loop()
                elif cmd == 'explore':
                    self.deep_explore()
                else:
                    print(f"未知命令: {cmd}")

            except KeyboardInterrupt:
                print()
                break
            except EOFError:
                break

    def close(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        print("\n[断开连接]")


def main():
    parser = argparse.ArgumentParser(description='Kingdom Rush AI Bot')
    parser.add_argument('--watch', action='store_true', help='观察模式')
    parser.add_argument('--explore', action='store_true', help='深度探索')
    parser.add_argument('--host', default=HOST, help=f'服务器地址 (默认: {HOST})')
    parser.add_argument('--port', type=int, default=PORT, help=f'端口 (默认: {PORT})')
    args = parser.parse_args()

    bot = KingdomRushBot()
    bot.host = args.host
    bot.port = args.port

    try:
        if not bot.connect():
            sys.exit(1)

        welcome = bot.receive(timeout=2.0)
        if welcome:
            print(f"  游戏: {welcome.get('game', '?')}")
            print(f"  版本: {welcome.get('version', '?')}")
            cmds = welcome.get('commands', [])
            if cmds:
                print(f"  支持命令: {', '.join(cmds)}")

        if args.explore:
            bot.deep_explore()
        elif args.watch:
            bot.watch_loop()
        else:
            bot.ping()
            bot.interactive()

    except KeyboardInterrupt:
        pass
    finally:
        bot.close()


if __name__ == '__main__':
    main()
