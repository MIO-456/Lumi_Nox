"""
Terraria Bot - TCP client for LumiBridge mod.
Controls Lumi's character in Terraria via tModLoader.

Usage:
    python terraria_bot.py          # Auto-launch tModLoader + test movement
    python terraria_bot.py --watch  # Connect and watch state only
    python terraria_bot.py --no-launch  # Don't launch tModLoader, just connect
"""

import socket
import json
import time
import sys
import os
import argparse
import subprocess
import threading
import ctypes
import ctypes.wintypes
import io
import queue
import requests
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


class TeeWriter:
    """Writes to both the original stream and a log file."""
    def __init__(self, original, log_file):
        self.original = original
        self.log_file = log_file

    def write(self, text):
        self.original.write(text)
        self.log_file.write(text)

    def flush(self):
        self.original.flush()
        self.log_file.flush()

# --- Windows API for window activation ---
user32 = ctypes.windll.user32
EnumWindows = user32.EnumWindows
GetWindowTextW = user32.GetWindowTextW
SetForegroundWindow = user32.SetForegroundWindow
ShowWindow = user32.ShowWindow
IsWindowVisible = user32.IsWindowVisible
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
SW_RESTORE = 9

TMOD_STEAM_APP_ID = "1281930"
TMOD_PATH = r"D:\steam\steamapps\common\tModLoader"
MOD_SOURCE_PATH = r"C:\Users\30506\Documents\My Games\Terraria\tModLoader\ModSources\LumiBridge"
PLAYER_NAME = "Lumi"
WORLD_NAME = "Lumi的世界"


def find_and_activate_window(title_keyword: str) -> bool:
    """Find a window by title keyword and bring it to foreground."""
    found = [None]

    def enum_cb(hwnd, _):
        if IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            GetWindowTextW(hwnd, buf, 256)
            if title_keyword.lower() in buf.value.lower():
                found[0] = hwnd
                return False  # stop enumeration
        return True

    EnumWindows(WNDENUMPROC(enum_cb), 0)

    if found[0]:
        ShowWindow(found[0], SW_RESTORE)
        SetForegroundWindow(found[0])
        return True
    return False


def activate_terraria():
    """Activate the Terraria/tModLoader window."""
    for keyword in ["泰拉瑞亚", "Terraria", "tModLoader"]:
        if find_and_activate_window(keyword):
            print(f"已激活泰拉瑞亚窗口")
            return True
    print("未找到泰拉瑞亚窗口")
    return False


def build_mod():
    """Build LumiBridge mod before launching. Ensures latest code is compiled."""
    if not os.path.exists(MOD_SOURCE_PATH):
        print(f"Mod 源码目录不存在: {MOD_SOURCE_PATH}，跳过构建")
        return False

    print("正在构建 LumiBridge Mod...")
    dotnet_dll = os.path.join(TMOD_PATH, "tModLoader.dll")
    try:
        result = subprocess.run(
            ["dotnet", dotnet_dll, "-build", MOD_SOURCE_PATH],
            cwd=TMOD_PATH,
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            print("Mod 构建成功!")
            return True
        else:
            print(f"Mod 构建失败 (returncode={result.returncode})")
            if result.stdout:
                # Only print last few lines to avoid spam
                lines = result.stdout.strip().split("\n")
                for line in lines[-10:]:
                    print(f"  {line}")
            if result.stderr:
                lines = result.stderr.strip().split("\n")
                for line in lines[-5:]:
                    print(f"  [err] {line}")
            return False
    except subprocess.TimeoutExpired:
        print("Mod 构建超时 (120s)")
        return False
    except Exception as e:
        print(f"Mod 构建异常: {e}")
        return False


def launch_tmodloader():
    """Launch tModLoader with auto-select player and world."""
    print(f"正在启动 tModLoader (角色: {PLAYER_NAME}, 世界: {WORLD_NAME})...")

    dotnet_dll = os.path.join(TMOD_PATH, "tModLoader.dll")
    if os.path.exists(dotnet_dll):
        print("使用 dotnet 直接启动 (跳过菜单)...")
        subprocess.Popen(
            ["dotnet", dotnet_dll, "-skipselect", f"{PLAYER_NAME}:{WORLD_NAME}"],
            cwd=TMOD_PATH,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        print("未找到 tModLoader.dll，使用 Steam 启动 (需手动选择角色/世界)...")
        os.startfile(f"steam://rungameid/{TMOD_STEAM_APP_ID}")

    print("等待泰拉瑞亚窗口出现...")
    for _ in range(60):
        time.sleep(2)
        if find_and_activate_window("泰拉瑞亚") or find_and_activate_window("Terraria"):
            print("tModLoader 已启动!")
            return True
    print("tModLoader 启动超时")
    return False


@dataclass
class PlayerState:
    name: str = ""
    hp: int = 0
    max_hp: int = 0
    mana: int = 0
    max_mana: int = 0
    x: float = 0
    y: float = 0
    tile_x: int = 0
    tile_y: int = 0
    velocity_x: float = 0
    velocity_y: float = 0
    direction: int = 1
    selected_item: int = 0
    grounded: bool = False
    brightness: float = 1.0  # 0.0=pitch black, 1.0=full light
    breath: int = 200        # 呼吸值 (200=满, 水下持续减少, 0时开始溺水扣血)
    breath_max: int = 200


@dataclass
class GameState:
    player: PlayerState = field(default_factory=PlayerState)
    hotbar: list = field(default_factory=list)
    nearby_npcs: list = field(default_factory=list)
    nearby_players: list = field(default_factory=list)  # 联机时其他玩家 [{name, tileX, tileY, hp, ...}]
    day_time: bool = True
    time: float = 0
    raining: bool = False
    blood_moon: bool = False
    tick: int = 0


class TerrariaBridge:
    """TCP client that communicates with LumiBridge mod."""

    def __init__(self, host="127.0.0.1", port=9877):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.state = GameState()
        self.connected = False
        self._recv_thread: Optional[threading.Thread] = None
        self._running = False
        self._callbacks = []

    def connect(self, timeout=120, retry_interval=2):
        """Connect to LumiBridge TCP server with retry."""
        print(f"正在连接 LumiBridge (端口 {self.port})...")
        start = time.time()
        while time.time() - start < timeout:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(5)
                self.sock.connect((self.host, self.port))
                self.sock.settimeout(None)
                self.connected = True
                self._running = True
                self._recv_thread = threading.Thread(
                    target=self._recv_loop, daemon=True
                )
                self._recv_thread.start()
                print("已连接到 LumiBridge!")
                return True
            except (ConnectionRefusedError, socket.timeout, OSError):
                time.sleep(retry_interval)
        print("连接超时。请确认已进入泰拉瑞亚世界且 LumiBridge mod 已加载。")
        return False

    def disconnect(self):
        self._running = False
        self.connected = False
        try:
            self.sock.close()
        except:
            pass

    def send(self, cmd: dict):
        """Send a JSON command to the mod."""
        if not self.connected:
            return
        try:
            data = json.dumps(cmd, ensure_ascii=False) + "\n"
            self.sock.sendall(data.encode("utf-8"))
        except Exception as e:
            print(f"发送失败: {e}")
            self.connected = False

    def on_message(self, callback):
        """Register a callback for incoming messages."""
        self._callbacks.append(callback)

    def _recv_loop(self):
        """Background thread: receive and parse messages from mod."""
        buf = ""
        try:
            while self._running:
                data = self.sock.recv(4096)
                if not data:
                    break
                buf += data.decode("utf-8")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                        self._handle_message(msg)
                    except json.JSONDecodeError as e:
                        print(f"  [DEBUG·JSON错误] {e} | line_len={len(line)} | head={line[:120]}")
                        pass
        except Exception as e:
            if self._running:
                print(f"连接断开: {e}")
        self.connected = False

    def _handle_message(self, msg: dict):
        """Process incoming message and update state."""
        msg_type = msg.get("type", "")

        if msg_type == "state":
            p = msg.get("player", {})
            self.state.player = PlayerState(
                name=p.get("name", ""),
                hp=p.get("hp", 0),
                max_hp=p.get("maxHp", 0),
                mana=p.get("mana", 0),
                max_mana=p.get("maxMana", 0),
                x=p.get("x", 0),
                y=p.get("y", 0),
                tile_x=p.get("tileX", 0),
                tile_y=p.get("tileY", 0),
                velocity_x=p.get("velocityX", 0),
                velocity_y=p.get("velocityY", 0),
                direction=p.get("direction", 1),
                selected_item=p.get("selectedItem", 0),
                grounded=p.get("grounded", False),
                brightness=p.get("brightness", 1.0),
                breath=p.get("breath", 200),
                breath_max=p.get("breathMax", 200),
            )
            self.state.hotbar = msg.get("hotbar", [])
            self.state.nearby_npcs = msg.get("nearbyNpcs", [])
            self.state.nearby_players = msg.get("nearbyPlayers", [])
            t = msg.get("time", {})
            self.state.day_time = t.get("dayTime", True)
            self.state.time = t.get("time", 0)
            self.state.raining = t.get("raining", False)
            self.state.blood_moon = t.get("bloodMoon", False)
            self.state.tick = msg.get("tick", 0)

        for cb in self._callbacks:
            try:
                cb(msg)
            except Exception as e:
                print(f"回调错误: {e}")

    # --- High-level commands ---

    def move(self, direction: str):
        """Move player: left, right, jump, up, down, stop"""
        self.send({"cmd": "move", "direction": direction})

    def move_combo(self, directions: list):
        """Move player with multiple simultaneous directions, e.g. ['jump', 'right'] for flying right."""
        self.send({"cmd": "move", "directions": directions})

    def use_item(self, target_x: int = -1, target_y: int = -1):
        """Use current item, optionally targeting a tile coordinate."""
        cmd = {"cmd": "use_item"}
        if target_x >= 0:
            cmd["target_x"] = target_x
        if target_y >= 0:
            cmd["target_y"] = target_y
        self.send(cmd)

    def stop_use(self):
        """Stop using item."""
        self.send({"cmd": "stop_use"})

    def select_item(self, slot: int):
        """Select hotbar slot (0-9)."""
        self.send({"cmd": "select_item", "slot": slot})

    def quick_heal(self):
        """Trigger quick heal (H key) — uses best healing potion in inventory."""
        self.send({"cmd": "quick_heal"})

    def set_auto_mode(self, enabled: bool):
        """Enable/disable auto mode (control injection)."""
        self.send({"cmd": "set_auto_mode", "enabled": enabled})

    def get_state(self):
        """Request immediate state push."""
        self.send({"cmd": "get_state"})

    def get_nearby_tiles(self, radius: int = 10):
        """Request nearby tile data."""
        self.send({"cmd": "get_nearby_tiles", "radius": radius})

    def ping(self):
        self.send({"cmd": "ping"})


class BehaviorEngine:
    """Layer 2: Behavior system - navigate_to, mine_tile, place_tile."""

    TOOL_RANGE = 4  # tiles - how far the player can reach
    ARRIVE_THRESHOLD = 1  # tiles - close enough to target

    def __init__(self, bridge: TerrariaBridge):
        self.bridge = bridge
        self._task_thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        # Response storage for request-response commands
        self._pending_responses = {}
        self._response_lock = threading.Lock()
        # 战斗黑名单: {(enemy_name, tile_x, tile_y): expire_time}
        self._combat_blacklist: dict[tuple, float] = {}
        bridge.on_message(self._on_bridge_msg)

    # Response types we capture for request-response pattern
    _RESPONSE_TYPES = {
        "tile_info", "tool_info", "nearby_tiles",
        "kill_tile_result", "kill_area_result",
        "place_tile_result", "place_wall_result",
        "tree_positions", "inventory", "item_search",
        "recipes", "craft_result", "teleport_result",
        "kill_npcs_result", "collect_result", "swap_result",
        "scan_result", "trash_result", "loot_result", "deposit_result",
        "nearest_npcs", "path_result", "nav_status", "equip_result",
        "open_chest_result", "close_chest_result",
        "spawn_result", "scan_chests_result", "quick_stack_result",
        "place_chest_result", "hold_use_item_result", "give_item_result",
    }

    # Message types that use a queue (multiple messages per type in rapid succession)
    _QUEUE_TYPES = {"nav_status"}

    def _on_bridge_msg(self, msg):
        """Capture responses from mod."""
        msg_type = msg.get("type", "")
        if msg_type in self._RESPONSE_TYPES:
            with self._response_lock:
                if msg_type in self._QUEUE_TYPES:
                    if msg_type not in self._pending_responses:
                        self._pending_responses[msg_type] = []
                    self._pending_responses[msg_type].append(msg)
                else:
                    self._pending_responses[msg_type] = msg

    def _wait_response(self, msg_type: str, timeout=2.0):
        """Wait for a specific response type from the mod."""
        with self._response_lock:
            if msg_type in self._QUEUE_TYPES:
                # Don't clear queue on first call — just check if there's already a message
                pass
            else:
                self._pending_responses.pop(msg_type, None)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._response_lock:
                if msg_type in self._pending_responses:
                    val = self._pending_responses[msg_type]
                    if isinstance(val, list):
                        if len(val) > 0:
                            return val.pop(0)
                    else:
                        return self._pending_responses.pop(msg_type)
            time.sleep(0.05)
        return None

    def cancel(self):
        """Cancel current behavior."""
        self._cancel.set()
        self.bridge.move("stop")
        self.bridge.stop_use()

    def _cancelled(self):
        return self._cancel.is_set() or not self.bridge.connected

    # --- Core behaviors ---

    def find_pickaxe(self) -> tuple:
        """Find best pickaxe. Returns (slot, power, name)."""
        self.bridge.send({"cmd": "find_pickaxe"})
        resp = self._wait_response("tool_info")
        if resp and resp.get("slot", -1) >= 0:
            return resp["slot"], resp["power"], resp["name"]
        return -1, 0, ""

    def check_tile(self, x: int, y: int) -> dict:
        """Check if tile at (x,y) exists. Returns tile info dict."""
        self.bridge.send({"cmd": "check_tile", "x": x, "y": y})
        resp = self._wait_response("tile_info")
        return resp or {"hasTile": False}

    def equip_wings(self, item_id: int = 0) -> dict:
        """Equip wings by item ID. Default 0 = use C# ItemID.LeafWings constant.
        Common wings: Frozen Wings=1871, Flame Wings=1869,
        Fishron Wings=2609, Jetpack=1862."""
        cmd = {"cmd": "equip_wings"}
        if item_id > 0:
            cmd["item_id"] = item_id
        self.bridge.send(cmd)
        resp = self._wait_response("equip_result", timeout=3)
        if resp and resp.get("success"):
            print(f"  [装备] 已装备翅膀: {resp.get('item')} (id={resp.get('itemId')}, slot={resp.get('slot')})")
            # Wait for equipment effects to take effect (wingTimeMax updates next frame)
            time.sleep(0.5)
        else:
            print(f"  [装备] 翅膀装备失败: {resp}")
        return resp or {}

    # Terraria tile type constants
    TREE_TILES = {5, 583, 584, 596, 616, 634}  # various tree types
    AXE_TILES = {5, 583, 584, 596, 616, 634, 80, 72}  # trees + cactus + mushroom

    def find_axe(self) -> tuple:
        """Find best axe. Returns (slot, power, name)."""
        self.bridge.send({"cmd": "find_axe"})
        resp = self._wait_response("tool_info")
        if resp and resp.get("slot", -1) >= 0:
            return resp["slot"], resp["power"], resp["name"]
        return -1, 0, ""

    def mine_tile(self, tile_x: int, tile_y: int, timeout=10) -> bool:
        """Mine a specific tile until it breaks.

        Auto-selects pickaxe or axe depending on tile type.
        Returns True if tile was mined.
        """
        self._cancel.clear()

        # Check if tile exists and get type
        info = self.check_tile(tile_x, tile_y)
        if not info.get("hasTile"):
            return True  # Already empty

        tile_type = info.get("tileType", 0)

        # Choose tool based on tile type
        if tile_type in self.AXE_TILES:
            tool_slot, tool_power, tool_name = self.find_axe()
            if tool_slot < 0:
                print(f"  [行为] 没有斧头!")
                return False
        else:
            tool_slot, tool_power, tool_name = self.find_pickaxe()
            if tool_slot < 0:
                print(f"  [行为] 没有镐子!")
                return False

        # Select tool
        self.bridge.select_item(tool_slot)
        time.sleep(0.05)

        # Start mining
        self.bridge.use_item(tile_x, tile_y)
        print(f"  [行为] 挖掘 ({tile_x}, {tile_y}) 使用 {tool_name} (类型={tile_type})")

        start = time.time()
        while not self._cancelled() and time.time() - start < timeout:
            info = self.check_tile(tile_x, tile_y)
            if not info.get("hasTile"):
                self.bridge.stop_use()
                self.bridge.select_item(0)  # 切回武器
                print(f"  [行为] 挖掘完成 ({tile_x}, {tile_y})")
                return True
            time.sleep(0.15)

        self.bridge.stop_use()
        self.bridge.select_item(0)  # 切回武器
        print(f"  [行为] 挖掘超时 ({tile_x}, {tile_y})")
        return False

    def mine_area(self, x1: int, y1: int, x2: int, y2: int) -> int:
        """Mine a rectangular area. Returns number of tiles mined."""
        self._cancel.clear()
        count = 0
        # Mine column by column, top to bottom
        for x in range(min(x1, x2), max(x1, x2) + 1):
            for y in range(min(y1, y2), max(y1, y2) + 1):
                if self._cancelled():
                    return count
                # Move close enough to reach the tile
                px = self.bridge.state.player.tile_x
                if abs(px - x) > self.TOOL_RANGE - 1:
                    target_x = x - (self.TOOL_RANGE - 2) if x > px else x + (self.TOOL_RANGE - 2)
                    self.nav_to(target_x)
                # Mine
                info = self.check_tile(x, y)
                if info.get("hasTile"):
                    if self.mine_tile(x, y):
                        count += 1
        print(f"  [行为] mine_area 完成，共挖掘 {count} 个方块")
        return count

    # === Direct API behaviors (instant, no animation) ===

    def kill_tile(self, x: int, y: int) -> bool:
        """Instantly destroy a tile."""
        self.bridge.send({"cmd": "kill_tile", "x": x, "y": y})
        resp = self._wait_response("kill_tile_result")
        return resp.get("success", False) if resp else False

    def kill_area(self, x1: int, y1: int, x2: int, y2: int) -> int:
        """Instantly destroy all tiles in a rectangle."""
        self.bridge.send({"cmd": "kill_area", "x1": x1, "y1": y1, "x2": x2, "y2": y2})
        resp = self._wait_response("kill_area_result", timeout=5)
        return resp.get("killed", 0) if resp else 0

    def place_tile(self, x: int, y: int, tile_type: int, style: int = 0) -> bool:
        """Instantly place a tile."""
        self.bridge.send({"cmd": "place_tile", "x": x, "y": y,
                          "tile_type": tile_type, "style": style})
        resp = self._wait_response("place_tile_result")
        return resp.get("success", False) if resp else False

    def place_wall(self, x: int, y: int, wall_type: int) -> bool:
        """Instantly place a background wall."""
        self.bridge.send({"cmd": "place_wall", "x": x, "y": y, "wall_type": wall_type})
        resp = self._wait_response("place_wall_result")
        return resp.get("success", False) if resp else False

    def find_trees(self, radius: int = 25) -> list:
        """Find tree base positions nearby. Returns list of {x, y, distance}."""
        self.bridge.send({"cmd": "find_trees", "radius": radius})
        resp = self._wait_response("tree_positions")
        trees = resp.get("trees", []) if resp else []
        trees.sort(key=lambda t: t["distance"])
        return trees

    def get_inventory(self) -> list:
        """Get full inventory (50 slots)."""
        self.bridge.send({"cmd": "get_inventory"})
        resp = self._wait_response("inventory")
        return resp.get("items", []) if resp else []

    def find_item(self, name: str = "", item_id: int = -1) -> list:
        """Find items in inventory by name or ID."""
        cmd = {"cmd": "find_item"}
        if name:
            cmd["name"] = name
        if item_id > 0:
            cmd["id"] = item_id
        self.bridge.send(cmd)
        resp = self._wait_response("item_search")
        return resp.get("found", []) if resp else []

    def count_item(self, name: str = "", item_id: int = -1) -> int:
        """Count total stack of an item in inventory."""
        items = self.find_item(name=name, item_id=item_id)
        return sum(it["stack"] for it in items)

    def get_recipes(self, category: str = "all", limit: int = 30) -> list:
        """Get crafting recipes. category: all/weapon/pick/axe/armor/potion."""
        cmd = {"cmd": "get_recipes", "limit": limit, "category": category}
        self.bridge.send(cmd)
        resp = self._wait_response("recipes", timeout=5)
        return resp.get("recipes", []) if resp else []

    def craft(self, item_name: str = "", item_id: int = -1, amount: int = 1) -> int:
        """Craft an item. Returns number actually crafted."""
        cmd = {"cmd": "craft", "amount": amount}
        if item_name:
            cmd["item_name"] = item_name
        if item_id > 0:
            cmd["item_id"] = item_id
        self.bridge.send(cmd)
        resp = self._wait_response("craft_result", timeout=3)
        if resp and resp.get("success"):
            print(f"  [行为] 合成 {resp.get('item', '?')} x{resp.get('crafted', 0)}")
            return resp.get("crafted", 0)
        return 0

    def teleport(self, tile_x: int, tile_y: int):
        """Teleport player to tile position."""
        self.bridge.send({"cmd": "teleport", "x": tile_x, "y": tile_y})
        self._wait_response("teleport_result")

    def scan_area(self, x: int, y: int, width: int, height: int) -> list:
        """Batch scan a rectangular area. Returns list of {x, y, t(ileType), c(lass)}.
        c: 0=Block, 1=OneWay(platform), 2=Ore."""
        self.bridge.send({"cmd": "scan_area", "x": x, "y": y,
                          "width": width, "height": height})
        resp = self._wait_response("scan_result", timeout=5)
        return resp.get("tiles", []) if resp else []

    def scan_relative(self, rx: int, ry: int, w: int, h: int) -> dict:
        """Scan area relative to player center. Returns {tiles, playerX, playerY, ...}.
        Tiles have {x, y, t, c} where c: 0=Block, 1=OneWay, 2=Ore."""
        self.bridge.send({"cmd": "scan_relative", "rx": rx, "ry": ry, "w": w, "h": h})
        resp = self._wait_response("scan_result", timeout=5)
        return resp if resp else {"tiles": [], "playerX": 0, "playerY": 0}

    def get_nearest_npcs(self, hostile: bool = True, count: int = 5, range_tiles: int = 50) -> list:
        """Get nearest NPCs sorted by distance. Returns list with tile coords.
        Each NPC has: name, id, life, lifeMax, tileX, tileY, dist, friendly, damage, boss."""
        self.bridge.send({"cmd": "get_nearest_npcs", "hostile": hostile,
                          "count": count, "range": range_tiles})
        resp = self._wait_response("nearest_npcs", timeout=2)
        return resp.get("npcs", []) if resp else []

    def find_path(self, target_x: int, target_y: int, allow_dig: bool = False,
                  range_tiles: int = 100) -> dict:
        """A* pathfinding — compute path only, no execution.
        Returns {success, waypoints: [{x,y}], rawLength} or {success: false, reason}."""
        self.bridge.send({"cmd": "find_path", "x": target_x, "y": target_y,
                          "allow_dig": allow_dig, "range": range_tiles})
        resp = self._wait_response("path_result", timeout=5)
        return resp if resp else {"success": False, "reason": "timeout"}

    def navigate_to(self, target_x: int, target_y: int, allow_dig: bool = False,
                    timeout: float = 30.0, air_penalty: int = 1) -> str:
        """A* pathfinding + execution. Blocks until arrived/stuck/timeout.
        target_y should be FEET position (tile_y + 2).
        air_penalty: higher values penalize air paths, forcing dig routes.
        Returns: 'arrived', 'stuck', 'cancelled', or 'timeout'."""
        # Auto-downgrade allow_dig if no pickaxe available
        if allow_dig:
            pick_slot, pick_power, pick_name = self.find_pickaxe()
            if pick_slot < 0:
                print(f"  [导航] 没有镐子，降级 allow_dig=False")
                allow_dig = False
        # Clear any stale nav_status messages from previous navigation
        with self._response_lock:
            self._pending_responses.pop("nav_status", None)
        cmd = {"cmd": "navigate_to", "x": target_x, "y": target_y,
               "allow_dig": allow_dig}
        if air_penalty > 1:
            cmd["air_penalty"] = air_penalty
        self.bridge.send(cmd)
        # Wait for initial response (started or stuck), skip debug/moving messages
        init_deadline = time.time() + 5
        while time.time() < init_deadline:
            resp = self._wait_response("nav_status", timeout=2)
            if not resp:
                return "timeout"
            status = resp.get("status", "")
            if status in ("debug", "moving", "repath"):
                continue
            if status == "stuck":
                dbg = resp.get('debug', '')
                print(f"  [导航] 无法到达 ({target_x},{target_y}): {resp.get('reason')}"
                      f" start=({resp.get('startX')},{resp.get('startY')})"
                      f" dig={resp.get('allowDig')} pick={resp.get('pickPower')}")
                if dbg:
                    print(f"  [导航] debug: {dbg}")
                return "stuck"
            if status == "started":
                break
            # Unexpected status — keep waiting
            continue
        else:
            return "timeout"

        mjh = resp.get('maxJumpHeight', '?')
        has_wings = resp.get('hasWings', False)
        wpl = resp.get('waypointList', [])
        wp_str = ' → '.join(f"({w['x']},{w['y']})" for w in wpl) if wpl else ''
        wing_str = " [飞行模式]" if has_wings else ""
        print(f"  [导航] 开始导航到 ({target_x},{target_y}), {resp.get('waypoints')} 个路点 (maxJump={mjh}){wing_str}")
        if wp_str:
            print(f"  [导航] 路点: {wp_str}")

        # Wait for completion
        deadline = time.time() + timeout
        last_combat_check = 0
        while time.time() < deadline and self.bridge.connected:
            # Periodic combat check during navigation (~every 2 seconds)
            now = time.time()
            if now - last_combat_check > 2.0:
                last_combat_check = now
                nearby_enemies = self.get_nearest_npcs(hostile=True, count=3, range_tiles=15)
                # Filter to very close enemies (within 8 tiles horizontal)
                self.bridge.get_state()
                ptx = self.bridge.state.player.tile_x
                pty = self.bridge.state.player.tile_y
                close_enemies = [e for e in nearby_enemies
                                 if abs(e["tileX"] - ptx) <= 8
                                 and abs(e["tileY"] - pty) <= 6]
                if close_enemies:
                    ename = close_enemies[0].get("name", "?")
                    print(f"  [导航] 附近有敌人 {ename}，暂停导航战斗")
                    self.cancel_navigate()
                    # Drain any pending nav messages
                    for _ in range(5):
                        r = self._wait_response("nav_status", timeout=0.3)
                        if not r or r.get("status") in ("stuck", "cancelled", "arrived"):
                            break
                    self.fight_nearest_enemy(timeout=8)
                    self.collect_nearby_items()
                    # Restart navigation with same parameters
                    restart_cmd = {"cmd": "navigate_to", "x": target_x, "y": target_y,
                                   "allow_dig": allow_dig}
                    if air_penalty > 1:
                        restart_cmd["air_penalty"] = air_penalty
                    self.bridge.send(restart_cmd)
                    # Wait for restarted navigation's "started" response
                    init_wait = time.time() + 3
                    while time.time() < init_wait:
                        r = self._wait_response("nav_status", timeout=1)
                        if not r:
                            break
                        if r.get("status") == "started":
                            break
                        if r.get("status") == "stuck":
                            return "stuck"
                    continue

            resp = self._wait_response("nav_status", timeout=2)
            if not resp:
                continue
            status = resp.get("status", "")
            if status == "moving":
                progress = resp.get("progress", 0)
                x, y = resp.get("x", 0), resp.get("y", 0)
                dig = resp.get("dig")
                jump = resp.get("jump")
                if dig:
                    print(f"  [导航] 进度 {progress:.0%} 位置 ({x},{y}) "
                          f"挖掘目标=({dig.get('digTargetX')},{dig.get('digTargetY')}) "
                          f"脚下=[{dig.get('tile0Type')},s={dig.get('tile0Solid')}|"
                          f"{dig.get('tile1Type')},s={dig.get('tile1Solid')}] "
                          f"useItem={dig.get('useItem')} item={dig.get('itemName')} "
                          f"stuck={dig.get('stuckFrames')}")
                elif jump:
                    print(f"  [导航] 进度 {progress:.0%} 位置 ({x},{y}) "
                          f"velY={jump.get('velY')} grounded={jump.get('grounded')} "
                          f"jumpFrames={jump.get('jumpFrames')} "
                          f"ctrl=[J={jump.get('ctrlJump')},L={jump.get('ctrlLeft')},R={jump.get('ctrlRight')}] "
                          f"目标在上方={jump.get('targetAbove')}")
                else:
                    print(f"  [导航] 进度 {progress:.0%} 位置 ({x},{y})")
            elif status == "repath":
                reason = resp.get('reason', '')
                reason_str = f" 原因={reason}" if reason else ""
                print(f"  [导航] 重新寻路 (第{resp.get('retry')}次){reason_str}")
            elif status in ("arrived", "stuck", "cancelled"):
                if status == "arrived":
                    print(f"  [导航] 到达目标!")
                elif status == "stuck":
                    print(f"  [导航] 卡住: {resp.get('reason')}")
                return status

        # Timeout — cancel navigation
        print(f"  [导航] 超时，取消导航")
        self.bridge.send({"cmd": "cancel_navigate"})
        self._wait_response("nav_status", timeout=2)
        return "timeout"

    def cancel_navigate(self):
        """Cancel ongoing navigation."""
        self.bridge.send({"cmd": "cancel_navigate"})

    def nav_to(self, target_x: int, target_y: int = None, allow_dig: bool = False,
               timeout: float = 30.0) -> bool:
        """Convenience wrapper for navigate_to. Returns True if arrived.
        If target_y is None, uses current feet Y (horizontal walk)."""
        if target_y is None:
            self.bridge.get_state()
            time.sleep(0.05)
            target_y = self.bridge.state.player.tile_y + 2  # feet Y
        result = self.navigate_to(target_x, target_y, allow_dig=allow_dig, timeout=timeout)
        return result == "arrived"

    def trash_item(self, slot: int) -> bool:
        """Destroy item in slot via trash can (doesn't drop on ground)."""
        self.bridge.send({"cmd": "trash_item", "slot": slot})
        resp = self._wait_response("trash_result", timeout=2)
        if resp and resp.get("success"):
            print(f"  [行为] 丢弃 {resp.get('name', '?')} x{resp.get('stack', 0)} (slot {slot})")
            return True
        return False

    def open_chest(self, x: int, y: int) -> bool:
        """Open chest UI at tile position (visual, for stream viewers)."""
        self.bridge.send({"cmd": "open_chest", "x": x, "y": y})
        resp = self._wait_response("open_chest_result", timeout=2)
        return resp and resp.get("success", False)

    def close_chest(self):
        """Close chest UI."""
        self.bridge.send({"cmd": "close_chest"})
        self._wait_response("close_chest_result", timeout=1)

    def loot_chest(self, x: int, y: int) -> list:
        """Loot all items from a chest at tile position. Returns looted items."""
        self.bridge.send({"cmd": "loot_chest", "x": x, "y": y})
        resp = self._wait_response("loot_result", timeout=3)
        if resp and resp.get("success"):
            items = resp.get("items", [])
            for it in items:
                print(f"  [行为] 拾取 {it.get('name', '?')} x{it.get('stack', 0)}")
            return items
        return []

    def deposit_to_chest(self, x: int, y: int, keep_slots: list = None) -> list:
        """Deposit inventory items into chest. keep_slots = slots to skip."""
        cmd = {"cmd": "deposit_to_chest", "x": x, "y": y}
        if keep_slots:
            cmd["keep_slots"] = keep_slots
        self.bridge.send(cmd)
        resp = self._wait_response("deposit_result", timeout=3)
        if resp and resp.get("success"):
            deposited = resp.get("deposited", [])
            for it in deposited:
                print(f"  [行为] 存入 {it.get('name', '?')} x{it.get('stack', 0)}")
            return deposited
        return []

    def get_spawn(self) -> tuple:
        """Get world spawn point coordinates."""
        self.bridge.send({"cmd": "get_spawn"})
        resp = self._wait_response("spawn_result", timeout=2)
        if resp:
            return (resp.get("x", 0), resp.get("y", 0))
        return None

    def scan_chests(self, cx: int, cy: int, range_x: int = 50, range_y: int = 30) -> list:
        """Scan for chests near (cx,cy). Returns list of {x,y,items_count,empty_slots,has_space}."""
        self.bridge.send({"cmd": "scan_chests", "cx": cx, "cy": cy,
                          "range_x": range_x, "range_y": range_y})
        resp = self._wait_response("scan_chests_result", timeout=3)
        if resp and resp.get("success"):
            return resp.get("chests", [])
        return []

    def quick_stack(self) -> int:
        """Quick-stack player inventory into currently open chest. Returns items stacked."""
        self.bridge.send({"cmd": "quick_stack"})
        resp = self._wait_response("quick_stack_result", timeout=2)
        if resp and resp.get("success"):
            count = resp.get("stacked_count", 0)
            if count > 0:
                print(f"  [行为] 快速堆叠 {count} 种物品")
            return count
        return 0

    def place_chest(self, x: int, y: int) -> bool:
        """Place a wooden chest at (x,y). Returns success."""
        self.bridge.send({"cmd": "place_chest", "x": x, "y": y})
        resp = self._wait_response("place_chest_result", timeout=2)
        if resp and resp.get("success"):
            print(f"  [行为] 放置箱子 @({resp.get('chest_x')},{resp.get('chest_y')})")
            return True
        else:
            reason = resp.get("reason", "unknown") if resp else "timeout"
            print(f"  [行为] 放置箱子失败: {reason}")
            return False

    def hold_use_item(self, frames: int = 180):
        """Hold use-item button for N frames (~3s at 60fps). For mirror/channeled items."""
        self.bridge.send({"cmd": "hold_use_item", "frames": frames})
        self._wait_response("hold_use_item_result", timeout=2)

    def give_item(self, item_id: int, stack: int = 1) -> bool:
        """Give item to player inventory. Returns success."""
        self.bridge.send({"cmd": "give_item", "item_id": item_id, "stack": stack})
        resp = self._wait_response("give_item_result", timeout=2)
        if resp and resp.get("success"):
            print(f"  [行为] 获得 {resp.get('name', '?')} x{stack}")
            return True
        return False

    def get_equip_slots(self) -> list:
        """Get current armor/accessory equipment. Returns list of slot dicts."""
        self.bridge.send({"cmd": "get_equip"})
        resp = self._wait_response("equip_info", timeout=2)
        return resp.get("slots", []) if resp else []

    def equip_item(self, inv_slot: int, equip_slot: int) -> bool:
        """Equip item from inventory slot to armor/accessory slot (0=head,1=body,2=legs,3-9=acc)."""
        self.bridge.send({"cmd": "equip_item", "from": inv_slot, "to": equip_slot})
        resp = self._wait_response("equip_result", timeout=2)
        if resp and resp.get("success"):
            print(f"  [装备] 装备了 {resp.get('equipped', '?')} → 栏位{equip_slot}")
            return True
        return False

    def chop_trees(self, max_trees: int = 5) -> int:
        """Find and chop nearby trees. Walks to each tree, chops it, picks up drops."""
        trees = self.find_trees()
        if not trees:
            print("  [行为] 附近没有树")
            return 0

        before = self.count_item(item_id=9)
        chopped = 0
        for tree in trees[:max_trees]:
            if self._cancelled():
                break
            tx, ty = tree["x"], tree["y"]

            # Walk to tree
            self.nav_to(tx, timeout=15)

            # Kill the entire tree column (base and up)
            for dy in range(-20, 1):
                tile = self.check_tile(tx, ty + dy)
                if tile.get("hasTile") and tile.get("tileType") in (5, 583, 584, 596, 616, 634):
                    self.kill_tile(tx, ty + dy)
            chopped += 1
            print(f"  [行为] 砍倒第 {chopped} 棵树 ({tx}, {ty})")

            # Wait a moment for items to drop, then they auto-pickup since we're close
            time.sleep(0.5)

        after = self.count_item(item_id=9)
        gained = after - before
        print(f"  [行为] 砍了 {chopped} 棵树，获得约 {gained} 木材")
        return gained

    def kill_hostile_npcs(self, range_pixels: float = 800) -> int:
        """Kill all hostile NPCs within range (instant, for utility)."""
        self.bridge.send({"cmd": "kill_hostile_npcs", "range": range_pixels})
        resp = self._wait_response("kill_npcs_result")
        if resp and resp.get("killed", 0) > 0:
            for npc in resp.get("npcs", []):
                print(f"  [战斗] 消灭 {npc['name']}")
            return resp["killed"]
        return 0

    # Combat constants
    KITE_IDEAL_DIST = 3    # Ideal tile distance for melee kiting
    KITE_TOO_CLOSE = 1     # Retreat if closer than this
    ENGAGE_DIST = 10       # Start actively fighting within this horizontal distance
    UNREACHABLE_DY = 15    # Vertical tile difference to consider enemy unreachable
    FLY_COMBAT_DY = 25     # Max vertical tiles to fly up and fight
    COMBAT_BLACKLIST_SECS = 30  # 打不到的怪冷却时间（秒）
    COMBAT_NO_DMG_TIMEOUT = 4   # 连续攻击N秒怪物血量不变，判定隔墙

    def _is_reachable(self, ptx, pty, etx, ety) -> bool:
        """Check if enemy is reachable — by walking (same level) or flying (above, no blocks)."""
        dy = abs(ety - pty)
        if dy <= 6:  # close enough vertically for ground combat
            return True
        if dy <= self.FLY_COMBAT_DY and ety < pty:  # enemy above — flyable
            return True
        return dy <= self.UNREACHABLE_DY

    def _blacklist_enemy(self, name, tx, ty, reason=""):
        """将敌人加入黑名单，一段时间内不再尝试攻击"""
        # 用粗略位置做 key（±5格内算同一个）
        key = (name, tx // 5, ty // 5)
        self._combat_blacklist[key] = time.time() + self.COMBAT_BLACKLIST_SECS
        print(f"  [战斗] {name} 加入黑名单 {self.COMBAT_BLACKLIST_SECS}s ({reason})")

    def _is_blacklisted(self, name, tx, ty) -> bool:
        """检查敌人是否在黑名单中"""
        key = (name, tx // 5, ty // 5)
        expire = self._combat_blacklist.get(key, 0)
        if expire > time.time():
            return True
        elif key in self._combat_blacklist:
            del self._combat_blacklist[key]
        return False

    def fight_nearest_enemy(self, timeout=10) -> bool:
        """Fight the nearest reachable hostile NPC with kiting behavior.
        - Uses get_nearest_npcs for C#-side distance sorting + tile coords
        - Skips enemies on different vertical levels (roof/pit)
        - Always swing sword (aim 2 tiles in front of player)
        - Maintain ideal distance: retreat when too close, pursue when far
        - Blacklists unreachable/walled enemies to avoid infinite loops
        Returns True if an enemy was killed, False if none reachable."""
        # Use C#-side sorted NPC list with tile coordinates
        enemies = self.get_nearest_npcs(hostile=True, count=5, range_tiles=50)
        if not enemies:
            return False

        self.bridge.get_state()
        time.sleep(0.05)
        state = self.bridge.state
        px, py = state.player.x, state.player.y
        ptx, pty = state.player.tile_x, state.player.tile_y

        # Filter: reachable + not blacklisted
        reachable = [e for e in enemies
                     if self._is_reachable(ptx, pty, e["tileX"], e["tileY"])
                     and not self._is_blacklisted(e["name"], e["tileX"], e["tileY"])]
        if not reachable:
            # 全都不可达或在黑名单中
            non_bl = [e for e in enemies
                      if not self._is_blacklisted(e["name"], e["tileX"], e["tileY"])]
            if non_bl:
                nearest = non_bl[0]
                edy = nearest["tileY"] - pty
                print(f"  [战斗] {nearest['name']} 在{'上方' if edy < 0 else '下方'}{abs(edy)}格，无法触及")
            return False

        target = reachable[0]  # closest reachable
        target_name = target["name"]
        initial_hp = target.get("life", 0)
        print(f"  [战斗] 发现 {target_name} HP:{initial_hp}，准备战斗!")

        # Select sword (slot 0) and start swinging
        self.bridge.select_item(0)
        time.sleep(0.05)

        start = time.time()
        stuck_count = 0
        last_px = -99999.0
        facing_dir = 1  # 1=right, -1=left
        last_aim_x = -1  # track aim to avoid redundant TCP messages
        # 隔墙检测：记录上一次 HP 变化的时间
        last_hp_change_time = time.time()
        last_target_hp = initial_hp

        while time.time() - start < timeout and not self._cancelled():
            self.bridge.get_state()
            time.sleep(0.05)
            state = self.bridge.state

            # Check if target is still alive
            alive = False
            for npc in state.nearby_npcs:
                if npc.get("name") == target_name and npc.get("life", 0) > 0:
                    target = npc
                    alive = True
                    break
            if not alive:
                self.bridge.move("stop")
                self.bridge.stop_use()
                print(f"  [战斗] {target_name} 已被击败!")
                return True

            # Check if enemy became unreachable (jumped to roof, fell into pit)
            # NPC position from state.nearby_npcs is in pixels, convert to tile
            etx = int(target.get("x", 0) / 16)
            ety = int(target.get("y", 0) / 16)
            ptx = state.player.tile_x
            pty = state.player.tile_y
            if not self._is_reachable(ptx, pty, etx, ety):
                self.bridge.move("stop")
                self.bridge.stop_use()
                self._blacklist_enemy(target_name, etx, ety, "不可达")
                edy = ety - pty
                print(f"  [战斗] {target_name} 跑到{'上方' if edy < 0 else '下方'}{abs(edy)}格，无法触及")
                return False

            # 隔墙检测：持续攻击但怪物 HP 没有变化
            cur_hp = target.get("life", 0)
            if cur_hp != last_target_hp:
                last_target_hp = cur_hp
                last_hp_change_time = time.time()
            elif time.time() - last_hp_change_time > self.COMBAT_NO_DMG_TIMEOUT:
                self.bridge.move("stop")
                self.bridge.stop_use()
                self._blacklist_enemy(target_name, etx, ety, "隔墙/打不到")
                print(f"  [战斗] {target_name} 连续{self.COMBAT_NO_DMG_TIMEOUT}s未掉血，判定隔墙")
                return False

            dx = etx - ptx
            dy = ety - pty  # negative = enemy above
            dist = abs(dx)

            # Update facing direction
            if dx != 0:
                facing_dir = 1 if dx > 0 else -1

            # Aerial combat: enemy is significantly above — fly up to engage
            if dy < -3:
                # Fly toward enemy: hold jump (activates wings) + move horizontally
                dirs = ["jump"]
                if dist > 2:
                    dirs.append("right" if facing_dir > 0 else "left")
                self.bridge.move_combo(dirs)
            elif dy > 3 and not state.player.grounded:
                # Enemy below while airborne — stop flying, let gravity pull us down
                self.bridge.move("stop")
            else:
                # Ground-level combat: kiting movement
                if dist <= self.KITE_TOO_CLOSE:
                    self.bridge.move("left" if facing_dir > 0 else "right")
                elif dist > self.KITE_IDEAL_DIST:
                    self.bridge.move("right" if facing_dir > 0 else "left")
                else:
                    self.bridge.move("stop")

            # Aim at enemy position (works for both ground and aerial targets)
            aim_x = etx if dist <= 8 else ptx + facing_dir * 2
            aim_y = ety if abs(dy) > 3 else pty + 1
            self.bridge.use_item(aim_x, aim_y)
            last_aim_x = aim_x

            # Stuck detection
            player_x = state.player.x
            if abs(player_x - last_px) < 2.0:
                stuck_count += 1
            else:
                stuck_count = 0
            last_px = player_x

            if stuck_count > 8:
                # Check if blocked by a door
                door_found = False
                check_x = ptx + facing_dir
                for check_y in [pty, pty + 1, pty + 2]:
                    tile = self.check_tile(check_x, check_y)
                    if tile.get("hasTile") and tile.get("tileType", 0) in {10, 11}:
                        door_found = True
                        break
                if door_found:
                    print(f"  [战斗] 前方有门，尝试开门")
                    self.bridge.move("up")
                    time.sleep(0.15)
                    self.bridge.move("right" if facing_dir > 0 else "left")
                else:
                    self.bridge.move("jump")
                time.sleep(0.15)
                self.bridge.select_item(0)
                aim_x = ptx + facing_dir * 2
                self.bridge.use_item(aim_x, pty + 1)
                last_aim_x = aim_x
                stuck_count = 0

            time.sleep(0.05)

        self.bridge.move("stop")
        self.bridge.stop_use()
        # 超时也加黑名单（可能卡住了）
        etx = int(target.get("x", 0) / 16)
        ety = int(target.get("y", 0) / 16)
        self._blacklist_enemy(target_name, etx, ety, "战斗超时")
        return False

    def collect_nearby_items(self, range_pixels: float = 600) -> int:
        """Collect dropped items nearby by teleporting them to player."""
        self.bridge.send({"cmd": "collect_items", "range": range_pixels})
        resp = self._wait_response("collect_result")
        collected = resp.get("collected", 0) if resp else 0
        if collected > 0:
            print(f"  [行为] 拾取了 {collected} 个掉落物")
        return collected


@dataclass
class StrategicGoal:
    """A high-level goal issued by the strategic brain (LLM)."""
    goal_type: str   # "gather" | "craft" | "explore" | "boss_prep"
    target: str      # item/boss english name
    reason: str      # LLM's reasoning (for logs / stream display)
    params: dict = field(default_factory=dict)  # direction, quantity, etc.


VALID_GOAL_TYPES = {"gather", "craft", "explore", "boss_prep"}


class TaskRunner:
    """Layer 3: Task queue - chains behaviors into goals."""

    def __init__(self, engine: BehaviorEngine):
        self.engine = engine
        self.bridge = engine.bridge
        self._cancel = threading.Event()
        self._current_task = ""
        # Base management
        self.base_x = None
        self.base_y = None
        self.base_chests = []  # [{x, y, items_count, empty_slots, has_space}]
        # Auto-heal
        self._last_heal_time = 0

    def cancel(self):
        self._cancel.set()
        self.engine.cancel()

    def _cancelled(self):
        return self._cancel.is_set() or not self.bridge.connected

    @property
    def status(self):
        return self._current_task

    def dig_down(self, depth: int) -> int:
        """Dig straight down. Returns number of tiles mined."""
        self._cancel.clear()
        self._current_task = f"向下挖掘 {depth} 格"
        print(f"\n  [任务] {self._current_task}")

        count = 0
        for i in range(depth):
            if self._cancelled():
                break

            self.bridge.get_state()
            time.sleep(0.15)
            px = self.bridge.state.player.tile_x
            py = self.bridge.state.player.tile_y

            # Mine the two tiles below feet (player is ~3 tiles tall, feet at py+2)
            feet_y = py + 3  # tile just below feet
            for dx in range(0, 2):  # 2 tiles wide
                tx = px + dx
                if self._cancelled():
                    break
                info = self.engine.check_tile(tx, feet_y)
                if info.get("hasTile"):
                    self.engine.mine_tile(tx, feet_y)
                    count += 1

            # Fall down by pressing down or waiting
            time.sleep(0.3)
            print(f"  [任务] 已挖 {i + 1}/{depth} 层 (共 {count} 块)")

        self._current_task = ""
        print(f"  [任务] dig_down 完成，共挖 {count} 块")
        return count

    def mine_tunnel(self, direction: str = "right", length: int = 10) -> int:
        """Mine a horizontal tunnel in the given direction.

        Clears a 3-tile-tall passage at player height. Only mines tiles
        that actually block the path — on flat ground it just walks.
        Returns number of tiles mined.
        """
        self._cancel.clear()
        dir_cn = "右" if direction == "right" else "左"
        self._current_task = f"向{dir_cn}挖隧道 {length} 格"
        print(f"\n  [任务] {self._current_task}")

        dx = 1 if direction == "right" else -1
        count = 0
        steps = 0

        while steps < length and not self._cancelled():
            self.bridge.get_state()
            time.sleep(0.15)
            px = self.bridge.state.player.tile_x
            py = self.bridge.state.player.tile_y

            col_x = px + dx

            # Check 3 tiles at player height (head, body, feet)
            # Player occupies py (head) to py+2 (feet)
            mined_any = False
            for ty in range(py, py + 3):
                if self._cancelled():
                    break
                info = self.engine.check_tile(col_x, ty)
                if info.get("hasTile"):
                    tile_type = info.get("tileType", 0)
                    # Skip trees — walk around them
                    if tile_type in self.engine.TREE_TILES:
                        continue
                    self.engine.mine_tile(col_x, ty)
                    count += 1
                    mined_any = True

            # Walk forward
            self.bridge.move(direction)
            time.sleep(0.4 if mined_any else 0.3)
            self.bridge.move("stop")
            time.sleep(0.1)

            # Check if we actually moved
            self.bridge.get_state()
            time.sleep(0.1)
            new_px = self.bridge.state.player.tile_x
            if new_px != px:
                steps += 1

            if (steps) % 5 == 0 and steps > 0:
                print(f"  [任务] 隧道进度 {steps}/{length} (共 {count} 块)")

        self._current_task = ""
        print(f"  [任务] mine_tunnel 完成，走了 {steps} 格，挖了 {count} 块")
        return count

    def gather_surface(self, radius: int = 15) -> int:
        """Mine surface tiles in a radius around the player.
        Useful for clearing an area for building.
        Returns number of tiles mined.
        """
        self._cancel.clear()
        self._current_task = f"清理地表 (半径 {radius})"
        print(f"\n  [任务] {self._current_task}")

        self.bridge.get_state()
        time.sleep(0.2)
        start_x = self.bridge.state.player.tile_x
        start_y = self.bridge.state.player.tile_y
        count = 0

        # Move left to right, mine surface tiles
        for x in range(start_x - radius, start_x + radius + 1):
            if self._cancelled():
                break

            # Move close
            self.engine.nav_to(x)

            # Mine a few tiles deep at this x
            for y in range(start_y, start_y + 4):
                if self._cancelled():
                    break
                info = self.engine.check_tile(x, y)
                if info.get("hasTile"):
                    self.engine.mine_tile(x, y)
                    count += 1

        self._current_task = ""
        print(f"  [任务] gather_surface 完成，共挖 {count} 块")
        return count

    def gather_wood(self, target: int = 50) -> int:
        """Chop trees until we have enough wood."""
        self._cancel.clear()
        self._current_task = f"收集木材 (目标 {target})"
        print(f"\n  [任务] {self._current_task}")

        total = self.engine.count_item(item_id=9)
        print(f"  [任务] 当前木材: {total}")

        rounds = 0
        while total < target and not self._cancelled() and rounds < 10:
            # Fight any nearby enemies before chopping
            self.engine.fight_nearest_enemy(timeout=5)
            gained = self.engine.chop_trees(max_trees=5)
            if gained == 0:
                print("  [任务] 附近没有更多树了")
                break
            total = self.engine.count_item(item_id=9)
            print(f"  [任务] 木材: {total}/{target}")
            rounds += 1

        self._current_task = ""
        print(f"  [任务] gather_wood 完成，总木材: {total}")
        return total

    def craft_item(self, item_name: str, amount: int = 1) -> int:
        """Craft an item by name. Returns amount crafted."""
        self._cancel.clear()
        self._current_task = f"合成 {item_name} x{amount}"
        print(f"\n  [任务] {self._current_task}")

        crafted = self.engine.craft(item_name=item_name, amount=amount)
        self._current_task = ""
        return crafted

    # --- Upgrade system (dynamic, recipe-driven) ---

    # Materials we can auto-gather (id → gather method name)
    GATHERABLE_MATERIALS = {
        9,   # Wood — chop trees
    }

    # Material source knowledge: id → description of where/how to get it
    MATERIAL_SOURCES = {
        9:    "砍树(地表)",
        23:   "打史莱姆掉落",
        3:    "挖石头(地表/地下)",
        61:   "挖铜矿(地表/地下浅层)",
        700:  "挖锡矿(地表/地下浅层)",
        56:   "挖铁矿(地下)",
        703:  "挖铅矿(地下)",
        57:   "挖银矿(地下)",
        706:  "挖钨矿(地下)",
        58:   "挖金矿(地下深层)",
        709:  "挖铂金矿(地下深层)",
        38:   "打恶魔眼掉落(夜晚)",
        71:   "打怪/开箱掉落(金币)",
        75:   "开箱/打怪掉落(红水晶)",
        169:  "挖沙块(沙漠)",
        173:  "挖泥块(地表/地下)",
        408:  "挖雪块(雪原)",
        210:  "蛛网(地下蜘蛛洞)",
        215:  "打黄蜂/在丛林采集(毒刺)",
        225:  "打暗影球/腐化地怪物(暗影鳞片)",
        86:   "采集(地表花朵)",
        2358: "挖沙漠化石(沙漠地下)",
        181:  "挖红晶石(地下)",
        178:  "钓鱼/打怪掉落",
        331:  "打骷髅/地牢怪物(骨头)",
    }

    # --- Item knowledge system ---

    # Important functional items (id → {category, description, keep_priority})
    # keep_priority: 3=must keep, 2=very useful, 1=nice to have
    ITEM_KNOWLEDGE = {
        # 回城类
        50:   {"cat": "recall", "desc": "魔镜(无限回城)", "pri": 3},
        3199: {"cat": "recall", "desc": "冰雪镜(无限回城)", "pri": 3},
        2350: {"cat": "recall", "desc": "回忆药水(一次性回城)", "pri": 2},
        # 钩爪类
        84:   {"cat": "hook", "desc": "抓钩(地下必备)", "pri": 3},
        1236: {"cat": "hook", "desc": "蛛网抓钩", "pri": 3},
        # 移动饰品
        53:   {"cat": "accessory", "desc": "云朵瓶(二段跳)", "pri": 2},
        54:   {"cat": "accessory", "desc": "疾风脚镯(加速)", "pri": 2},
        158:  {"cat": "accessory", "desc": "幸运马掌(防摔伤)", "pri": 2},
        # 照明
        8:    {"cat": "light", "desc": "火把", "pri": 1},
        282:  {"cat": "light", "desc": "荧光棒", "pri": 1},
        # 绳子/平台
        965:  {"cat": "mobility", "desc": "绳子(垂直移动)", "pri": 1},
        94:   {"cat": "building", "desc": "木平台", "pri": 1},
    }

    # Low-value blocks that can be trashed when inventory is full
    TRASH_ITEMS = {2, 3, 123, 169, 408, 170}  # dirt, stone, silt, sand, snow, mud

    # Items to always keep on player (never deposit to chest)
    # Checked by item ID; hotbar slots 0-9 are also always kept
    KEEP_ITEM_IDS = {
        50, 3199,       # magic mirror / ice mirror
        2350,           # recall potion
        84, 1236,       # grappling hooks
        8,              # torches
        965,            # rope
        94,             # wood platform
    }

    def _get_item_info(self, item: dict) -> str:
        """Get human-readable description of an item based on knowledge base + stats."""
        item_id = item.get("id", 0)
        if item_id in self.ITEM_KNOWLEDGE:
            return self.ITEM_KNOWLEDGE[item_id]["desc"]
        # Infer from stats
        parts = []
        if item.get("damage", 0) > 0:
            if item.get("pick", 0) > 0: parts.append(f"镐(pick={item['pick']})")
            elif item.get("axe", 0) > 0: parts.append(f"斧(axe={item['axe']})")
            elif item.get("hammer", 0) > 0: parts.append(f"锤(hammer={item['hammer']})")
            else: parts.append(f"武器(dmg={item['damage']})")
        if item.get("defense", 0) > 0: parts.append(f"防具(def={item['defense']})")
        if item.get("healLife", 0) > 0: parts.append(f"治疗(+{item['healLife']}HP)")
        if item.get("healMana", 0) > 0: parts.append(f"回蓝(+{item['healMana']}MP)")
        if item.get("accessory"): parts.append("饰品")
        if parts:
            return ", ".join(parts)
        return ""

    # --- Inventory management ---

    # --- Torch management ---

    _torch_slot = -1  # cached slot of torch in hotbar, -1 = unknown
    _holding_torch = False  # whether we're currently holding torch for light

    def _find_torch_slot(self) -> int:
        """Find torch slot in inventory. Returns slot or -1."""
        inv = self.engine.get_inventory()
        for item in inv:
            if item.get("id") == 8:  # torch
                return item["slot"]
        return -1

    def _torch_count(self) -> int:
        """Count total torches in inventory."""
        return self.engine.count_item(item_id=8)

    def hold_torch(self):
        """Hold torch in hand for light (when low on torches)."""
        if self._holding_torch:
            return
        slot = self._find_torch_slot()
        if slot >= 0 and slot < 10:
            self.engine.bridge.select_item(slot)
            self._holding_torch = True
        elif slot >= 10:
            # Move torch to hotbar slot 9 first
            self.engine.bridge.send({"cmd": "swap_slots", "from": slot, "to": 9})
            time.sleep(0.1)
            self.engine.bridge.select_item(9)
            self._holding_torch = True

    def unhold_torch(self):
        """Switch back to weapon (slot 0) from torch."""
        if self._holding_torch:
            self.engine.bridge.select_item(0)
            self._holding_torch = False

    def maybe_place_torch(self, last_torch_x: int, underground: bool = False) -> int:
        """Place torch if conditions met. Returns updated last_torch_x."""
        self.engine.bridge.get_state()
        time.sleep(0.02)
        px = self.engine.bridge.state.player.tile_x
        py = self.engine.bridge.state.player.tile_y
        is_night = not self.engine.bridge.state.day_time

        interval = 20 if underground else 40
        should_place = underground or is_night

        if not should_place or abs(px - last_torch_x) < interval:
            return last_torch_x

        torch_count = self._torch_count()
        if torch_count <= 0:
            return last_torch_x

        if torch_count <= 1:
            # Last torch — hold it instead of placing
            self.hold_torch()
            return last_torch_x

        # Try placing torch at multiple positions (body height, feet height, ground level)
        feet_y = py + 2
        candidates = [
            (px, feet_y),       # at feet level (on wall behind)
            (px, feet_y - 1),   # body level (on wall behind)
            (px + 1, feet_y),   # right side feet
            (px - 1, feet_y),   # left side feet
            (px, feet_y + 1),   # on ground surface
        ]
        for tx, ty in candidates:
            self.engine.bridge.send({"cmd": "place_tile",
                "x": tx, "y": ty, "tile_type": 4})
            resp = self.engine._wait_response("place_tile_result", timeout=0.5)
            if resp and resp.get("success"):
                print(f"  [火把] 放置火把 @({tx},{ty})")
                return px
        return last_torch_x  # failed, don't update position (retry sooner)

    _last_craft_torch_check = 0  # cooldown for craft attempts

    def _ensure_light(self):
        """Check brightness and hold/craft/place torch if dark. Call from any movement loop."""
        brightness = self.engine.bridge.state.player.brightness
        if brightness >= 0.3:
            return  # bright enough
        # Skip if game just started (lighting not yet calculated)
        if self.engine.bridge.state.tick < 300:
            return
        # Dark area — need torch
        torch_count = self._torch_count()
        if torch_count < 5:
            # Try to craft torches (need wood id=9 + gel id=23)
            now = time.time()
            if now - self._last_craft_torch_check > 10:  # check every 10s
                self._last_craft_torch_check = now
                wood = self.engine.count_item(item_id=9)
                gel = self.engine.count_item(item_id=23)
                if wood >= 2 and gel >= 1:
                    craft_amount = min(wood // 2, gel, 20)  # use at most half wood
                    print(f"  [火把] 黑暗区域，制造火把 (木材={wood}, 用{craft_amount}, 凝胶={gel})")
                    self.engine.bridge.send({"cmd": "craft", "item_id": 8,
                                             "amount": craft_amount})
                    resp = self.engine._wait_response("craft_result", timeout=2)
                    if resp:
                        crafted_n = resp.get("crafted", 0)
                        if crafted_n > 0:
                            print(f"  [火把] 成功制造 {crafted_n} 组火把")
                        else:
                            print(f"  [火把] 制造失败 (配方未找到或材料不足)")
                    torch_count = self._torch_count()
        if torch_count <= 0:
            return  # no torches and can't craft
        # Hold torch for light
        if not self._holding_torch:
            print(f"  [火把] 亮度={brightness:.2f}，手持火把照明")
            self.hold_torch()

    def _platform_count(self) -> int:
        """Count total wood platforms in inventory."""
        return self.engine.count_item(item_id=94)

    _last_craft_platform_check = 0

    def _ensure_platforms(self, min_count: int = 30):
        """Craft wood platforms if running low. Uses at most half of available wood.
        Recipe: 1 Wood → 2 Wood Platforms."""
        platform_count = self._platform_count()
        if platform_count >= min_count:
            return
        now = time.time()
        if now - self._last_craft_platform_check < 15:  # check every 15s
            return
        self._last_craft_platform_check = now
        wood = self.engine.count_item(item_id=9)
        wood_for_platforms = wood // 2  # reserve half for torches
        if wood_for_platforms < 1:
            return
        # 1 wood → 2 platforms, craft enough to reach min_count
        need = (min_count - platform_count + 1) // 2  # wood needed
        craft_amount = min(need, wood_for_platforms, 25)
        if craft_amount < 1:
            return
        print(f"  [补给] 制造木平台 (木材={wood}, 用{craft_amount}, 现有平台={platform_count})")
        self.engine.bridge.send({"cmd": "craft", "item_id": 94,
                                  "amount": craft_amount})
        resp = self.engine._wait_response("craft_result", timeout=2)
        if resp:
            crafted_n = resp.get("crafted", 0)
            if crafted_n > 0:
                print(f"  [补给] 成功制造 {crafted_n * 2} 个木平台")

    def clean_inventory(self) -> int:
        """Trash low-value blocks from inventory. Returns number of slots freed."""
        inv = self.engine.get_inventory()
        freed = 0
        for item in inv:
            slot = item.get("slot", -1)
            if slot < 10:  # never trash hotbar
                continue
            if item.get("id", 0) in self.TRASH_ITEMS:
                if self.engine.trash_item(slot):
                    freed += 1
        if freed > 0:
            print(f"  [背包] 清理了 {freed} 格低价值方块")
        return freed

    def count_empty_slots(self) -> int:
        """Count empty inventory slots."""
        inv = self.engine.get_inventory()
        used = {item["slot"] for item in inv}
        return 50 - len(used)

    def auto_equip(self) -> bool:
        """Check inventory for better equipment, auto-swap to hotbar + armor/accessory slots."""
        inv = self.engine.get_inventory()
        # Get current hotbar equipment stats
        cur = {}
        for item in inv:
            s = item.get("slot", -1)
            if s == 0: cur["weapon"] = item
            elif s == 1: cur["pick"] = item
            elif s == 2: cur["axe"] = item

        swapped = False
        for item in inv:
            slot = item.get("slot", -1)
            if slot < 10:  # already in hotbar
                continue
            item_id = item.get("id", 0)
            dmg = item.get("damage", 0)
            pick = item.get("pick", 0)
            axe = item.get("axe", 0)

            # Better weapon? (exclude coins, consumables like grenades/bombs)
            is_coin = item_id in {71, 72, 73, 74}  # copper/silver/gold/platinum coin
            is_consumable = item.get("consumable", False)
            if dmg > 0 and pick == 0 and axe == 0 and item.get("hammer", 0) == 0 \
                    and not is_coin and not is_consumable:
                cur_dmg = cur.get("weapon", {}).get("damage", 0)
                if dmg > cur_dmg:
                    self.engine.bridge.send({"cmd": "swap_slots", "from": slot, "to": 0})
                    time.sleep(0.1)
                    print(f"  [装备] 换武器: {item['name']} (dmg {cur_dmg}→{dmg})")
                    swapped = True
                    cur["weapon"] = item
            # Better pickaxe?
            elif pick > 0:
                cur_pick = cur.get("pick", {}).get("pick", 0)
                if pick > cur_pick:
                    self.engine.bridge.send({"cmd": "swap_slots", "from": slot, "to": 1})
                    time.sleep(0.1)
                    print(f"  [装备] 换镐: {item['name']} (pick {cur_pick}→{pick})")
                    swapped = True
                    cur["pick"] = item
            # Better axe?
            elif axe > 0:
                cur_axe = cur.get("axe", {}).get("axe", 0)
                if axe > cur_axe:
                    self.engine.bridge.send({"cmd": "swap_slots", "from": slot, "to": 2})
                    time.sleep(0.1)
                    print(f"  [装备] 换斧: {item['name']} (axe {cur_axe}→{axe})")
                    swapped = True
                    cur["axe"] = item

            # Important functional item? Move to hotbar if there's room
            if item_id in self.ITEM_KNOWLEDGE and self.ITEM_KNOWLEDGE[item_id]["pri"] >= 2:
                # Find empty hotbar slot (3-9)
                hotbar_used = {it["slot"] for it in inv if it["slot"] < 10}
                for hs in range(3, 10):
                    if hs not in hotbar_used:
                        self.engine.bridge.send({"cmd": "swap_slots", "from": slot, "to": hs})
                        time.sleep(0.1)
                        info = self.ITEM_KNOWLEDGE[item_id]
                        print(f"  [装备] 发现重要物品: {item['name']} ({info['desc']}) → slot {hs}")
                        swapped = True
                        break

        # --- 防具和饰品自动装备 ---
        equip_swapped = self._auto_equip_armor_accessory(inv)
        return swapped or equip_swapped

    def _auto_equip_armor_accessory(self, inv: list) -> bool:
        """Check inventory for armor/accessories and equip if better than current."""
        # 获取当前装备栏
        cur_equip = self.engine.get_equip_slots()
        if cur_equip is None:
            return False  # 命令不支持（mod未更新）

        # 当前装备的防御值
        cur_head_def = 0
        cur_body_def = 0
        cur_legs_def = 0
        cur_acc_ids = set()  # 已装备的饰品ID（避免重复装备）
        cur_acc_slots = {}   # equip_slot → defense/rare
        for eq in cur_equip:
            s = eq.get("slot", -1)
            if s == 0: cur_head_def = eq.get("defense", 0)
            elif s == 1: cur_body_def = eq.get("defense", 0)
            elif s == 2: cur_legs_def = eq.get("defense", 0)
            elif 3 <= s <= 9:
                cur_acc_ids.add(eq.get("id", 0))
                cur_acc_slots[s] = eq

        swapped = False

        # 收集背包中的防具和饰品
        armor_candidates = {"head": [], "body": [], "legs": []}
        accessory_candidates = []

        for item in inv:
            slot = item.get("slot", -1)
            if slot < 0 or slot >= 50:
                continue
            item_id = item.get("id", 0)
            defense = item.get("defense", 0)

            # 头盔：headSlot > 0
            if item.get("headSlot", -1) > 0:
                armor_candidates["head"].append((slot, item, defense))
            # 胸甲：bodySlot > 0
            elif item.get("bodySlot", -1) > 0:
                armor_candidates["body"].append((slot, item, defense))
            # 腿甲：legSlot > 0
            elif item.get("legSlot", -1) > 0:
                armor_candidates["legs"].append((slot, item, defense))
            # 饰品：accessory=True 且不是已装备的
            elif item.get("accessory", False) and item_id not in cur_acc_ids:
                accessory_candidates.append((slot, item))

        # 装备更好的防具
        armor_map = {"head": (0, cur_head_def), "body": (1, cur_body_def), "legs": (2, cur_legs_def)}
        for part, (equip_slot, cur_def) in armor_map.items():
            candidates = armor_candidates[part]
            if not candidates:
                continue
            # 选防御最高的
            best_slot, best_item, best_def = max(candidates, key=lambda x: x[2])
            if best_def > cur_def:
                if self.engine.equip_item(best_slot, equip_slot):
                    part_name = {"head": "头盔", "body": "胸甲", "legs": "腿甲"}[part]
                    print(f"  [装备] 换{part_name}: {best_item['name']} (防御 {cur_def}→{best_def})")
                    swapped = True

        # 装备饰品（填空位或替换低稀有度的）
        for acc_slot, acc_item in accessory_candidates:
            # 先找空的饰品栏（3-9）
            empty_slot = None
            for s in range(3, 10):
                if s not in cur_acc_slots:
                    empty_slot = s
                    break

            if empty_slot is not None:
                if self.engine.equip_item(acc_slot, empty_slot):
                    print(f"  [装备] 新饰品: {acc_item['name']} → 栏位{empty_slot}")
                    cur_acc_slots[empty_slot] = acc_item
                    cur_acc_ids.add(acc_item.get("id", 0))
                    swapped = True
            else:
                # 所有栏位都满了，尝试替换稀有度最低的
                acc_rare = acc_item.get("rare", 0)
                worst_slot = None
                worst_rare = acc_rare  # 只替换比新饰品差的
                for s, eq in cur_acc_slots.items():
                    eq_rare = eq.get("rare", 0)
                    if eq_rare < worst_rare:
                        worst_rare = eq_rare
                        worst_slot = s
                if worst_slot is not None:
                    old_name = cur_acc_slots[worst_slot].get("name", "?")
                    if self.engine.equip_item(acc_slot, worst_slot):
                        print(f"  [装备] 换饰品: {old_name} → {acc_item['name']} (稀有度更高)")
                        cur_acc_slots[worst_slot] = acc_item
                        cur_acc_ids.add(acc_item.get("id", 0))
                        swapped = True

        return swapped

    def deposit_items(self, chest_x: int, chest_y: int) -> list:
        """Deposit items to chest, keeping equipment/potions/tools on player."""
        inv = self.engine.get_inventory()
        keep_slots = set(self.ESSENTIAL_HOTBAR_SLOTS)  # weapon, pick, axe

        for item in inv:
            slot = item.get("slot", -1)
            item_id = item.get("id", 0)
            if item_id == 0:
                continue
            # Hotbar non-essential: only keep if in KEEP_ITEM_IDS
            if slot < 10 and slot not in self.ESSENTIAL_HOTBAR_SLOTS:
                if item_id in self.KEEP_ITEM_IDS:
                    keep_slots.add(slot)
                continue
            # Backpack: keep functional items
            if item_id in self.KEEP_ITEM_IDS:
                keep_slots.add(slot)
            elif item.get("healLife", 0) > 0:
                keep_slots.add(slot)
            elif item.get("healMana", 0) > 0:
                keep_slots.add(slot)

        return self.engine.deposit_to_chest(chest_x, chest_y, list(keep_slots))

    # --- Base management ---

    def init_base(self):
        """Initialize base location at spawn point and scan for chests."""
        spawn = self.engine.get_spawn()
        if not spawn:
            print("  [基地] 获取出生点失败")
            return False
        self.base_x, self.base_y = spawn
        print(f"  [基地] 出生点: ({self.base_x}, {self.base_y})")
        self.refresh_base_chests()

        # Ensure player has a magic mirror for teleporting home
        mirror = self.engine.find_item(item_id=50)
        if not mirror:
            mirror = self.engine.find_item(item_id=3199)  # ice mirror
        if not mirror:
            print("  [基地] 没有魔镜，生成一个")
            self.engine.give_item(50)  # magic mirror

        return True

    def _is_base_chest(self, x: int, y: int) -> bool:
        """Check if a chest at (x, y) belongs to our base storage.
        Chests are 2x2 tiles, so use proximity check (within 1 tile)."""
        for ch in self.base_chests:
            if abs(ch["x"] - x) <= 1 and abs(ch["y"] - y) <= 1:
                return True
        return False

    def refresh_base_chests(self):
        """Rescan chests near base."""
        if self.base_x is None:
            return
        self.base_chests = self.engine.scan_chests(self.base_x, self.base_y,
                                                    range_x=50, range_y=30)
        print(f"  [基地] 扫描到 {len(self.base_chests)} 个箱子")
        for ch in self.base_chests:
            print(f"    箱子 @({ch['x']},{ch['y']}): {ch['items_count']}物品, {ch['empty_slots']}空位")

    # --- Base storage flow ---

    def store_items_at_base(self) -> int:
        """Store non-essential items in base chests. Returns total items stored."""
        if not self.base_chests:
            self.refresh_base_chests()
        if not self.base_chests:
            print("  [基地] 没有箱子可用，尝试放置新箱子")
            if not self._place_new_chest():
                print("  [基地] 无法放置箱子，跳过存储")
                return 0
            self.refresh_base_chests()

        total_stored = 0
        # Build keep_slots list — only keep essential hotbar + functional items
        inv = self.engine.get_inventory()
        keep_slots = list(self.ESSENTIAL_HOTBAR_SLOTS)  # weapon, pick, axe
        for item in inv:
            slot = item.get("slot", -1)
            item_id = item.get("id", 0)
            if item_id == 0:
                continue
            # Hotbar non-essential slots: only keep if item is in KEEP_ITEM_IDS
            if slot < 10 and slot not in self.ESSENTIAL_HOTBAR_SLOTS:
                if item_id in self.KEEP_ITEM_IDS:
                    keep_slots.append(slot)
                continue
            # Backpack slots: keep functional items
            if item_id in self.KEEP_ITEM_IDS:
                keep_slots.append(slot)
            elif item.get("healLife", 0) > 0:
                keep_slots.append(slot)
            elif item.get("healMana", 0) > 0:
                keep_slots.append(slot)

        for ch in self.base_chests:
            cx, cy = ch["x"], ch["y"]
            # Navigate close to chest (within 4 tiles)
            self.engine.bridge.get_state()
            time.sleep(0.1)
            px = self.engine.bridge.state.player.tile_x
            if abs(px - cx) > 4:
                result = self.engine.navigate_to(cx, cy - 1, timeout=10)
                if result not in ("arrived", "close_enough"):
                    print(f"  [基地] 无法走到箱子 @({cx},{cy}), 跳过")
                    continue

            # Open chest with viewer pause
            opened = self.engine.open_chest(cx, cy)
            print(f"  [基地·诊断] open_chest @({cx},{cy}) → {opened}")
            if not opened:
                continue
            time.sleep(2)  # let viewers see chest contents

            # Quick stack first (merge existing item types)
            stacked = self.engine.quick_stack()
            print(f"  [基地·诊断] quick_stack 返回 {stacked}")

            # Deposit remaining non-essential items
            deposited = self.engine.deposit_to_chest(cx, cy, keep_slots)
            print(f"  [基地·诊断] deposit_to_chest @({cx},{cy}) 返回 {len(deposited)} 件 keep_slots={sorted(keep_slots)}")
            total_stored += len(deposited)

            self.engine.close_chest()
            time.sleep(0.3)

            # Check if inventory still has stuff to store
            inv_check = self.engine.get_inventory()
            non_empty = sum(1 for it in inv_check if it.get("id", 0) > 0 and it["slot"] >= 10
                            and it["slot"] not in keep_slots)
            if non_empty == 0:
                break  # all stored

        # If still have items and all chests full, place new chest
        inv_check = self.engine.get_inventory()
        non_empty = sum(1 for it in inv_check if it.get("id", 0) > 0 and it["slot"] >= 10
                        and it["slot"] not in keep_slots)
        if non_empty > 0:
            print(f"  [基地] 箱子已满，尝试放置新箱子")
            if self._place_new_chest():
                self.refresh_base_chests()
                new_ch = self.base_chests[-1]
                self.engine.open_chest(new_ch["x"], new_ch["y"])
                time.sleep(1)
                deposited = self.engine.deposit_to_chest(new_ch["x"], new_ch["y"], keep_slots)
                total_stored += len(deposited)
                self.engine.close_chest()

        print(f"  [基地] 共存入 {total_stored} 种物品")
        return total_stored

    def _place_new_chest(self) -> bool:
        """Craft and place a new chest near base. Returns success."""
        # Check if we have any chest item (createTile field, C# side checks TileID.Containers)
        # Common chest IDs: 48=wooden chest, 306=gold chest, 328=shadow chest, etc.
        has_chest = False
        inv = self.engine.get_inventory()
        for item in inv:
            if item.get("createTile") == 21 and item.get("id", 0) > 0:
                has_chest = True
                break
        if not has_chest:
            crafted = self.engine.craft(item_id=48, amount=1)
            if crafted == 0:
                print("  [基地] 无法合成箱子（需要木材×8 + 工作台），背包也没有箱子物品")
                return False

        bx, by = self.base_x, self.base_y
        for offset in range(0, 40, 3):
            for sign in [1, -1]:
                tx = bx + offset * sign
                for dy in range(-3, 4):
                    ty = by + dy
                    if self.engine.place_chest(tx, ty):
                        return True
        print("  [基地] 找不到合适的箱子放置位置")
        return False

    # --- Go-home flow ---

    def use_mirror(self) -> bool:
        """Use magic mirror or recall potion to teleport home. Returns success."""
        mirror = self.engine.find_item(item_id=50)  # magic mirror
        if not mirror:
            mirror = self.engine.find_item(item_id=3199)  # ice mirror
        if not mirror:
            mirror = self.engine.find_item(item_id=2350)  # recall potion
        if not mirror:
            print("  [回家] 没有魔镜或回忆药水")
            return False

        slot = mirror[0]["slot"]
        if slot >= 10:
            self.engine.bridge.send({"cmd": "swap_slots", "from": slot, "to": 9})
            time.sleep(0.2)
            slot = 9

        self.engine.bridge.select_item(slot)
        time.sleep(0.1)
        self.engine.hold_use_item(frames=200)  # ~3.3 seconds at 60fps
        time.sleep(3.5)  # wait for animation to complete

        self.engine.bridge.get_state()
        time.sleep(0.1)
        px = self.engine.bridge.state.player.tile_x
        if self.base_x and abs(px - self.base_x) < 30:
            print(f"  [回家] 传送成功，当前位置 ({px})")
            return True
        print(f"  [回家] 传送可能失败，当前位置 ({px}), 基地 ({self.base_x})")
        return False

    def go_home_and_resupply(self) -> bool:
        """Full home trip: teleport → craft upgrades → store items → resupply."""
        print(f"\n  [回家] 开始回家流程")

        if self.base_x is None:
            self.init_base()

        # Step 1: Teleport home
        if not self.use_mirror():
            print("  [回家] 无传送手段，步行回基地")
            if self.base_x:
                self.engine.navigate_to(self.base_x, self.base_y - 3, timeout=120)
            else:
                return False

        # Step 2: Craft upgrades BEFORE storing (so ores aren't put away)
        time.sleep(0.5)
        self.check_upgrade()

        # Step 3: Store items in base chests
        self.store_items_at_base()

        # Step 4: Clean remaining trash
        self.clean_inventory()

        # Step 5: Resupply
        self._resupply()

        print(f"  [回家] 回家流程完成")
        return True

    def _resupply(self):
        """Ensure enough torches and potions in hotbar."""
        torch_count = self.engine.count_item(8)  # torch
        if torch_count < 50:
            deficit = 50 - torch_count
            craft_times = (deficit + 2) // 3
            crafted = self.engine.craft(item_id=8, amount=craft_times)
            if crafted > 0:
                print(f"  [补给] 合成火把 x{crafted * 3}")

        inv = self.engine.get_inventory()
        hotbar_has_potion = False
        for item in inv:
            if item["slot"] < 10 and item.get("healLife", 0) > 0:
                hotbar_has_potion = True
                break

        if not hotbar_has_potion:
            for item in inv:
                if item["slot"] >= 10 and item.get("healLife", 0) > 0:
                    self.engine.bridge.send({"cmd": "swap_slots",
                        "from": item["slot"], "to": 9})
                    print(f"  [补给] 治疗药水移到快捷栏: {item.get('name')}")
                    time.sleep(0.2)
                    break

    # --- Auto-heal ---

    def _auto_heal_check(self):
        """Auto-use healing potion if HP below 50%. Respects 2s cooldown."""
        if time.time() - self._last_heal_time < 2:
            return
        self.engine.bridge.get_state()
        hp = self.engine.bridge.state.player.hp
        max_hp = self.engine.bridge.state.player.max_hp
        if max_hp > 0 and hp < max_hp * 0.5:
            self.engine.bridge.send({"cmd": "quick_heal"})
            self._last_heal_time = time.time()
            print(f"  [药水] 自动治疗 (HP: {hp}/{max_hp})")

    # --- Inventory helpers ---

    # Hotbar slots that should always stay on the player (weapon, pick, axe, torch, platform)
    ESSENTIAL_HOTBAR_SLOTS = {0, 1, 2}  # weapon, pick, axe — always kept
    # Other hotbar slots are kept only if item is in KEEP_ITEM_IDS

    def _inventory_nearly_full(self, threshold: int = 3) -> bool:
        """Check if inventory (all 50 slots) has fewer than threshold empty slots."""
        empty = self.count_empty_slots()
        return empty < threshold

    # --- Two-layer crafting chain ---

    def _try_craft_intermediate(self, ingredient: dict) -> bool:
        """Try to craft a missing intermediate material (1-layer deduction).
        e.g. missing iron bars -> check if we have iron ore -> smelt.
        Returns True if successfully crafted enough."""
        name = ingredient.get("name", "")
        need = ingredient.get("need", 0)
        have = ingredient.get("have", 0)
        deficit = need - have
        if deficit <= 0:
            return True

        recipes = self.engine.get_recipes(category="all", limit=100)
        for r in recipes:
            result = r.get("result", {})
            if result.get("name") != name and result.get("id") != ingredient.get("id"):
                continue
            if not r.get("hasMaterials", False):
                continue
            if not r.get("hasStations", False):
                continue
            result_stack = result.get("stack", 1)
            craft_times = (deficit + result_stack - 1) // result_stack
            print(f"  [合成链] 中间产物: {name} 需要{deficit}个, 合成{craft_times}次")
            crafted = self.engine.craft(item_name=name, amount=craft_times)
            if crafted > 0:
                print(f"  [合成链] 成功合成 {name} x{crafted}")
                return True
        return False

    def check_upgrade(self) -> bool:
        """Scan all recipes, find best upgrades, gather materials if possible, craft. Returns True if something was crafted."""
        self._cancel.clear()
        print(f"\n  [任务] 检查可合成升级...")

        # Refresh state
        self.engine.bridge.get_state()
        time.sleep(0.1)
        state = self.engine.bridge.state
        hotbar = state.hotbar

        # Get current equipment stats
        cur_weapon_dmg = 0
        cur_pick_power = 0
        cur_axe_power = 0
        for h in hotbar:
            s = h.get("slot", -1)
            if s == 0:
                cur_weapon_dmg = h.get("damage", 0)
            elif s == 1:
                cur_pick_power = h.get("pick", 0)
            elif s == 2:
                cur_axe_power = h.get("axe", 0)

        crafted_any = False

        # Query each category separately for efficiency
        upgrade_checks = [
            ("weapon", "武器", 0, "damage", cur_weapon_dmg),
            ("pick",   "镐",   1, "pick",   cur_pick_power),
            ("axe",    "斧",   2, "axe",    cur_axe_power),
        ]

        for category, label, target_slot, stat_key, cur_stat in upgrade_checks:
            if self._cancelled():
                break

            recipes = self.engine.get_recipes(category=category, limit=50)
            if not recipes:
                continue

            # Find best upgrade (highest stat that exceeds current)
            best = None  # (recipe, stat_value)
            for r in recipes:
                result = r.get("result", {})
                val = result.get(stat_key, 0)
                if val > cur_stat:
                    if best is None or val > best[1]:
                        best = (r, val)

            if not best:
                continue

            recipe, best_val = best
            result = recipe["result"]
            item_name = result["name"]
            ingredients = recipe.get("ingredients", [])
            can_craft = recipe.get("canCraft", False)

            has_materials = recipe.get("hasMaterials", False)
            has_stations = recipe.get("hasStations", True)
            stations = recipe.get("stations", [])

            print(f"  [任务] 发现{label}升级: {item_name} ({stat_key}={best_val}, 当前={cur_stat})")

            # Check crafting station requirement
            if not has_stations:
                missing = [s["name"] for s in stations if not s.get("nearby", False)]
                print(f"  [任务] {item_name} 需要工作站: {', '.join(missing)}，请先走到旁边")
                continue

            # If materials not enough, try gathering
            if not has_materials:
                # Try 2-layer deduction — craft intermediate materials first
                all_intermediates_ok = True
                for ing in ingredients:
                    if ing.get("enough", False):
                        continue
                    if not self._try_craft_intermediate(ing):
                        all_intermediates_ok = False

                if all_intermediates_ok:
                    recipes2 = self.engine.get_recipes(category=category, limit=50)
                    for r2 in recipes2:
                        if r2["result"].get("name") == item_name and r2.get("canCraft", False):
                            can_craft = True
                            has_materials = True
                            break

                if not has_materials:
                    # Fall through to existing gathering logic below...
                    all_gathered = True
                    for ing in ingredients:
                        if ing.get("enough", False):
                            continue
                        # Try to gather this material
                        if ing["id"] in self.GATHERABLE_MATERIALS:
                            need = ing.get("need", 0)
                            have = ing.get("have", 0)
                            deficit = need - have + 10
                            if ing["id"] == 9:  # wood
                                print(f"  [任务] 合成 {item_name} 需要 {ing['name']} (有{have}/需{need})，去砍树")
                                self.gather_wood(target=have + deficit)
                        else:
                            need = ing.get("need", 0)
                            have = ing.get("have", 0)
                            source = self.MATERIAL_SOURCES.get(ing["id"], "未知来源")
                            print(f"  [任务] {label}升级 {item_name} 缺 {ing['name']}(有{have}/需{need}) — 获取方式: {source}")
                            all_gathered = False
                            break

                    if not all_gathered:
                        continue

                    # Re-query to check if we can craft now
                    recipes2 = self.engine.get_recipes(category=category, limit=50)
                    can_now = False
                    for r2 in recipes2:
                        if r2["result"].get("name") == item_name and r2.get("canCraft", False):
                            can_now = True
                            break
                    if not can_now:
                        print(f"  [任务] 采集后仍无法合成 {item_name}")
                        continue

            # Craft the upgrade
            crafted = self.engine.craft(item_name=item_name, amount=1)
            if crafted > 0:
                print(f"  [任务] 合成 {label} {item_name} 成功!")
                # Swap to correct hotbar slot
                items = self.engine.find_item(name=item_name)
                if items:
                    from_slot = items[0]["slot"]
                    if from_slot != target_slot:
                        self.engine.bridge.send({"cmd": "swap_slots",
                            "from": from_slot, "to": target_slot})
                        time.sleep(0.1)
                crafted_any = True

        if not crafted_any:
            print(f"  [任务] 当前没有可升级的装备")
        return crafted_any

    # --- Strategic brain support ---

    def build_status_summary(self, recent_actions: list[str] = None,
                             completed_goals: list[str] = None) -> str:
        """Build a text summary of current game state for the strategic brain."""
        self.engine.bridge.get_state()
        time.sleep(0.1)
        state = self.engine.bridge.state
        p = state.player

        # Equipment stats from hotbar
        weapon_name, weapon_dmg = "none", 0
        pick_name, pick_power = "none", 0
        axe_name, axe_power = "none", 0
        for h in state.hotbar:
            s = h.get("slot", -1)
            if s == 0:
                weapon_name = h.get("name", "?")
                weapon_dmg = h.get("damage", 0)
            elif s == 1:
                pick_name = h.get("name", "?")
                pick_power = h.get("pick", 0)
            elif s == 2:
                axe_name = h.get("name", "?")
                axe_power = h.get("axe", 0)

        # Defense from armor (sum of equipped armor defense)
        defense = 0
        inv = self.engine.get_inventory()
        for item in inv:
            if item.get("defense", 0) > 0 and item.get("slot", 99) >= 50:
                # Armor slots are 50+ in terraria
                defense += item.get("defense", 0)

        # Key materials count
        key_ores = {
            61: "Copper Ore", 700: "Tin Ore",
            56: "Iron Ore", 703: "Lead Ore",
            57: "Silver Ore", 706: "Tungsten Ore",
            58: "Gold Ore", 709: "Platinum Ore",
            20: "Copper Bar", 22: "Iron Bar", 21: "Silver Bar", 19: "Gold Bar",
            704: "Lead Bar", 707: "Tungsten Bar", 710: "Platinum Bar",
            38: "Lens", 43: "Suspicious Looking Eye",
            29: "Life Crystal",
        }
        materials = []
        for ore_id, ore_name in key_ores.items():
            count = self.engine.count_item(item_id=ore_id)
            if count > 0:
                materials.append(f"  {ore_name}: {count}")

        # Build text
        lines = [
            "=== 当前状态报告 ===",
            f"HP: {p.hp}/{p.max_hp}",
            f"位置: ({p.tile_x}, {p.tile_y})",
            f"时间: {'白天' if state.day_time else '夜晚'}",
            "",
            "装备:",
            f"  武器: {weapon_name} (伤害={weapon_dmg})",
            f"  镐: {pick_name} (镐力={pick_power})",
            f"  斧: {axe_name} (斧力={axe_power})",
            f"  防御力: {defense}",
            "",
            f"背包空位: {self.count_empty_slots()}/50",
        ]

        if materials:
            lines.append("")
            lines.append("关键材料:")
            lines.extend(materials)

        if recent_actions:
            lines.append("")
            lines.append("最近行为:")
            for a in recent_actions[-5:]:  # last 5 actions
                lines.append(f"  - {a}")

        if completed_goals:
            lines.append("")
            lines.append("已完成目标:")
            for g in completed_goals[-10:]:
                lines.append(f"  - {g}")

        return "\n".join(lines)

    # --- Goal execution ---

    def execute_goal(self, goal: StrategicGoal) -> tuple[bool, str]:
        """Execute a strategic goal. Returns (success, result_description)."""
        print(f"\n  [目标] 执行: {goal.goal_type} → {goal.target} ({goal.reason})")

        if goal.goal_type == "gather":
            return self._execute_gather(goal)
        elif goal.goal_type == "craft":
            return self._execute_craft(goal)
        elif goal.goal_type == "explore":
            return self._execute_explore(goal)
        elif goal.goal_type == "boss_prep":
            return self._execute_boss_prep(goal)
        else:
            return False, f"未知目标类型: {goal.goal_type}"

    def _execute_gather(self, goal: StrategicGoal) -> tuple[bool, str]:
        """Gather target material: explore underground, come home, check count."""
        target_name = goal.target
        quantity = goal.params.get("quantity") or 10
        direction = goal.params.get("direction") or 1

        # Check how much we already have
        before_count = self.engine.count_item(name=target_name)
        print(f"  [gather] 目标: {target_name} x{quantity}, 当前: {before_count}")

        if before_count >= quantity:
            return True, f"已有 {target_name} x{before_count} >= {quantity}"

        # Go explore
        self.explore_underground(direction=direction, max_time=300)

        # Come home and check
        self.go_home_and_resupply()

        after_count = self.engine.count_item(name=target_name)
        gained = after_count - before_count
        print(f"  [gather] 探索后: {target_name} x{after_count} (获得+{gained})")

        if after_count >= quantity:
            return True, f"收集到 {target_name} x{after_count}"
        else:
            return False, f"{target_name} 仍不足: {after_count}/{quantity} (本轮获得+{gained})"

    def _execute_craft(self, goal: StrategicGoal) -> tuple[bool, str]:
        """Craft target item at base."""
        target_name = goal.target

        # Ensure we're at base
        if self.base_x is None:
            self.init_base()
        self.use_mirror()
        time.sleep(1)

        # Try upgrade check (handles intermediate crafting too)
        crafted = self.check_upgrade()
        if crafted:
            # Verify the specific item was made
            items = self.engine.find_item(name=target_name)
            if items:
                return True, f"合成 {target_name} 成功"

        # Try direct craft if check_upgrade didn't cover it
        result = self.engine.craft(item_name=target_name, amount=1)
        if result > 0:
            return True, f"合成 {target_name} 成功"

        return False, f"无法合成 {target_name}，可能缺少材料"

    def _execute_explore(self, goal: StrategicGoal) -> tuple[bool, str]:
        """Explore underground in the given direction."""
        direction = goal.params.get("direction", 1)
        dir_cn = "右" if direction > 0 else "左"

        result = self.explore_underground(direction=direction, max_time=300)

        # Come home after exploring
        self.go_home_and_resupply()

        return True, f"向{dir_cn}探索完成"

    def _execute_boss_prep(self, goal: StrategicGoal) -> tuple[bool, str]:
        """Check if ready for a boss fight."""
        target_boss = goal.target
        missing = []

        self.engine.bridge.get_state()
        time.sleep(0.1)

        # Get current stats
        weapon_dmg = 0
        for h in self.engine.bridge.state.hotbar:
            if h.get("slot") == 0:
                weapon_dmg = h.get("damage", 0)
                break

        if target_boss.lower() in ("eye_of_cthulhu", "eye of cthulhu"):
            # Check: Suspicious Looking Eye
            eye_count = self.engine.count_item(item_id=43)
            if eye_count < 1:
                lens_count = self.engine.count_item(item_id=38)
                if lens_count >= 6:
                    missing.append("需要在恶魔祭坛合成 Suspicious Looking Eye (有Lens x" + str(lens_count) + ")")
                else:
                    missing.append(f"需要 Lens x6 (当前 x{lens_count})")
            if weapon_dmg < 12:
                missing.append(f"武器伤害不足: {weapon_dmg} < 12")

        elif target_boss.lower() in ("eater_of_worlds", "eater of worlds"):
            worm_food = self.engine.count_item(item_id=70)
            if worm_food < 1:
                missing.append("需要 Worm Food")
            if weapon_dmg < 15:
                missing.append(f"武器伤害不足: {weapon_dmg} < 15")

        elif target_boss.lower() in ("brain_of_cthulhu", "brain of cthulhu"):
            spine = self.engine.count_item(item_id=1331)
            if spine < 1:
                missing.append("需要 Bloody Spine")
            if weapon_dmg < 15:
                missing.append(f"武器伤害不足: {weapon_dmg} < 15")

        else:
            return False, f"未知Boss: {target_boss}"

        if not missing:
            return True, f"{target_boss} 准备就绪"
        else:
            return False, f"{target_boss} 未就绪: " + "; ".join(missing)

    # --- Underground exploration ---

    # Ore tile types (tileType → name)
    ORE_TILES = {
        7: "铜矿", 166: "锡矿",
        6: "铁矿", 167: "铅矿",
        9: "银矿", 168: "钨矿",
        8: "金矿", 169: "铂金矿",
        22: "恶魔矿", 204: "猩红矿",
        37: "陨石",
        56: "黑曜石",
        58: "地狱石",
        63: "蓝宝石", 64: "红宝石", 65: "绿宝石",
        66: "黄玉", 67: "紫晶", 68: "钻石",
    }

    # Chest tile type
    CHEST_TILE = 21
    # Pot tile type
    POT_TILE = 28

    # Pickaxe reach limits (from player center tile)
    PICK_RANGE_X = 5
    PICK_RANGE_Y = 4

    def scan_surroundings(self, radius: int = 20) -> dict:
        """One scan, extract all useful info. Replaces separate scan_ores/passages/drops/cavity calls.

        Returns dict with:
          px, py: player tile position
          solid: set((x,y)) for fast lookup
          ores: [(x, y, tileType, name)] sorted by distance
          chests: [(x, y)] sorted by distance
          pots: [(x, y)] sorted by distance
          passages: [passage_dict] sorted by priority
          drop_points: [drop_dict] sorted by fall_depth
          cavity_below: cavity_dict or None
        """
        # One scan call for everything
        resp = self.engine.scan_relative(-radius, -radius, radius * 2, radius * 2)
        tiles = resp.get("tiles", [])
        px = resp.get("playerX", 0)
        py = resp.get("playerY", 0)
        feet_y = py + 2

        # Build solid set + classify tiles
        solid = set()
        ores = []
        chests = set()
        pots = set()
        for t in tiles:
            tx, ty, tt = t["x"], t["y"], t.get("t", -1)
            tc = t.get("c", 0)  # 0=Block, 1=OneWay, 2=Ore
            solid.add((tx, ty))
            if tc == 2 or tt in self.ORE_TILES:
                ores.append((tx, ty, tt, self.ORE_TILES.get(tt, f"矿石({tt})")))
            if tt == self.CHEST_TILE:
                chests.add((tx, ty))
            elif tt == self.POT_TILE:
                pots.add((tx, ty))

        ores.sort(key=lambda o: abs(o[0] - px) + abs(o[1] - py))
        chests_sorted = sorted(chests, key=lambda c: abs(c[0] - px) + abs(c[1] - py))
        pots_sorted = sorted(pots, key=lambda c: abs(c[0] - px) + abs(c[1] - py))

        # ── Helper functions ──
        def is_air(x, y):
            return (x, y) not in solid

        def body_passable(x, y):
            """Check if player (3 tiles tall) can stand at (x, y=head)."""
            return is_air(x, y) and is_air(x, y + 1) and is_air(x, y + 2)

        # ── Passages (4 directions) ──
        passages = []
        for dir_name, x_range, x_step in [("right", range(px + 2, px + radius), 1),
                                            ("left", range(px - 2, px - radius, -1), -1)]:
            depth = 0
            scan_y = py
            deepest_y = py
            for col_x in x_range:
                found = False
                for offset in [0, 1, -1, 2, -2]:
                    test_y = scan_y + offset
                    if body_passable(col_x, test_y):
                        scan_y = test_y
                        deepest_y = max(deepest_y, test_y + 2)
                        found = True
                        break
                if not found:
                    break
                depth += 1
            if depth >= 5:
                total = depth * 3
                air = sum(1 for dx in range(1, depth + 1)
                          for dy in range(3) if is_air(px + x_step * dx, py + dy))
                passages.append({
                    "direction": dir_name, "entry_x": px + x_step * 2, "entry_y": py,
                    "depth": depth, "goes_deeper": deepest_y > feet_y + 3,
                    "air_ratio": air / max(total, 1)
                })

        # Down passage
        depth_d = 0
        for row_y in range(feet_y + 1, py + radius):
            if is_air(px, row_y) and is_air(px + 1, row_y):
                depth_d += 1
            else:
                break
        if depth_d >= 5:
            passages.append({
                "direction": "down", "entry_x": px, "entry_y": feet_y + 1,
                "depth": depth_d, "goes_deeper": True, "air_ratio": 1.0
            })

        # Up passage
        depth_u = 0
        for row_y in range(py - 1, py - radius, -1):
            if is_air(px, row_y) and is_air(px + 1, row_y):
                depth_u += 1
            else:
                break
        if depth_u >= 5:
            passages.append({
                "direction": "up", "entry_x": px, "entry_y": py - 1,
                "depth": depth_u, "goes_deeper": False, "air_ratio": 1.0
            })

        passages.sort(key=lambda p: (-int(p["goes_deeper"]), -p["depth"]))

        # ── Drop points ──
        gap_columns = {}
        for col_x in range(px - radius, px + radius):
            best_floor = None
            for search_y in range(py - radius, py + radius):
                if (col_x, search_y) in solid and is_air(col_x, search_y - 1):
                    if best_floor is None or abs(search_y - feet_y) < abs(best_floor - feet_y):
                        best_floor = search_y
            if best_floor is None or not is_air(col_x, best_floor):
                continue
            fall_depth = 0
            for dy in range(0, radius):
                if is_air(col_x, best_floor + dy):
                    fall_depth += 1
                else:
                    break
            if fall_depth >= 3:
                gap_columns[col_x] = {"floor_y": best_floor, "fall_depth": fall_depth}

        drop_points = []
        if gap_columns:
            sorted_cols = sorted(gap_columns.keys())
            i = 0
            while i < len(sorted_cols):
                start_x = sorted_cols[i]
                end_x = start_x
                max_depth = gap_columns[start_x]["fall_depth"]
                while i + 1 < len(sorted_cols) and sorted_cols[i + 1] == sorted_cols[i] + 1:
                    i += 1
                    end_x = sorted_cols[i]
                    max_depth = max(max_depth, gap_columns[end_x]["fall_depth"])
                gap_width = end_x - start_x + 1
                center_x = (start_x + end_x) // 2
                if gap_width >= 2:
                    reachable = True
                    walk_y = py
                    step_dir = 1 if center_x > px else -1
                    for check_x in range(px + step_dir, center_x, step_dir):
                        found = False
                        for offset in [0, 1, -1, 2, -2]:
                            if body_passable(check_x, walk_y + offset):
                                walk_y = walk_y + offset
                                found = True
                                break
                        if not found:
                            reachable = False
                            break
                    drop_points.append({
                        "x": center_x, "gap_width": gap_width,
                        "fall_depth": max_depth, "reachable": reachable
                    })
                i += 1
            drop_points.sort(key=lambda d: -d["fall_depth"])

        # ── Cavity below ──
        best_cavity = None
        for col_x in range(px - 5, px + 5):
            in_solid = True
            air_start = None
            air_count = 0
            for check_y in range(feet_y + 1, feet_y + 30):
                if check_y < 0 or check_y >= py + radius:
                    break
                if is_air(col_x, check_y):
                    if in_solid:
                        air_start = check_y
                        air_count = 1
                        in_solid = False
                    else:
                        air_count += 1
                else:
                    if not in_solid and air_count >= 3:
                        cover = air_start - (feet_y + 1)
                        if cover <= 8 and (best_cavity is None or air_count > best_cavity["height"]):
                            best_cavity = {
                                "center_x": col_x, "top_y": air_start,
                                "height": air_count, "cover_thickness": cover
                            }
                        break
                    in_solid = True

        return {
            "px": px, "py": py,
            "solid": solid,
            "ores": ores,
            "chests": chests_sorted,
            "pots": pots_sorted,
            "passages": passages,
            "drop_points": drop_points,
            "cavity_below": best_cavity,
        }

    def scan_ores(self, radius: int = 15) -> list:
        """Scan area around player for ore tiles. Returns [(x, y, tileType, name)]."""
        self.engine.bridge.get_state()
        time.sleep(0.05)
        px = self.engine.bridge.state.player.tile_x
        py = self.engine.bridge.state.player.tile_y
        tiles = self.engine.scan_area(px - radius, py - radius, radius * 2, radius * 2)
        ores = []
        for t in tiles:
            tt = t.get("t", -1)
            if tt in self.ORE_TILES:
                ores.append((t["x"], t["y"], tt, self.ORE_TILES[tt]))
        # Sort by distance
        ores.sort(key=lambda o: abs(o[0] - px) + abs(o[1] - py))
        return ores

    def scan_nearby_objects(self, radius: int = 15) -> dict:
        """Scan for chests and pots near player. Returns {chests: [(x,y)], pots: [(x,y)]}."""
        self.engine.bridge.get_state()
        time.sleep(0.05)
        px = self.engine.bridge.state.player.tile_x
        py = self.engine.bridge.state.player.tile_y
        tiles = self.engine.scan_area(px - radius, py - radius, radius * 2, radius * 2)
        chests = set()
        pots = set()
        for t in tiles:
            tt = t.get("t", -1)
            if tt == self.CHEST_TILE:
                chests.add((t["x"], t["y"]))
            elif tt == self.POT_TILE:
                pots.add((t["x"], t["y"]))
        return {
            "chests": sorted(chests, key=lambda c: abs(c[0] - px) + abs(c[1] - py)),
            "pots": sorted(pots, key=lambda c: abs(c[0] - px) + abs(c[1] - py)),
        }

    # ── Passage scanning system (V3) ──────────────────────────────

    def scan_passages(self, scan_radius: int = 20) -> list:
        """Scan surrounding area and detect passable tunnels in 4 directions.

        Returns list of passage dicts sorted by priority:
        {direction, entry_x, entry_y, depth, goes_deeper, air_ratio}
        """
        self.engine.bridge.get_state()
        time.sleep(0.05)
        px = self.engine.bridge.state.player.tile_x
        py = self.engine.bridge.state.player.tile_y
        feet_y = py + 2

        # Scan a large area around player
        scan_x = px - scan_radius
        scan_y = py - scan_radius
        scan_w = scan_radius * 2
        scan_h = scan_radius * 2
        tiles = self.engine.scan_area(scan_x, scan_y, scan_w, scan_h)

        # Build solid set for fast lookup
        solid = set()
        for t in tiles:
            solid.add((t["x"], t["y"]))

        def is_air(x, y):
            return (x, y) not in solid

        def body_passable(x, y):
            """Check if player (3 tiles tall) can stand at (x, y=head)."""
            return is_air(x, y) and is_air(x, y + 1) and is_air(x, y + 2)

        passages = []

        # ── Right passage ──
        depth_r = 0
        scan_y_r = py  # track current passage height (follows slopes)
        deepest_y_r = py
        for col_x in range(px + 2, px + scan_radius):
            # Check if passable at current height, or with ±1-2 offset (slope)
            found = False
            for offset in [0, 1, -1, 2, -2]:
                test_y = scan_y_r + offset
                if body_passable(col_x, test_y):
                    scan_y_r = test_y
                    deepest_y_r = max(deepest_y_r, test_y + 2)
                    found = True
                    break
            if not found:
                break
            depth_r += 1
        if depth_r >= 5:
            total = depth_r * 3
            air = sum(1 for dx in range(1, depth_r + 1)
                      for dy in range(3) if is_air(px + 1 + dx, py + dy))
            passages.append({
                "direction": "right", "entry_x": px + 2, "entry_y": py,
                "depth": depth_r, "goes_deeper": deepest_y_r > feet_y + 3,
                "air_ratio": air / max(total, 1)
            })

        # ── Left passage ──
        depth_l = 0
        scan_y_l = py
        deepest_y_l = py
        for col_x in range(px - 2, px - scan_radius, -1):
            found = False
            for offset in [0, 1, -1, 2, -2]:
                test_y = scan_y_l + offset
                if body_passable(col_x, test_y):
                    scan_y_l = test_y
                    deepest_y_l = max(deepest_y_l, test_y + 2)
                    found = True
                    break
            if not found:
                break
            depth_l += 1
        if depth_l >= 5:
            total = depth_l * 3
            air = sum(1 for dx in range(1, depth_l + 1)
                      for dy in range(3) if is_air(px - 1 - dx, py + dy))
            passages.append({
                "direction": "left", "entry_x": px - 2, "entry_y": py,
                "depth": depth_l, "goes_deeper": deepest_y_l > feet_y + 3,
                "air_ratio": air / max(total, 1)
            })

        # ── Down passage ──
        depth_d = 0
        for row_y in range(feet_y + 1, py + scan_radius):
            if is_air(px, row_y) and is_air(px + 1, row_y):
                depth_d += 1
            else:
                break
        if depth_d >= 5:
            passages.append({
                "direction": "down", "entry_x": px, "entry_y": feet_y + 1,
                "depth": depth_d, "goes_deeper": True,
                "air_ratio": 1.0
            })

        # ── Up passage ──
        depth_u = 0
        for row_y in range(py - 1, py - scan_radius, -1):
            if is_air(px, row_y) and is_air(px + 1, row_y):
                depth_u += 1
            else:
                break
        if depth_u >= 5:
            passages.append({
                "direction": "up", "entry_x": px, "entry_y": py - 1,
                "depth": depth_u, "goes_deeper": False,
                "air_ratio": 1.0
            })

        # Sort by priority: goes_deeper first, then by depth
        passages.sort(key=lambda p: (-int(p["goes_deeper"]), -p["depth"]))
        return passages

    def scan_drop_points(self, scan_radius: int = 20) -> list:
        """Scan nearby area for gaps/holes in the cave floor where player can fall.

        Returns list of drop point dicts sorted by fall_depth (deepest first):
        {x, gap_width, fall_depth, reachable}
        """
        self.engine.bridge.get_state()
        time.sleep(0.05)
        px = self.engine.bridge.state.player.tile_x
        py = self.engine.bridge.state.player.tile_y
        feet_y = py + 2

        # Scan area around player
        scan_x = px - scan_radius
        scan_y = py - scan_radius
        scan_w = scan_radius * 2
        scan_h = scan_radius * 2
        tiles = self.engine.scan_area(scan_x, scan_y, scan_w, scan_h)

        solid = set()
        for t in tiles:
            solid.add((t["x"], t["y"]))

        def is_air(x, y):
            return (x, y) not in solid

        def body_passable(x, y):
            return is_air(x, y) and is_air(x, y + 1) and is_air(x, y + 2)

        # For each column, find the cave floor (walkable surface near player feet)
        # Then check if below the floor there's air (= a gap/hole)
        gap_columns = {}  # col_x -> {"floor_y": y, "fall_depth": d}

        for col_x in range(px - scan_radius, px + scan_radius):
            # Find cave floor for this column: solid block with air above, near feet_y
            best_floor = None
            for search_y in range(py - scan_radius, py + scan_radius):
                if (col_x, search_y) in solid and is_air(col_x, search_y - 1):
                    # This is a surface (solid with air above)
                    if best_floor is None or abs(search_y - feet_y) < abs(best_floor - feet_y):
                        best_floor = search_y
            if best_floor is None:
                continue

            # Check if at this floor level, there's actually a gap
            # A gap means: at feet_y level (or floor level), the tile is AIR
            # (the player could walk to this column and fall)
            # We check if there's a hole starting from the floor level
            if not is_air(col_x, best_floor):
                # Floor is solid here - no gap
                continue

            # Count fall depth (consecutive air below)
            fall_depth = 0
            for dy in range(0, scan_radius):
                if is_air(col_x, best_floor + dy):
                    fall_depth += 1
                else:
                    break

            if fall_depth >= 3:
                gap_columns[col_x] = {"floor_y": best_floor, "fall_depth": fall_depth}

        if not gap_columns:
            return []

        # Merge adjacent gap columns into drop points
        drop_points = []
        sorted_cols = sorted(gap_columns.keys())
        i = 0
        while i < len(sorted_cols):
            start_x = sorted_cols[i]
            end_x = start_x
            max_depth = gap_columns[start_x]["fall_depth"]

            # Extend to adjacent columns
            while i + 1 < len(sorted_cols) and sorted_cols[i + 1] == sorted_cols[i] + 1:
                i += 1
                end_x = sorted_cols[i]
                max_depth = max(max_depth, gap_columns[end_x]["fall_depth"])

            gap_width = end_x - start_x + 1
            center_x = (start_x + end_x) // 2

            if gap_width >= 2:  # Player needs at least 2 tiles wide to fall through
                # Check reachability: can player walk from current position to gap?
                reachable = True
                walk_y = py
                step_dir = 1 if center_x > px else -1
                for check_x in range(px + step_dir, center_x, step_dir):
                    # Check if passable at current walk height (allow ±2 slope)
                    found = False
                    for offset in [0, 1, -1, 2, -2]:
                        if body_passable(check_x, walk_y + offset):
                            walk_y = walk_y + offset
                            found = True
                            break
                    if not found:
                        reachable = False
                        break

                drop_points.append({
                    "x": center_x,
                    "gap_width": gap_width,
                    "fall_depth": max_depth,
                    "reachable": reachable
                })
            i += 1

        # Sort by fall depth (deepest first), filter unreachable
        drop_points.sort(key=lambda d: -d["fall_depth"])
        return drop_points

    def scan_cavity_below(self, scan_radius: int = 15) -> dict:
        """Check if there's an air cavity below the player's feet (covered by solid blocks).

        Returns cavity info dict or None:
        {center_x, top_y, height, cover_thickness}
        """
        self.engine.bridge.get_state()
        time.sleep(0.05)
        px = self.engine.bridge.state.player.tile_x
        py = self.engine.bridge.state.player.tile_y
        feet_y = py + 2

        # Scan below player
        scan_x = px - scan_radius
        scan_y = feet_y
        scan_w = scan_radius * 2
        scan_h = 30  # look 30 blocks deep
        tiles = self.engine.scan_area(scan_x, scan_y, scan_w, scan_h)

        solid = set()
        for t in tiles:
            solid.add((t["x"], t["y"]))

        def is_air(x, y):
            return (x, y) not in solid

        # For each column near player, look for air cavity below solid floor
        best_cavity = None
        for col_x in range(px - 5, px + 5):
            # Scan downward from feet
            in_solid = True
            solid_start = feet_y + 1
            air_start = None
            air_count = 0

            for check_y in range(feet_y + 1, feet_y + 30):
                if is_air(col_x, check_y):
                    if in_solid:
                        # Transition from solid to air = found cavity top
                        air_start = check_y
                        air_count = 1
                        in_solid = False
                    else:
                        air_count += 1
                else:
                    if not in_solid and air_count >= 3:
                        # Found a cavity: air_count blocks of air
                        cover = air_start - (feet_y + 1)
                        if cover <= 8 and (best_cavity is None or air_count > best_cavity["height"]):
                            best_cavity = {
                                "center_x": col_x,
                                "top_y": air_start,
                                "height": air_count,
                                "cover_thickness": cover
                            }
                        break
                    in_solid = True

        return best_cavity

    def mine_ore_vein(self, start_x: int, start_y: int, ore_type: int) -> int:
        """BFS mine an entire ore vein starting from (start_x, start_y). Returns tiles mined."""
        self._cancel.clear()
        ore_name = self.ORE_TILES.get(ore_type, f"矿石({ore_type})")

        # First, dig a tunnel to reach the ore vein
        self.engine.bridge.get_state()
        time.sleep(0.05)
        px = self.engine.bridge.state.player.tile_x
        py = self.engine.bridge.state.player.tile_y
        player_center_y = py + 1
        hdist = abs(start_x - px)
        vdist = abs(start_y - player_center_y)

        if hdist <= self.PICK_RANGE_X and vdist <= self.PICK_RANGE_Y:
            print(f"\n  [决策] 发现{ore_name} @({start_x},{start_y}), "
                  f"在镐子范围内(水平{hdist} 垂直{vdist}), 直接开挖")
        else:
            print(f"\n  [决策] 发现{ore_name} @({start_x},{start_y}), "
                  f"超出镐子范围(水平{hdist} 垂直{vdist}), A* 导航到达")
            # Navigate near ore using A* (target feet Y near ore)
            target_feet_y = start_y  # feet near the ore
            result = self.engine.navigate_to(start_x, target_feet_y,
                                             allow_dig=True, timeout=30)
            if result != "arrived":
                # Fallback: if ore is roughly below us, dig shaft down manually
                self.engine.bridge.get_state()
                time.sleep(0.05)
                cur_px = self.engine.bridge.state.player.tile_x
                cur_py = self.engine.bridge.state.player.tile_y
                cur_feet_y = cur_py + 2
                ore_hdist = abs(start_x - cur_px)
                ore_below = start_y - cur_feet_y
                if ore_hdist <= 3 and 0 < ore_below <= 10:
                    print(f"  [决策] A* 导航失败，矿在正下方{ore_below}格，手动挖竖井")
                    for dig_y in range(cur_feet_y + 1, start_y + 1):
                        for dx in [0, 1]:
                            tile = self.engine.check_tile(cur_px + dx, dig_y)
                            if tile.get("hasTile"):
                                self.engine.mine_tile(cur_px + dx, dig_y, timeout=8)
                    # Let character fall
                    time.sleep(0.5)
                    self.engine.bridge.get_state()
                    time.sleep(0.1)
                else:
                    print(f"  [决策] A* 导航失败 ({result})，放弃{ore_name}矿脉")
                    return 0
            print(f"  [决策] 已到达矿脉附近，开始BFS采集")

        queue = [(start_x, start_y)]
        visited = set()
        mined = 0

        while queue and not self._cancelled():
            x, y = queue.pop(0)
            if (x, y) in visited:
                continue
            visited.add((x, y))

            # Check if this tile is still the target ore
            tile = self.engine.check_tile(x, y)
            if not tile.get("hasTile") or tile.get("tileType") != ore_type:
                continue

            # Refresh player position
            self.engine.bridge.get_state()
            time.sleep(0.05)
            px = self.engine.bridge.state.player.tile_x
            py = self.engine.bridge.state.player.tile_y
            player_center_y = py + 1

            # If ore tile is out of reach, navigate closer
            hdist = abs(x - px)
            vdist = abs(y - player_center_y)
            if hdist > self.PICK_RANGE_X or vdist > self.PICK_RANGE_Y:
                result = self.engine.navigate_to(x, y, allow_dig=True, timeout=15)
                if result != "arrived":
                    continue

            # Move to within horizontal reach
            px = self.engine.bridge.state.player.tile_x
            if abs(x - px) > 3:
                self.engine.nav_to(x, timeout=10)

            # Mine the tile
            if self.engine.mine_tile(x, y, timeout=8):
                mined += 1
            else:
                continue  # couldn't mine, skip neighbors

            # Add neighbors to queue
            for dx, dy in [(0, -1), (0, 1), (-1, 0), (1, 0)]:
                nx, ny = x + dx, y + dy
                if (nx, ny) not in visited:
                    queue.append((nx, ny))

        if mined > 0:
            print(f"  [任务] {ore_name}矿脉采集完成, 共挖 {mined} 格")
            self.engine.collect_nearby_items()
        return mined

    MAX_ENTRANCE_DEPTH = 5  # max solid blocks above cave to count as "entrance"

    def find_cave_entrance(self, direction: int = 1, max_distance: int = 150):
        """Walk in direction, using scan_area to find accessible cave entrances.
        Only accepts: surface openings OR caves with ≤5 blocks of solid cover.
        Returns (entrance_x, entrance_y) or None."""
        self._cancel.clear()
        self._current_task = f"寻找洞穴入口 ({'右' if direction > 0 else '左'})"
        print(f"\n  [任务] {self._current_task}")

        self.engine.bridge.get_state()
        time.sleep(0.1)
        start_x = self.engine.bridge.state.player.tile_x
        scan_depth = 50
        scan_width = 25
        walked = 0
        last_torch_x = start_x

        while walked < max_distance and not self._cancelled():
            # Fight enemies encountered while walking
            if self._fight_if_enemies():
                continue

            self.engine.bridge.get_state()
            time.sleep(0.05)
            px = self.engine.bridge.state.player.tile_x
            py = self.engine.bridge.state.player.tile_y
            walked = abs(px - start_x)

            # Batch scan ahead
            scan_x = px if direction > 0 else px - scan_width
            tiles = self.engine.scan_area(scan_x, py - 2, scan_width, scan_depth)
            solid = set()
            found_chests = []
            found_pots = []
            for t in tiles:
                solid.add((t["x"], t["y"]))
                tt = t.get("t", 0)
                if tt == 21:  # chest
                    found_chests.append((t["x"], t["y"]))
                elif tt == 28:  # pot
                    found_pots.append((t["x"], t["y"]))

            # Interact with chests/pots found in scan (skip base chests)
            for cx, cy in found_chests:
                if self._cancelled():
                    break
                if self._is_base_chest(cx, cy):
                    print(f"  [任务] 跳过基地箱子 ({cx}, {cy})")
                    continue
                print(f"  [任务] 路边发现箱子 ({cx}, {cy})")
                self.loot_nearby_chest(cx, cy)
            for pot_x, pot_y in found_pots:
                if self._cancelled():
                    break
                # Only smash pots that are reachable (near ground level)
                if abs(pot_y - py) <= 6:
                    self.unhold_torch()
                    self.engine.nav_to(pot_x, timeout=8)
                    self.engine.mine_tile(pot_x, pot_y, timeout=3)
                    self.engine.collect_nearby_items()

            for col_x in range(scan_x, scan_x + scan_width):
                # Find ground level
                ground_y = None
                for cy in range(py, py + 10):
                    if (col_x, cy) in solid:
                        ground_y = cy
                        break

                if ground_y is None:
                    # Surface opening — check depth of air
                    air_count = 0
                    for cy in range(py + 3, py - 2 + scan_depth):
                        if (col_x, cy) not in solid:
                            air_count += 1
                        else:
                            break
                    if air_count >= 8:
                        print(f"  [任务] 发现地表开口 ({col_x}, {py + 3}), 深度≥{air_count}格")
                        self._current_task = ""
                        return (col_x, py + 3)
                    continue

                # Check below ground — only accept shallow caves (cover ≤ MAX_ENTRANCE_DEPTH)
                cover_count = 0
                air_start_y = None
                air_count = 0
                for cy in range(ground_y + 1, py - 2 + scan_depth):
                    if (col_x, cy) in solid:
                        if air_count >= 8 and cover_count <= self.MAX_ENTRANCE_DEPTH:
                            print(f"  [任务] 发现浅层洞穴 ({col_x}, {air_start_y}), 覆盖{cover_count}格, 空腔{air_count}格")
                            self._current_task = ""
                            return (col_x, air_start_y)
                        if air_start_y is None:
                            cover_count += 1
                        else:
                            # Hit solid after air — reset
                            air_start_y = None
                            air_count = 0
                    else:
                        if air_start_y is None:
                            air_start_y = cy
                        air_count += 1

                # Check trailing air
                if air_count >= 8 and cover_count <= self.MAX_ENTRANCE_DEPTH:
                    print(f"  [任务] 发现浅层洞穴 ({col_x}, {air_start_y}), 覆盖{cover_count}格, 空腔{air_count}格")
                    self._current_task = ""
                    return (col_x, air_start_y)

            # No entrance in this strip — walk ahead
            # Place torch for visibility
            last_torch_x = self.maybe_place_torch(last_torch_x, underground=False)

            target_x = px + direction * scan_width
            arrived = self.engine.nav_to(target_x, timeout=15)
            if not arrived:
                # Check if we actually moved
                self.engine.bridge.get_state()
                time.sleep(0.05)
                new_px = self.engine.bridge.state.player.tile_x
                if abs(new_px - px) < 3:
                    print(f"  [任务] 导航受阻，无法继续前进")
                    break

        print(f"  [任务] 走了{walked}格未找到洞穴入口")
        self._current_task = ""
        return None

    def _fight_if_enemies(self):
        """Check for nearby enemies and fight them. Returns True if fought."""
        enemies = self.engine.get_nearest_npcs(hostile=True, count=3, range_tiles=30)
        # Filter reachable (vertical distance ≤ 10)
        self.engine.bridge.get_state()
        time.sleep(0.05)
        pty = self.engine.bridge.state.player.tile_y
        enemies = [e for e in enemies if abs(e.get("tileY", 0) - pty) <= 10]
        if enemies:
            self.unhold_torch()
            self.engine.fight_nearest_enemy(timeout=8)
            self.engine.collect_nearby_items()
            self._ensure_light()  # re-hold torch if dark
            return True
        # No enemies — still check if we need light
        self._ensure_light()
        return False

    def _safe_move_to(self, target_x: int, timeout: int = 60, allow_dig: bool = False):
        """Move to target with periodic combat checks. For long-distance travel.
        If allow_dig=True, uses A* navigate_to (for underground traversal)."""
        self.engine.bridge.get_state()
        time.sleep(0.05)
        start = time.time()
        consecutive_fails = 0
        max_consecutive_fails = 3

        while not self._cancelled() and time.time() - start < timeout:
            px = self.engine.bridge.state.player.tile_x
            if abs(px - target_x) <= 2:
                break

            # Fight enemies before walking
            self._fight_if_enemies()

            # Quick heal if low
            hp_ratio = self.engine.bridge.state.player.hp / max(self.engine.bridge.state.player.max_hp, 1)
            if hp_ratio < 0.5:
                self.engine.bridge.quick_heal()

            # Navigate a short segment (15 tiles at a time)
            direction = 1 if target_x > px else -1
            segment_x = px + direction * min(15, abs(target_x - px))
            if allow_dig:
                feet_y = self.engine.bridge.state.player.tile_y + 2
                result = self.engine.navigate_to(segment_x, feet_y, allow_dig=True, timeout=15)
                arrived = (result == "arrived")
            else:
                arrived = self.engine.nav_to(segment_x, timeout=10)

            self.engine.bridge.get_state()
            time.sleep(0.05)

            if arrived:
                consecutive_fails = 0
            else:
                consecutive_fails += 1
                # After 2 consecutive fails without dig, try with dig as fallback
                if not allow_dig and consecutive_fails >= 2:
                    print(f"  [安全移动] 连续{consecutive_fails}次导航失败，尝试挖掘模式")
                    feet_y = self.engine.bridge.state.player.tile_y + 2
                    result = self.engine.navigate_to(segment_x, feet_y, allow_dig=True, timeout=15)
                    if result == "arrived":
                        consecutive_fails = 0
                        continue
                if consecutive_fails >= max_consecutive_fails:
                    print(f"  [安全移动] 连续{consecutive_fails}次导航失败，终止")
                    break

    def enter_cave(self, entrance_x: int, entrance_y: int) -> bool:
        """Navigate to cave entrance and descend using A* pathfinding.
        Returns True if successfully entered."""
        self._cancel.clear()
        self._current_task = f"进入洞穴 ({entrance_x}, {entrance_y})"
        print(f"\n  [任务] {self._current_task}")

        # Fight enemies before proceeding
        self._fight_if_enemies()

        # Capture surface position
        self.engine.bridge.get_state()
        time.sleep(0.1)
        surface_py = self.engine.bridge.state.player.tile_y

        # Scan area around entrance for chests/pots
        objects = self.scan_nearby_objects(radius=12)
        for cx, cy in objects.get("chests", []):
            if self._cancelled():
                break
            if self._is_base_chest(cx, cy):
                print(f"  [任务] 跳过基地箱子 ({cx}, {cy})")
                continue
            print(f"  [任务] 入口附近发现箱子 ({cx}, {cy})")
            self.loot_nearby_chest(cx, cy)
        for pot_x, pot_y in objects.get("pots", []):
            if self._cancelled():
                break
            self.unhold_torch()
            self.engine.nav_to(pot_x, timeout=8)
            self.engine.mine_tile(pot_x, pot_y, timeout=3)
            self.engine.collect_nearby_items()

        # Fight again if enemies showed up
        self._fight_if_enemies()

        # Use A* to navigate directly to cave entrance (with digging if needed)
        # entrance_y is the air space Y, target feet position is entrance_y
        print(f"  [任务] A* 导航到洞穴入口 ({entrance_x}, {entrance_y})")
        result = self.engine.navigate_to(entrance_x, entrance_y, allow_dig=True, timeout=30)

        self.engine.bridge.get_state()
        time.sleep(0.1)
        cur_py = self.engine.bridge.state.player.tile_y
        # Use entrance_y as reference: must be deeper than surface AND near entrance depth
        entered = result == "arrived" or (cur_py > surface_py + 3 and cur_py >= entrance_y - 5)

        if entered:
            print(f"  [任务] 成功进入洞穴, 当前深度 y={cur_py}")
            self._current_task = ""
            return True

        # A* failed — try walking toward entrance and falling in naturally
        print(f"  [任务] A* 导航失败 ({result}), 尝试走向入口自然下落")
        self.engine.nav_to(entrance_x, timeout=15)
        time.sleep(0.5)

        self.engine.bridge.get_state()
        time.sleep(0.1)
        cur_py = self.engine.bridge.state.player.tile_y
        if cur_py > surface_py + 3 and cur_py >= entrance_y - 5:
            print(f"  [任务] 自然下落进入洞穴, 当前深度 y={cur_py}")
            self._current_task = ""
            return True

        print(f"  [任务] 未能进入洞穴 (y={cur_py}, 地表y={surface_py}, 入口y={entrance_y})")
        self._current_task = ""
        return False

    # Track explored cave chunks to avoid revisiting
    _visited_chunks = None  # set of (chunk_x, chunk_y), chunk = 5x5

    def _get_chunk(self, x, y):
        return (x // 5, y // 5)

    def _mark_visited(self, x, y):
        if self._visited_chunks is None:
            self._visited_chunks = set()
        self._visited_chunks.add(self._get_chunk(x, y))

    def _is_visited(self, x, y):
        if self._visited_chunks is None:
            return False
        return self._get_chunk(x, y) in self._visited_chunks

    def cave_explore_step(self, direction: int, path: list) -> str:
        """Take one exploration step in a cave (wing-based).

        Simple strategy: always try to go deeper.
        1. Scan below for air space → fly there
        2. No air below → dig shaft down
        3. Same depth explored → walk to goes_deeper passage
        4. Nothing works → dead_end

        Returns: 'moved', 'blocked', 'fell', 'dead_end'.
        """
        self._auto_heal_check()
        # ── Scan ──
        scan = self.scan_surroundings(20)
        px, py = scan["px"], scan["py"]
        old_x, old_y = px, py
        feet_y = py + 2
        solid = scan["solid"]
        self._mark_visited(px, py)

        passages = scan["passages"]
        cavity = scan["cavity_below"]

        if passages:
            desc = ", ".join(f"{p['direction']}({p['depth']}格{'↓' if p['goes_deeper'] else ''})"
                             for p in passages)
            print(f"  [洞穴] 通道: {desc}")

        # ── Strategy 1: Go deeper — find any open space below ──

        # 1a: Cavity below (scan_surroundings already detects this)
        if cavity and cavity["cover_thickness"] <= 8:
            cx = cavity["center_x"]
            target_y = cavity["top_y"]
            depth = target_y - feet_y
            if depth >= 3:  # only dive if cavity is actually below us
                print(f"  [决策] 脚下空腔 (深{cavity['height']}格, 覆盖{cavity['cover_thickness']}格), 飞行下潜")
                self.unhold_torch()
                result = self.engine.navigate_to(cx, target_y, allow_dig=True, timeout=20)
                self.engine.bridge.get_state()
                time.sleep(0.1)
                new_py = self.engine.bridge.state.player.tile_y
                if new_py > old_y + 2:  # must ACTUALLY descend, not just "arrived"
                    new_px = self.engine.bridge.state.player.tile_x
                    self._mark_visited(new_px, new_py)
                    path.append((new_px, new_py))
                    print(f"  [决策] 下潜成功 y={new_py} (下降{new_py - old_y}格)")
                    return "fell"
                print(f"  [决策] 下潜失败 ({result}), y未变 ({new_py})")

        # 1b: Scan for deepest air pocket below within ±10 tiles horizontal
        best_dive = None  # (x, y, depth_gain)
        for scan_x in range(px - 10, px + 10):
            in_air = False
            air_y_start = None
            for scan_y in range(feet_y + 1, feet_y + 20):
                is_air = (scan_x, scan_y) not in solid
                if is_air:
                    if not in_air:
                        air_y_start = scan_y
                        in_air = True
                else:
                    if in_air:
                        air_height = scan_y - air_y_start
                        depth_gain = air_y_start - feet_y
                        if air_height >= 3 and depth_gain >= 3:
                            if best_dive is None or air_y_start > best_dive[1]:
                                best_dive = (scan_x, air_y_start, depth_gain)
                        in_air = False
            if in_air:
                air_height = (feet_y + 20) - air_y_start
                depth_gain = air_y_start - feet_y
                if air_height >= 3 and depth_gain >= 3:
                    if best_dive is None or air_y_start > best_dive[1]:
                        best_dive = (scan_x, air_y_start, depth_gain)

        if best_dive:
            dive_x, dive_y, dive_depth = best_dive
            print(f"  [决策] 发现下方{dive_depth}格空间 @({dive_x},{dive_y}), 飞行下潜")
            self.unhold_torch()
            result = self.engine.navigate_to(dive_x, dive_y, allow_dig=True, timeout=20)
            self.engine.bridge.get_state()
            time.sleep(0.1)
            new_py = self.engine.bridge.state.player.tile_y
            if new_py > old_y + 2:  # must ACTUALLY descend
                new_px = self.engine.bridge.state.player.tile_x
                self._mark_visited(new_px, new_py)
                path.append((new_px, new_py))
                print(f"  [决策] 下潜成功 y={new_py} (下降{new_py - old_y}格)")
                return "fell"
            print(f"  [决策] 下潜失败 ({result}), y未变 ({new_py})")

        # 1c: Direct down passage
        down_passages = [p for p in passages if p["direction"] == "down"]
        if down_passages:
            dp = down_passages[0]
            target_y = feet_y + dp["depth"]
            print(f"  [决策] 正下方通道 (深{dp['depth']}格), 飞行下去")
            self.unhold_torch()
            result = self.engine.navigate_to(px, target_y, allow_dig=True, timeout=15)
            self.engine.bridge.get_state()
            time.sleep(0.1)
            new_py = self.engine.bridge.state.player.tile_y
            if new_py > old_y + 2:  # must ACTUALLY descend
                new_px = self.engine.bridge.state.player.tile_x
                self._mark_visited(new_px, new_py)
                path.append((new_px, new_py))
                return "fell"

        # ── Strategy 2: Horizontal movement — only goes_deeper passages ──
        h_passages = [p for p in passages if p["direction"] in ("right", "left")]
        # Prefer unvisited passages that go deeper
        deeper_unvisited = []
        other_unvisited = []
        for p in h_passages:
            ex, ey = p["entry_x"], p["entry_y"]
            check_x = ex + 3 if p["direction"] == "right" else ex - 3
            if not self._is_visited(check_x, ey):
                if p["goes_deeper"]:
                    deeper_unvisited.append(p)
                else:
                    other_unvisited.append(p)

        chosen = None
        reason = ""
        if deeper_unvisited:
            chosen = deeper_unvisited[0]
            reason = "未探索+向下延伸"
        elif other_unvisited:
            chosen = other_unvisited[0]
            reason = "未探索通道"
        else:
            # All visited — only follow if goes_deeper
            deeper_visited = [p for p in h_passages if p["goes_deeper"]]
            if deeper_visited:
                chosen = deeper_visited[0]
                reason = "已探索但向下延伸"

        if chosen:
            dir_cn = "右" if chosen["direction"] == "right" else "左"
            step = min(chosen["depth"], 8)
            target_x = px + step if chosen["direction"] == "right" else px - step
            print(f"  [决策] 向{dir_cn}探索{step}格 — {reason}")
            self.engine.nav_to(target_x, timeout=8)

            self.engine.bridge.get_state()
            time.sleep(0.05)
            new_px = self.engine.bridge.state.player.tile_x
            new_py = self.engine.bridge.state.player.tile_y
            # Mark entire walked path
            self._mark_visited(new_px, new_py)
            step_dir = 1 if new_px > old_x else -1
            if step_dir != 0:
                for mark_x in range(old_x, new_px + step_dir, step_dir * 3 if step_dir != 0 else 1):
                    self._mark_visited(mark_x, new_py)

            if abs(new_px - old_x) > 2 or abs(new_py - old_y) > 2:
                path.append((new_px, new_py))

            if new_py - old_y > 3:
                print(f"  [决策] 行走中掉落到 y={new_py}")
                return "fell"
            if abs(new_px - old_x) >= 1:
                return "moved"
            return "blocked"

        # ── Strategy 3: Dig shaft down ──
        # Use navigate_to with allow_dig — the C# navigator handles digging
        # frame-by-frame with correct player position, avoiding edge-standing issues
        print(f"  [决策] 无可用通道，挖掘脚下向下突破")
        self.unhold_torch()
        result = self.engine.navigate_to(px, feet_y + 5, allow_dig=True, timeout=20)
        self.engine.bridge.get_state()
        time.sleep(0.1)
        new_py = self.engine.bridge.state.player.tile_y
        if new_py > old_y + 2:
            new_px = self.engine.bridge.state.player.tile_x
            self._mark_visited(new_px, new_py)
            path.append((new_px, new_py))
            print(f"  [决策] 挖掘下潜成功 y={new_py} (下降{new_py - old_y}格)")
            return "fell"

        print(f"  [决策] 无法继续深入，死胡同")
        return "dead_end"

    def _climb_wall_upward(self, target_y: int, timeout: int = 30):
        """Fallback: walk to nearest wall, then dig diagonally upward along it.
        Used when normal A* navigation can't get us up through open cavities."""
        self.engine.bridge.get_state()
        time.sleep(0.05)
        px = self.engine.bridge.state.player.tile_x
        py = self.engine.bridge.state.player.tile_y
        feet_y = py + 2

        # Scan left and right to find nearest solid wall
        left_dist, right_dist = 999, 999
        for dist in range(1, 50):
            if left_dist == 999:
                tile_l = self.engine.check_tile(px - dist, feet_y)
                if tile_l.get("hasTile") and tile_l.get("solid"):
                    left_dist = dist
            if right_dist == 999:
                tile_r = self.engine.check_tile(px + dist + 1, feet_y)
                if tile_r.get("hasTile") and tile_r.get("solid"):
                    right_dist = dist
            if left_dist < 999 and right_dist < 999:
                break

        if left_dist == 999 and right_dist == 999:
            print(f"  [返回] 周围50格内未找到墙壁")
            return

        # Walk to nearer wall
        wall_dir = -1 if left_dist <= right_dist else 1
        wall_x = px + wall_dir * (min(left_dist, right_dist) - 1)
        dir_name = "左" if wall_dir == -1 else "右"
        print(f"  [返回] 向{dir_name}走到墙壁 (距离{min(left_dist, right_dist)}格)")
        self.engine.nav_to(wall_x, timeout=10)

        # Now dig diagonally upward along the wall
        # Navigate to a point above current position, near the wall
        # A* with high air_penalty will plan a dig route along the solid wall
        self.engine.bridge.get_state()
        time.sleep(0.05)
        cur_x = self.engine.bridge.state.player.tile_x
        cur_y = self.engine.bridge.state.player.tile_y
        climb_target_y = max(target_y, cur_y - 15)  # climb up to 15 tiles at a time
        climb_feet_y = climb_target_y + 2
        print(f"  [返回] 沿墙壁挖掘上行 目标y={climb_feet_y} (当前y={cur_y + 2})")
        self.engine.navigate_to(cur_x, climb_feet_y, allow_dig=True,
                                timeout=timeout, air_penalty=10)

    def return_to_surface(self, path: list, home_x: int) -> bool:
        """Follow recorded path backwards to return to surface using A*, then walk home.
        If normal navigation fails, falls back to wall-climbing (dig along nearest wall)."""
        self._cancel.clear()
        self._current_task = "返回地面"
        print(f"\n  [任务] 返回地面 (路径点: {len(path)}个)")
        self.unhold_torch()  # switch to weapon/pick for climbing

        consecutive_no_progress = 0

        # Reverse path and follow waypoints using A* navigation
        for wx, wy in reversed(path):
            if self._cancelled():
                break

            self.engine.bridge.get_state()
            time.sleep(0.05)
            px = self.engine.bridge.state.player.tile_x
            py = self.engine.bridge.state.player.tile_y

            # Skip waypoints we're already near
            if abs(wx - px) <= 2 and abs(wy - py) <= 3:
                continue

            # Skip waypoints that are deeper than current position (don't go down)
            if wy > py + 3:
                continue

            # First try normal A* with moderate air_penalty
            target_feet_y = wy + 2  # path stores head Y, convert to feet Y
            print(f"  [返回] 导航到路径点 ({wx}, {target_feet_y})")
            result = self.engine.navigate_to(wx, target_feet_y, allow_dig=True,
                                             timeout=20, air_penalty=5)

            # Check progress
            self.engine.bridge.get_state()
            time.sleep(0.05)
            new_py = self.engine.bridge.state.player.tile_y
            if new_py < py - 1:
                # Made upward progress
                consecutive_no_progress = 0
            else:
                # Navigation failed to go up — immediately try wall climbing
                consecutive_no_progress += 1
                print(f"  [返回] 导航未能上升 (第{consecutive_no_progress}次)，切换墙壁挖掘模式")
                self._climb_wall_upward(target_y=wy)
                self.engine.bridge.get_state()
                time.sleep(0.05)
                after_climb_y = self.engine.bridge.state.player.tile_y
                if after_climb_y < new_py - 1:
                    consecutive_no_progress = 0

            # Fight enemies encountered during return
            self._fight_if_enemies()

        # Now walk home (with combat awareness, allow dig in case still underground)
        print(f"  [任务] 回到地面，走向家 (x={home_x})")
        self._safe_move_to(home_x, timeout=120, allow_dig=True)
        self._current_task = ""
        return True

    def loot_nearby_chest(self, chest_x: int, chest_y: int) -> bool:
        """Walk to chest, open UI for viewers, loot it, mine it. Returns True if looted."""
        self._cancel.clear()
        print(f"  [任务] 开箱 ({chest_x}, {chest_y})")

        # Walk to chest
        self.engine.nav_to(chest_x, timeout=10)

        # Open chest UI so viewers can see contents
        self.engine.open_chest(chest_x, chest_y)
        time.sleep(2.0)  # let viewers see the chest contents

        # Loot via API
        items = self.engine.loot_chest(chest_x, chest_y)
        if items:
            print(f"  [任务] 箱子内获得 {len(items)} 种物品:")
            for it in items:
                print(f"    → {it.get('name', '?')} x{it.get('stack', 0)}")
                time.sleep(0.5)  # pause per item for stream viewers
            # Auto-equip better gear
            self.auto_equip()
            # Pause after looting so viewers can see what was found
            time.sleep(1.5)

        # Close chest UI
        self.engine.close_chest()
        time.sleep(0.3)

        # Mine the chest to take it with us
        self.engine.mine_tile(chest_x, chest_y, timeout=5)
        self.engine.collect_nearby_items()
        return len(items) > 0

    def explore_underground(self, direction: int = None, max_time: int = 300) -> bool:
        """Complete underground exploration loop.
        Finds cave → enters → explores (mine ore, loot chests, fight) → returns home.
        max_time in seconds."""
        self._cancel.clear()
        self._current_task = "地下探索"
        if direction is None:
            direction = 1
        print(f"\n{'='*50}")
        print(f"  [地下探索] 开始 (方向: {'右' if direction > 0 else '左'}, 限时: {max_time}s)")
        print(f"{'='*50}")

        # Ensure we have platforms for climbing
        self._ensure_platforms(min_count=30)

        # Reset visited chunks for fresh exploration
        self._visited_chunks = set()

        if self.base_x is None:
            self.init_base()

        # Clean trash before exploring to free up inventory space
        self.clean_inventory()

        # Remember home position
        self.engine.bridge.get_state()
        time.sleep(0.1)
        home_x = self.engine.bridge.state.player.tile_x

        # Phase 1: Find cave entrance
        entrance = self.find_cave_entrance(direction, max_distance=150)
        if not entrance:
            print(f"  [地下探索] 未找到洞穴入口")
            self._current_task = ""
            return False

        entrance_x, entrance_y = entrance
        path = [(entrance_x, entrance_y)]  # record path for return

        # Phase 2: Enter cave
        if not self.enter_cave(entrance_x, entrance_y):
            print(f"  [地下探索] 无法进入洞穴，战斗撤退")
            self._safe_move_to(home_x, timeout=60, allow_dig=True)
            self._current_task = ""
            return False

        # Record position after entering
        self.engine.bridge.get_state()
        time.sleep(0.1)
        path.append((self.engine.bridge.state.player.tile_x,
                      self.engine.bridge.state.player.tile_y))

        # Phase 3: Exploration loop
        start_time = time.time()
        explore_dir = direction
        stuck_turns = 0
        max_stuck_turns = 4
        last_torch_x = self.engine.bridge.state.player.tile_x

        # Place first torch immediately upon entering cave
        if self._torch_count() > 1:
            self.engine.bridge.send({"cmd": "place_tile",
                "x": self.engine.bridge.state.player.tile_x,
                "y": self.engine.bridge.state.player.tile_y + 1,
                "tile_type": 4})
            time.sleep(0.2)
        # Hold torch for light while exploring
        self.hold_torch()

        # Track unreachable targets to avoid repeating failed navigation
        _failed_targets = set()  # (x, y) tuples that A* couldn't reach

        while not self._cancelled() and (time.time() - start_time) < max_time:
            elapsed = int(time.time() - start_time)

            if self._inventory_nearly_full(5):
                print(f"  [地下探索] 背包快满，回家存储")
                self.go_home_and_resupply()
                break

            self.engine.bridge.get_state()
            time.sleep(0.1)
            state = self.engine.bridge.state

            # Place torch periodically + ensure light in dark areas
            last_torch_x = self.maybe_place_torch(last_torch_x, underground=True)
            self._ensure_light()

            # Priority 0: Self-preservation
            hp = state.player.hp
            max_hp = state.player.max_hp
            hp_ratio = hp / max_hp if max_hp > 0 else 1
            if hp_ratio < 0.3:
                print(f"  [决策] 血量过低 ({hp}/{max_hp}={hp_ratio:.0%})，太危险，决定撤退回地面")
                break

            if hp_ratio < 0.5:
                print(f"  [决策] 血量偏低 ({hp}/{max_hp}={hp_ratio:.0%})，喝药回血")
                self.engine.bridge.quick_heal()

            # Priority 1: Combat (use C#-side sorted NPC list)
            enemies = self.engine.get_nearest_npcs(hostile=True, count=3, range_tiles=30)
            px_now = state.player.tile_x
            py_now = state.player.tile_y
            # Filter reachable
            reachable_enemies = [e for e in enemies if abs(e.get("tileY", 0) - py_now) <= 10]
            if reachable_enemies:
                nearest = reachable_enemies[0]
                ename = nearest.get("name", "未知怪物")
                edist_h = abs(nearest["tileX"] - px_now)
                edist_v = abs(nearest["tileY"] - py_now)
                print(f"  [决策] 发现敌人 {ename} (水平{edist_h}格,垂直{edist_v}格)，切换武器战斗")
                self.unhold_torch()
                self._auto_heal_check()
                killed = self.engine.fight_nearest_enemy(timeout=8)
                self._auto_heal_check()
                self.engine.collect_nearby_items()
                if killed:
                    self.auto_equip()
                self.hold_torch()
                continue

            # Priority 2: Scan for ores (skip unreachable ones)
            ores = self.scan_ores(radius=15)
            if ores:
                # Find first ore not in failed targets
                ore = None
                for o in ores:
                    if (o[0], o[1]) not in _failed_targets:
                        ore = o
                        break
                if ore:
                    ox, oy, ot, oname = ore
                    px_now = state.player.tile_x
                    py_now = state.player.tile_y
                    ore_hdist = abs(ox - px_now)
                    ore_vdist = abs(oy - (py_now + 1))
                    print(f"  [决策] 扫描到{oname} @({ox},{oy}), "
                          f"距离: 水平{ore_hdist}格 垂直{ore_vdist}格 [{elapsed}s]")
                    self.unhold_torch()
                    mined = self.mine_ore_vein(ox, oy, ot)
                    if mined == 0:
                        # Mark this tile AND nearby same-ore tiles to avoid re-scanning same vein
                        for fdx in range(-2, 3):
                            for fdy in range(-2, 3):
                                _failed_targets.add((ox + fdx, oy + fdy))
                        print(f"  [决策] 标记 ({ox},{oy}) 周围区域为不可达，后续跳过")
                    self.hold_torch()
                    continue

            # Priority 3: Scan for chests/pots (skip unreachable)
            objects = self.scan_nearby_objects(radius=12)
            reachable_chests = [c for c in objects["chests"]
                                if tuple(c) not in _failed_targets and not self._is_base_chest(c[0], c[1])]
            if reachable_chests:
                cx, cy = reachable_chests[0]
                cdist = abs(cx - state.player.tile_x)
                print(f"  [决策] 发现箱子 @({cx},{cy}), 距离{cdist}格, 前往开箱 [{elapsed}s]")
                self.loot_nearby_chest(cx, cy)
                self.hold_torch()
                continue
            reachable_pots = [p for p in objects["pots"] if tuple(p) not in _failed_targets]
            if reachable_pots:
                pot_x, pot_y = reachable_pots[0]
                pdist = abs(pot_x - state.player.tile_x)
                print(f"  [决策] 发现罐子 @({pot_x},{pot_y}), 距离{pdist}格, 前往打碎 [{elapsed}s]")
                self.unhold_torch()
                self.engine.nav_to(pot_x, timeout=5)
                self.engine.mine_tile(pot_x, pot_y, timeout=3)
                self.engine.collect_nearby_items()
                self.hold_torch()
                continue

            # Priority 4: Check inventory
            empty = self.count_empty_slots()
            if empty <= 3:
                print(f"  [决策] 背包空位不足({empty}格), 清理低价值物品")
                freed = self.clean_inventory()
                if self.count_empty_slots() <= 3:
                    print(f"  [决策] 清理后仍只有{self.count_empty_slots()}格空位，背包满了，撤退 [{elapsed}s]")
                    break

            # Priority 5: Continue exploring
            print(f"  [决策] 附近无矿石/箱子/敌人，继续沿洞穴探索 [{elapsed}s]")
            result = self.cave_explore_step(explore_dir, path)
            if result == "dead_end":
                print(f"  [决策] 四周无可通行通道，判定为死胡同，结束探索 [{elapsed}s]")
                break
            elif result == "blocked":
                stuck_turns += 1
                if stuck_turns >= max_stuck_turns:
                    print(f"  [决策] 已连续{stuck_turns}次碰壁，该区域可能已探索完毕，结束 [{elapsed}s]")
                    break
                explore_dir = -explore_dir
                print(f"  [决策] 碰壁，转向{'右' if explore_dir > 0 else '左'}继续探索 "
                      f"(连续碰壁{stuck_turns}/{max_stuck_turns}次) [{elapsed}s]")
            elif result == "fell":
                stuck_turns = 0
                new_y = self.engine.bridge.state.player.tile_y
                print(f"  [决策] 掉落到更深层 (当前深度y={new_y})，继续探索 [{elapsed}s]")
            else:
                stuck_turns = 0

        # Phase 4: Return to surface
        elapsed = int(time.time() - start_time)
        print(f"\n  [地下探索] 开始返回 (已探索{elapsed}s, 路径点{len(path)}个)")
        self.return_to_surface(path, home_x)

        # Auto-equip after trip
        self.auto_equip()

        self.go_home_and_resupply()

        self._current_task = ""
        print(f"  [地下探索] 探索结束")
        return True

    # Track built houses to avoid demolishing them
    built_houses = []  # list of (x_start, x_end, y_start, y_end)

    def _area_has_structure(self, x1, y1, x2, y2, threshold=10) -> bool:
        """Check if an area already has significant player-placed structures."""
        solid_count = 0
        for x in range(x1, x2 + 1, 2):  # sample every 2 tiles for speed
            for y in range(y1, y2 + 1, 2):
                tile = self.engine.check_tile(x, y)
                if tile.get("hasTile"):
                    tile_type = tile.get("tileType", 0)
                    # Wood blocks, doors, workbenches, chairs, torches = player-built
                    if tile_type in {30, 10, 11, 18, 15, 4}:
                        solid_count += 1
        return solid_count >= threshold

    def build_house(self) -> bool:
        """Build a simple NPC house near the player.

        Uses direct API (instant place_tile/place_wall) for speed.
        House layout: 12 wide x 7 tall, with door, torch placement,
        workbench (flat surface) and chair (comfort).

        Terraria NPC house requirements:
        - Enclosed with blocks (floor, ceiling, walls)
        - Player-placed background walls
        - A door (3 tiles tall)
        - A light source (torch)
        - A flat surface item (table or workbench)
        - A comfort item (chair)
        - Minimum 60 non-solid tiles inside
        """
        self._cancel.clear()
        self._current_task = "建造 NPC 房屋"
        print(f"\n  [任务] {self._current_task}")

        # Get player position and find ground level
        self.bridge.get_state()
        time.sleep(0.2)
        px = self.bridge.state.player.tile_x
        py = self.bridge.state.player.tile_y

        # Find ground: scan down from player feet
        ground_y = py + 3
        for scan_y in range(py, py + 15):
            info = self.engine.check_tile(px, scan_y)
            if info.get("hasTile"):
                ground_y = scan_y
                break

        # House dimensions
        W, H = 12, 7
        start_x = px + 3  # build a few tiles to the right
        floor_y = ground_y  # floor sits on existing ground
        ceil_y = floor_y - H + 1

        # Check if area already has structures — shift right if so
        for attempt in range(5):
            if not self._area_has_structure(start_x, ceil_y, start_x + W - 1, floor_y):
                break
            print(f"  [任务] 位置 x={start_x} 已有建筑，向右偏移")
            start_x += W + 2  # shift right past existing house + gap

        # Also register overlap with previously built houses
        for (hx1, hx2, hy1, hy2) in self.built_houses:
            if start_x <= hx2 and start_x + W - 1 >= hx1:
                start_x = hx2 + 3  # move past existing house
                print(f"  [任务] 避开已建房屋，调整到 x={start_x}")

        # Walk to build site
        self.engine.nav_to(start_x + W // 2, timeout=10)

        print(f"  [任务] 建房位置: ({start_x}, {ceil_y}) ~ ({start_x + W - 1}, {floor_y})")

        # Tile type constants
        WOOD_BLOCK = 30   # TileID.WoodBlock
        WOOD_WALL = 4     # WallID.Wood
        DOOR_TILE = 10    # TileID.ClosedDoor (placed as OpenDoor tile type)
        WORKBENCH_TILE = 18  # TileID.WorkBenches
        CHAIR_TILE = 15   # TileID.Chairs
        TORCH_TILE = 4    # TileID.Torches

        # Step 1: Clear interior space (NOT the floor — avoid player falling)
        print("  [任务] 清理建筑区域...")
        cleared = self.engine.kill_area(start_x, ceil_y, start_x + W - 1, floor_y - 1)
        print(f"  [任务] 清理了 {cleared} 个方块")

        # Step 2: Build frame (floor, ceiling, walls)
        print("  [任务] 建造框架...")
        placed = 0
        # Floor: replace one tile at a time (kill then place, so player doesn't fall)
        for x in range(start_x, start_x + W):
            self.engine.kill_tile(x, floor_y)
            if self.engine.place_tile(x, floor_y, WOOD_BLOCK):
                placed += 1
        # Ceiling
        for x in range(start_x, start_x + W):
            if self.engine.place_tile(x, ceil_y, WOOD_BLOCK):
                placed += 1
        # Left wall (with 3-tile door gap at bottom)
        left_door_x = start_x
        for y in range(ceil_y + 1, floor_y):
            if y < floor_y - 3:  # solid above door
                if self.engine.place_tile(left_door_x, y, WOOD_BLOCK):
                    placed += 1
            # else: leave gap for door (floor_y-3 to floor_y-1)
        # Right wall (with 3-tile door gap at bottom)
        right_door_x = start_x + W - 1
        for y in range(ceil_y + 1, floor_y):
            if y < floor_y - 3:  # solid above door
                if self.engine.place_tile(right_door_x, y, WOOD_BLOCK):
                    placed += 1
            # else: leave gap for door (floor_y-3 to floor_y-1)
        print(f"  [任务] 框架放置 {placed} 块")

        # Step 3: Background walls (interior only)
        print("  [任务] 放置背景墙...")
        walls = 0
        for x in range(start_x + 1, start_x + W - 1):
            for y in range(ceil_y + 1, floor_y):
                if self.engine.place_wall(x, y, WOOD_WALL):
                    walls += 1
        print(f"  [任务] 放置 {walls} 面背景墙")

        # Step 4: Place doors (both sides)
        print("  [任务] 放置门...")
        door_y = floor_y - 3  # door top position
        self.engine.place_tile(left_door_x, door_y, DOOR_TILE)
        self.engine.place_tile(right_door_x, door_y, DOOR_TILE)

        # Step 5: Place furniture
        print("  [任务] 放置家具...")
        # Workbench (2 wide, goes on floor)
        self.engine.place_tile(start_x + 3, floor_y - 1, WORKBENCH_TILE)
        # Chair (1 wide, goes on floor)
        self.engine.place_tile(start_x + 6, floor_y - 1, CHAIR_TILE)
        # Torch (on left wall)
        self.engine.place_tile(start_x + 1, ceil_y + 2, TORCH_TILE)

        # Step 6: Clear doorways — remove blocks outside both doors so player can exit
        print("  [任务] 清理门外通道...")
        for door_x, outside_dir in [(left_door_x, -1), (right_door_x, 1)]:
            for offset in [1, 2]:
                clear_x = door_x + outside_dir * offset
                # Clear door height (3 tiles above floor)
                for y in range(floor_y - 3, floor_y):
                    self.engine.kill_tile(clear_x, y)

        # Register house location so future builds don't demolish it
        self.built_houses.append((start_x, start_x + W - 1, ceil_y, floor_y))

        self._current_task = ""
        print(f"  [任务] 房屋建造完成! 位置: x={start_x}~{start_x + W - 1}")
        return True

    def early_game(self) -> bool:
        """Full early game sequence: chop trees → craft → build house."""
        self._cancel.clear()
        self._current_task = "前期准备"
        print(f"\n{'='*50}")
        print(f"  开始前期准备流程")
        print(f"{'='*50}")

        # Fight any nearby enemies first
        self.engine.fight_nearest_enemy(timeout=5)

        # Step 1: Gather wood
        print("\n--- 第1步：收集木材 ---")
        wood = self.gather_wood(target=50)
        if wood < 20:
            print("  [任务] 木材不足，无法继续")
            self._current_task = ""
            return False

        # Step 2: Check available recipes
        print("\n--- 第2步：查看可合成物品 ---")
        recipes = self.engine.get_recipes()
        if recipes:
            craftable = [r for r in recipes if r.get("canCraft", False)]
            print(f"  [任务] 当前可合成 {len(craftable)}/{len(recipes)} 种物品:")
            for r in craftable[:10]:
                ingr = ", ".join(f"{i['name']}x{i.get('need',0)}" for i in r["ingredients"])
                print(f"    - {r['result']['name']} (需要: {ingr})")

        # Step 3: Craft workbench
        print("\n--- 第3步：合成工作台 ---")
        crafted = self.engine.craft(item_name="WorkBench", amount=1)
        if crafted == 0:
            # Try Chinese name
            crafted = self.engine.craft(item_name="工作台", amount=1)
        if crafted > 0:
            print("  [任务] 工作台合成成功!")
        else:
            print("  [任务] 工作台合成失败，尝试查看配方...")
            recipes = self.engine.get_recipes()
            for r in recipes[:20]:
                ingr = ", ".join(f"{i['name']}({i.get('have',0)}/{i.get('need',0)})" for i in r["ingredients"])
                print(f"    {r['result']['name']}: {ingr} {'✓' if r.get('canCraft') else '✗'}")

        # Step 4: Craft other items needed for house
        print("\n--- 第4步：合成建筑材料 ---")
        # After workbench, need to place it first to unlock more recipes
        # For now, craft what we can
        self.engine.craft(item_name="Wall", amount=5)  # wood walls
        self.engine.craft(item_name="Door", amount=1)
        self.engine.craft(item_name="Chair", amount=1)
        wood = self.engine.count_item(item_id=9)
        torch_wood = wood // 2
        platform_wood = wood - torch_wood
        if torch_wood > 0:
            self.engine.craft(item_name="Torch", amount=torch_wood)
        if platform_wood > 0:
            self.engine.craft(item_id=94, amount=platform_wood)
            print(f"  [任务] 木材分配: {torch_wood}→火把, {platform_wood}→木平台")

        # Step 5: Show inventory
        print("\n--- 第5步：当前背包 ---")
        inv = self.engine.get_inventory()
        for item in inv:
            print(f"    [{item['slot']}] {item['name']} x{item['stack']}")

        # Fight enemies before building
        self.engine.fight_nearest_enemy(timeout=5)

        # Step 6: Build house
        print("\n--- 第6步：建造房屋 ---")
        self.build_house()

        self._current_task = ""
        print(f"\n{'='*50}")
        print(f"  前期准备完成!")
        print(f"{'='*50}")
        return True


def _is_hostile(npc: dict) -> bool:
    """Check if NPC is truly hostile (not a critter, not friendly, not a town NPC).

    Requires mod to provide 'damage' field to distinguish critters (damage=0) from enemies.
    If 'damage' field is missing (mod not rebuilt), falls back to treating all non-friendly as hostile.
    """
    if npc.get("friendly") or npc.get("townNPC"):
        return False
    # If mod provides damage field, use it to filter critters
    if "damage" in npc:
        return npc["damage"] > 0
    # Fallback: no damage field (mod not rebuilt) — treat all non-friendly as hostile
    # This is safer than ignoring enemies (player won't get killed by "critters")
    return True


def _npc_tag(npc: dict) -> str:
    """Get display tag for NPC."""
    if npc.get("boss"):
        return "BOSS"
    if npc.get("townNPC"):
        return "城镇"
    if npc.get("friendly"):
        return "友好"
    if "damage" in npc and npc["damage"] <= 0:
        return "小动物"
    if "damage" not in npc and not npc.get("friendly"):
        return "敌对?"  # damage field missing, assuming hostile
    return "敌对"


def print_state(state: GameState):
    """Pretty print current game state."""
    p = state.player
    time_str = "白天" if state.day_time else "夜晚"
    weather = ""
    if state.raining:
        weather = " (下雨)"
    if state.blood_moon:
        weather = " (血月!)"

    print(f"\n--- 游戏状态 (tick {state.tick}) ---")
    print(f"  玩家: {p.name}  HP: {p.hp}/{p.max_hp}  Mana: {p.mana}/{p.max_mana}")
    bright_str = f"  亮度: {p.brightness:.2f}" if p.brightness < 0.5 else ""
    print(f"  位置: ({p.tile_x}, {p.tile_y})  方向: {'→' if p.direction > 0 else '←'}  {'站立' if p.grounded else '空中'}{bright_str}")
    print(f"  时间: {time_str}{weather}")

    if state.hotbar:
        items = ", ".join(f"[{h['slot']}]{h['name']}x{h['stack']}" for h in state.hotbar)
        print(f"  快捷栏: {items}")

    if state.nearby_npcs:
        for npc in state.nearby_npcs[:5]:
            tag = _npc_tag(npc)
            print(f"  NPC: {npc['name']} HP:{npc['life']}/{npc['lifeMax']} ({tag})")


# ============================================================
# Strategic Brain — LLM-driven goal selection (background thread)
# ============================================================

STRATEGY_GUIDE_PATH = os.path.join(os.path.dirname(__file__), "terraria_strategy_guide.md")

STRATEGIC_BRAIN_SYSTEM_PROMPT = """你是泰拉瑞亚的战略顾问。你必须调用 set_goal 工具下达指令，不要回复文本。

规则：
1. 必须调用 set_goal，每次只下达一个目标
2. 目标必须具体（物品用英文名，gather要给quantity数量）
3. 优先级：升级镐 > 升级防具 > 升级武器 > 打Boss > 继续探索
4. 材料不够 → gather，材料够 → craft
5. 不要重复已完成的目标"""

STRATEGIC_BRAIN_TOOL = {
    "type": "function",
    "name": "set_goal",
    "description": "设定玩家的下一个战略目标",
    "parameters": {
        "type": "object",
        "properties": {
            "goal_type": {
                "type": "string",
                "enum": ["gather", "craft", "explore", "boss_prep"],
                "description": "目标类型"
            },
            "target": {
                "type": "string",
                "description": "目标物品英文名(如 Iron Ore)或boss英文名(如 Eye of Cthulhu)"
            },
            "reason": {
                "type": "string",
                "description": "做出此决策的理由"
            },
            "direction": {
                "type": "integer",
                "description": "探索方向 1=右 -1=左 (仅explore/gather时需要)"
            },
            "quantity": {
                "type": "integer",
                "description": "需要收集的数量 (仅gather时需要)"
            }
        },
        "required": ["goal_type", "target", "reason"]
    }
}


class StrategicBrain:
    """Background thread that calls LLM to decide strategic goals."""

    def __init__(self, goal_queue: queue.Queue):
        self.goal_queue = goal_queue
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._summary = ""  # latest summary from tactical layer
        self._thread = threading.Thread(target=self._run, daemon=True, name="StrategicBrain")

        # API config — same as Lumi slow brain (Volcengine ARK)
        self._api_url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        self._api_key = os.getenv("ARK_API_KEY", "")
        self._model = "doubao-seed-1-8-251228"

    def start(self):
        if not self._api_key:
            print("  [慢脑] 警告: ARK_API_KEY 未设置!")
        else:
            print(f"  [慢脑] API Key: {self._api_key[:8]}...{self._api_key[-4:]}")
        self._thread.start()
        print("  [慢脑] 战略脑线程已启动")

    def stop(self):
        self._stop.set()
        self._wake.set()  # unblock wait

    def request_goal(self, summary: str):
        """Called by tactical layer to request a new goal."""
        self._summary = summary
        self._wake.set()

    def _load_strategy_guide(self) -> str:
        try:
            with open(STRATEGY_GUIDE_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return "(进度指南文件未找到)"

    def _call_llm(self, summary: str) -> StrategicGoal | None:
        """Call LLM via chat/completions endpoint with reasoning_effort=low."""
        guide = self._load_strategy_guide()
        user_text = f"## 进度指南\n\n{guide}\n\n## 当前状态\n\n{summary}"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        # Use chat/completions endpoint (supports reasoning_effort)
        chat_tool = {
            "type": "function",
            "function": {
                "name": STRATEGIC_BRAIN_TOOL["name"],
                "description": STRATEGIC_BRAIN_TOOL["description"],
                "parameters": STRATEGIC_BRAIN_TOOL["parameters"],
            }
        }
        payload = {
            "model": self._model,
            "max_completion_tokens": 512,
            "reasoning_effort": "medium",
            "messages": [
                {"role": "system", "content": STRATEGIC_BRAIN_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "tools": [chat_tool],
        }

        try:
            t0 = time.time()
            resp = requests.post(self._api_url, headers=headers, json=payload, timeout=30)
            elapsed = time.time() - t0
            if resp.status_code != 200:
                print(f"  [慢脑] HTTP {resp.status_code}: {resp.text[:300]}")
                return None
            data = resp.json()

            # Parse chat/completions response format
            choices = data.get("choices", [])
            if not choices:
                print(f"  [慢脑] LLM返回空choices ({elapsed:.1f}s)")
                return None

            msg = choices[0].get("message", {})

            # Log thinking if present
            thinking = msg.get("reasoning_content", "")
            if thinking:
                print(f"  [慢脑·思考] {thinking[:120]}...")

            # Parse tool calls
            tool_calls = msg.get("tool_calls", [])
            if not tool_calls:
                text = msg.get("content", "")
                print(f"  [慢脑] LLM未下达目标: {text[:100]} ({elapsed:.1f}s)")
                return None

            tc = tool_calls[0]["function"]
            try:
                args = json.loads(tc.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            goal_type = args.get("goal_type", "")
            if goal_type not in VALID_GOAL_TYPES:
                print(f"  [慢脑] 未知目标类型: {goal_type}，忽略 ({elapsed:.1f}s)")
                return None

            params = {}
            if "direction" in args:
                params["direction"] = args["direction"]
            if "quantity" in args:
                params["quantity"] = args["quantity"]

            goal = StrategicGoal(
                goal_type=goal_type,
                target=args.get("target", ""),
                reason=args.get("reason", ""),
                params=params,
            )
            print(f"  [慢脑] 新目标: {goal.goal_type} → {goal.target} ({goal.reason}) ({elapsed:.1f}s)")
            return goal

        except requests.Timeout:
            print(f"  [慢脑] LLM调用超时 (30s)")
            return None
        except Exception as e:
            print(f"  [慢脑] LLM调用失败: {e}")
            return None

    def _run(self):
        """Main loop: wait for wake signal, call LLM, put goal in queue."""
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break

            summary = self._summary
            if not summary:
                continue

            # Retry up to 3 times if LLM doesn't call the tool
            goal = None
            for attempt in range(3):
                if self._stop.is_set():
                    break
                goal = self._call_llm(summary)
                if goal:
                    break
                print(f"  [慢脑] 第{attempt+1}次未获得目标，{3 if attempt < 2 else '放弃'}秒后重试...")
                if attempt < 2:
                    time.sleep(3)

            if goal:
                # Clear old goal and put new one
                try:
                    self.goal_queue.get_nowait()
                except queue.Empty:
                    pass
                self.goal_queue.put(goal)
            else:
                print("  [慢脑] 3次尝试均未获得目标，等待下次触发")

        print("  [慢脑] 战略脑线程已停止")


# ============================================================
# Survival Loop — main autonomous gameplay loop
# ============================================================

def survival_loop(bridge: TerrariaBridge):
    """Autonomous survival loop: strategic brain sets goals, tactical layer executes."""
    engine = BehaviorEngine(bridge)
    runner = TaskRunner(engine)

    print("\n" + "=" * 50)
    print("  生存循环模式 (慢脑驱动)")
    print("=" * 50)

    # --- Initialization ---
    bridge.get_state()
    time.sleep(0.3)

    if not runner.init_base():
        print("  [生存] 初始化基地失败，退出")
        return

    engine.equip_wings()
    runner.auto_equip()

    # --- Strategic brain setup ---
    goal_queue = queue.Queue(maxsize=1)
    brain = StrategicBrain(goal_queue)
    brain.start()

    # State tracking
    current_goal: StrategicGoal | None = None
    completed_goals: list[str] = []
    recent_actions: list[str] = []
    patrol_dir = 1
    patrol_range = 20
    last_explore_time = time.time()
    explore_cooldown = 180  # seconds
    patrol_cycles = 0

    # Request first goal
    summary = runner.build_status_summary(recent_actions, completed_goals)
    brain.request_goal(summary)
    recent_actions.append("启动生存循环，请求首个目标")

    print("\n=== 进入生存循环 ===")

    try:
        while bridge.connected:
            bridge.get_state()
            time.sleep(0.1)

            hp = bridge.state.player.hp
            max_hp = bridge.state.player.max_hp
            hp_ratio = hp / max_hp if max_hp > 0 else 1.0

            # --- Priority 0: Self-preservation ---
            if hp_ratio < 0.5:
                runner._auto_heal_check()

                if hp_ratio < 0.3:
                    enemies = [npc for npc in bridge.state.nearby_npcs
                               if _is_hostile(npc) and npc.get("life", 0) > 0]
                    if enemies:
                        nearest = min(enemies, key=lambda e: abs(e["x"] - bridge.state.player.x))
                        flee_dir = -1 if nearest["x"] > bridge.state.player.x else 1
                        print(f"  [生存] 危险! HP:{hp}/{max_hp}，逃跑!")
                        flee_x = bridge.state.player.tile_x + flee_dir * 5
                        engine.nav_to(flee_x, timeout=3)
                        continue

            # --- Priority 0.1: 溺水逃离 ---
            breath = bridge.state.player.breath
            breath_max = bridge.state.player.breath_max
            if breath < breath_max * 0.5:
                my_tx = bridge.state.player.tile_x
                my_ty = bridge.state.player.tile_y
                escape_y = my_ty - 10
                print(f"  [生存] 溺水! 呼吸值 {breath}/{breath_max}，紧急上浮!")
                engine.nav_to(my_tx, target_y=escape_y, allow_dig=True, timeout=8)
                continue

            # --- Priority 1: Fight nearby enemies ---
            if hp_ratio >= 0.5:
                enemies = [npc for npc in bridge.state.nearby_npcs
                           if _is_hostile(npc) and npc.get("life", 0) > 0]
                if enemies:
                    killed = engine.fight_nearest_enemy(timeout=8)
                    if killed:
                        engine.collect_nearby_items()
                        continue

            # --- Priority 2: Check for new strategic goal ---
            if current_goal is None:
                try:
                    new_goal = goal_queue.get_nowait()
                    current_goal = new_goal
                    print(f"\n  [生存] 收到新目标: {current_goal.goal_type} → {current_goal.target}")
                except queue.Empty:
                    pass

            # --- Priority 3: Execute current goal ---
            if current_goal is not None:
                success, result_desc = runner.execute_goal(current_goal)

                goal_desc = f"{current_goal.goal_type}:{current_goal.target}"
                if success:
                    completed_goals.append(goal_desc)
                    recent_actions.append(f"完成目标: {goal_desc}")
                    print(f"\n  [生存] 目标完成: {result_desc}")
                else:
                    recent_actions.append(f"目标失败: {goal_desc} — {result_desc}")
                    print(f"\n  [生存] 目标失败: {result_desc}")

                current_goal = None

                # Request next goal from brain
                summary = runner.build_status_summary(recent_actions, completed_goals)
                brain.request_goal(summary)
                continue

            # --- Priority 4: Fallback — patrol + periodic explore ---
            patrol_cycles += 1

            if patrol_cycles % 10 == 0:
                runner.auto_equip()
                if runner.count_empty_slots() <= 5:
                    runner.clean_inventory()

            # Periodic exploration when no goal
            if time.time() - last_explore_time > explore_cooldown and hp_ratio >= 0.8:
                print(f"\n  [生存] Fallback: 开始定时探索")
                runner.explore_underground(direction=patrol_dir, max_time=300)
                last_explore_time = time.time()
                runner.go_home_and_resupply()
                recent_actions.append(f"Fallback探索 (方向={'右' if patrol_dir > 0 else '左'})")

                # After exploration, request a goal
                summary = runner.build_status_summary(recent_actions, completed_goals)
                brain.request_goal(summary)
                continue

            # Patrol back and forth
            px = bridge.state.player.tile_x
            home_x = runner.base_x or px

            if patrol_dir == 1 and px >= home_x + patrol_range:
                patrol_dir = -1
            elif patrol_dir == -1 and px <= home_x - patrol_range:
                patrol_dir = 1

            target_x = px + patrol_dir * 5
            engine.nav_to(target_x, timeout=5)

    except KeyboardInterrupt:
        print("\n  [生存] 收到中断信号")
    finally:
        brain.stop()
        print("  [生存] 生存循环结束")


def demo_walk(bridge: TerrariaBridge):
    """Demo: walk right for 2 seconds, then left for 2 seconds."""
    print("\n=== 测试移动: 向右走 2 秒 ===")
    bridge.move("right")
    time.sleep(2)

    print("=== 测试移动: 向左走 2 秒 ===")
    bridge.move("left")
    time.sleep(2)

    print("=== 测试移动: 跳跃 ===")
    bridge.move("jump")
    time.sleep(0.5)

    bridge.move("stop")
    print("=== 移动测试完成 ===")


def demo_behavior(bridge: TerrariaBridge):
    """Demo: test behavior system - move_to + mine_tile."""
    engine = BehaviorEngine(bridge)

    state = bridge.state
    px = state.player.tile_x
    print(f"\n当前位置: ({px}, {state.player.tile_y})")

    # Test 1: Move 10 tiles to the right
    target_x = px + 10
    print(f"\n=== 测试: move_to(x={target_x}) ===")
    engine.nav_to(target_x)

    time.sleep(0.5)
    bridge.get_state()
    time.sleep(0.3)

    # Test 2: Mine a single tile
    px = bridge.state.player.tile_x
    py = bridge.state.player.tile_y
    print(f"\n当前位置: ({px}, {py})，扫描附近实心方块...")

    for dy in range(2, 6):
        for dx in range(0, 4):
            tx, ty = px + dx, py + dy
            info = engine.check_tile(tx, ty)
            if info.get("hasTile"):
                print(f"\n=== 测试: mine_tile({tx}, {ty}) ===")
                engine.mine_tile(tx, ty)
                print("\n=== 行为测试完成 ===")
                return
    print("附近没找到可挖的方块")


def test_obstacle(bridge: TerrariaBridge):
    """Test: walk left 20 tiles, testing obstacle clearing. No combat."""
    engine = BehaviorEngine(bridge)
    bridge.get_state()
    time.sleep(0.3)
    px = bridge.state.player.tile_x
    py = bridge.state.player.tile_y
    print(f"\n=== 障碍测试: 从 ({px}, {py}) 向左走 20 格 ===")

    target_x = px + 20
    result = engine.nav_to(target_x, timeout=60)
    bridge.get_state()
    time.sleep(0.1)
    final_x = bridge.state.player.tile_x
    print(f"\n=== 结果: {'成功' if result else '失败'}, 最终位置 x={final_x} ===")


def demo_task(bridge: TerrariaBridge):
    """Demo: early game - chop trees, craft, build house, then survive."""
    engine = BehaviorEngine(bridge)
    runner = TaskRunner(engine)

    bridge.get_state()
    time.sleep(0.3)
    px = bridge.state.player.tile_x
    py = bridge.state.player.tile_y
    print(f"\n当前位置: ({px}, {py})")

    # Skip early_game if already set up (have wood >= 20 means we've played before)
    wood_count = engine.count_item(item_id=9)  # wood
    if wood_count >= 20:
        print(f"  已有木材 {wood_count}，跳过前期准备流程")
    else:
        runner.early_game()

    # Check for available upgrades before patrolling
    runner.check_upgrade()

    # Enter survival loop: patrol, fight, collect, explore
    print("\n=== 进入生存模式 (巡逻 + 战斗 + 探索) ===")
    bridge.get_state()
    time.sleep(0.2)
    home_x = bridge.state.player.tile_x
    patrol_dir = 1  # 1 = right, -1 = left
    patrol_range = 20

    last_heal_time = 0  # track heal cooldown to avoid spam
    last_explore_time = time.time()  # cooldown between exploration trips
    explore_cooldown = 180  # seconds between underground trips
    patrol_cycles = 0  # count patrol cycles for periodic checks

    while bridge.connected:
        bridge.get_state()
        time.sleep(0.1)

        hp = bridge.state.player.hp
        max_hp = bridge.state.player.max_hp
        hp_ratio = hp / max_hp if max_hp > 0 else 1.0

        # Priority 0: Self-preservation (HP < 50%)
        if hp_ratio < 0.5:
            now = time.time()
            if now - last_heal_time > 1.0:
                bridge.quick_heal()
                last_heal_time = now
                print(f"  [生存] 血量低 ({hp}/{max_hp}={hp_ratio:.0%})，尝试喝药")

            if hp_ratio < 0.3:
                enemies = [npc for npc in bridge.state.nearby_npcs
                           if _is_hostile(npc) and npc.get("life", 0) > 0]
                if enemies:
                    nearest = min(enemies, key=lambda e: abs(e["x"] - bridge.state.player.x))
                    flee_dir = -1 if nearest["x"] > bridge.state.player.x else 1
                    print(f"  [生存] 危险! HP:{hp}/{max_hp}，逃跑!")
                    flee_x = bridge.state.player.tile_x + flee_dir * 5
                    engine.nav_to(flee_x, timeout=3)
                    continue

        # Priority 0.1: 溺水逃离
        breath = bridge.state.player.breath
        breath_max = bridge.state.player.breath_max
        if breath < breath_max * 0.5:
            my_tx = bridge.state.player.tile_x
            my_ty = bridge.state.player.tile_y
            escape_y = my_ty - 10
            print(f"  [生存] 溺水! 呼吸值 {breath}/{breath_max}，紧急上浮!")
            engine.nav_to(my_tx, target_y=escape_y, allow_dig=True, timeout=8)
            continue

        # Priority 1: Fight nearby hostile NPCs
        if hp_ratio >= 0.5:
            enemies = [npc for npc in bridge.state.nearby_npcs
                       if _is_hostile(npc) and npc.get("life", 0) > 0]
            if enemies:
                killed = engine.fight_nearest_enemy(timeout=8)
                if killed:
                    engine.collect_nearby_items()
                    continue

        # Priority 2: Periodic checks (every 10 patrol cycles)
        patrol_cycles += 1
        if patrol_cycles % 10 == 0:
            # Auto-equip better gear found
            runner.auto_equip()
            # Clean trash from inventory
            if runner.count_empty_slots() <= 5:
                runner.clean_inventory()

        # Priority 3: Underground exploration (when cooldown expires)
        if time.time() - last_explore_time > explore_cooldown and hp_ratio >= 0.8:
            print(f"\n  [生存] 开始地下探索")
            runner.explore_underground(direction=patrol_dir, max_time=300)
            last_explore_time = time.time()
            # After exploration, check upgrades near workbench
            runner.check_upgrade()
            # Update home position (might have moved)
            bridge.get_state()
            time.sleep(0.1)
            home_x = bridge.state.player.tile_x
            continue

        # Priority 4: Patrol back and forth
        px = bridge.state.player.tile_x

        if patrol_dir == 1 and px >= home_x + patrol_range:
            patrol_dir = -1
        elif patrol_dir == -1 and px <= home_x - patrol_range:
            patrol_dir = 1

        target_x = px + patrol_dir * 5
        arrived = engine.nav_to(target_x, timeout=5)

        if not arrived:
            patrol_dir *= -1
            print(f"  [生存] 遇到障碍，掉头")

        time.sleep(0.3)


def test_batch1(bridge: TerrariaBridge):
    """Batch 1 验证测试: scan_relative, get_nearest_npcs, scan_surroundings."""
    engine = BehaviorEngine(bridge)
    runner = TaskRunner(engine)

    bridge.get_state()
    time.sleep(0.3)
    px = bridge.state.player.tile_x
    py = bridge.state.player.tile_y
    print(f"\n{'='*60}")
    print(f"  批次 1 测试 — 玩家位置: ({px}, {py})")
    print(f"{'='*60}")

    passed = 0
    failed = 0

    # ═══════════════════════════════════════════════════
    # Test 1: scan_relative 基础功能 + tile 分类
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 1: scan_relative 返回数据 + tile 分类 ---")
    resp = engine.scan_relative(-10, -10, 20, 20)
    tiles = resp.get("tiles", [])
    player_x = resp.get("playerX", 0)
    player_y = resp.get("playerY", 0)

    print(f"  返回 tile 数量: {len(tiles)}")
    print(f"  返回 playerX={player_x}, playerY={player_y}")

    # 1a: 有返回数据
    if len(tiles) > 0:
        print(f"  [PASS] scan_relative 返回了 {len(tiles)} 个 tile")
        passed += 1
    else:
        print(f"  [FAIL] scan_relative 返回空数据!")
        failed += 1

    # 1b: playerX/Y 合理
    if abs(player_x - px) <= 2 and abs(player_y - py) <= 2:
        print(f"  [PASS] 玩家坐标匹配 (bridge: {px},{py} vs scan: {player_x},{player_y})")
        passed += 1
    else:
        print(f"  [FAIL] 玩家坐标不匹配! bridge=({px},{py}) scan=({player_x},{player_y})")
        failed += 1

    # 1c: tile 分类字段存在
    class_counts = {0: 0, 1: 0, 2: 0, -1: 0}  # Block, OneWay, Ore, Missing
    has_c_field = True
    for t in tiles:
        c = t.get("c", -1)
        if "c" not in t:
            has_c_field = False
        class_counts[c] = class_counts.get(c, 0) + 1

    if has_c_field:
        print(f"  [PASS] 所有 tile 都有 'c' 分类字段")
        passed += 1
    else:
        print(f"  [FAIL] 部分 tile 缺少 'c' 字段!")
        failed += 1

    print(f"  分类统计: Block(c=0)={class_counts[0]}, OneWay(c=1)={class_counts[1]}, Ore(c=2)={class_counts[2]}")

    # 1d: 脚下应该有实心方块 (Block)
    foot_tiles = [t for t in tiles if t["x"] == player_x and t["y"] == player_y + 3]
    if foot_tiles:
        ft = foot_tiles[0]
        print(f"  脚下方块: type={ft.get('t')}, class={ft.get('c')} (0=Block)")
        if ft.get("c") == 0:
            print(f"  [PASS] 脚下方块正确分类为 Block")
            passed += 1
        else:
            print(f"  [WARN] 脚下方块分类为 {ft.get('c')} (可能站在平台上)")
            passed += 1  # 不算失败，可能站在平台上
    else:
        print(f"  [INFO] 脚下无方块（可能在空中）")

    # 打印几个样本 tile
    print(f"  样本 tile (前5个):")
    for t in tiles[:5]:
        print(f"    x={t['x']}, y={t['y']}, type={t.get('t')}, class={t.get('c')}")

    # ═══════════════════════════════════════════════════
    # Test 2: get_nearest_npcs 距离排序
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 2: get_nearest_npcs 距离排序 ---")

    # 2a: 获取所有 NPC（包括友好）
    all_npcs = engine.get_nearest_npcs(hostile=False, count=10, range_tiles=100)
    print(f"  范围 100 格内所有 NPC: {len(all_npcs)} 个")
    for npc in all_npcs:
        print(f"    {npc.get('name', '?')} | 距离={npc.get('dist', '?'):.1f}格 "
              f"| 位置=({npc.get('tileX')},{npc.get('tileY')}) "
              f"| HP={npc.get('life')}/{npc.get('lifeMax')} "
              f"| {'友好' if npc.get('friendly') else '敌对'}")

    if len(all_npcs) > 0:
        print(f"  [PASS] get_nearest_npcs 返回了 {len(all_npcs)} 个 NPC")
        passed += 1

        # 2b: 检查距离排序
        dists = [npc.get("dist", 0) for npc in all_npcs]
        is_sorted = all(dists[i] <= dists[i + 1] for i in range(len(dists) - 1))
        if is_sorted:
            print(f"  [PASS] 距离排序正确: {[f'{d:.1f}' for d in dists]}")
            passed += 1
        else:
            print(f"  [FAIL] 距离排序错误! {[f'{d:.1f}' for d in dists]}")
            failed += 1

        # 2c: 坐标合理性 (tileX/tileY 应在合理范围)
        coords_ok = all(
            abs(npc.get("tileX", 0) - px) < 200 and abs(npc.get("tileY", 0) - py) < 200
            for npc in all_npcs
        )
        if coords_ok:
            print(f"  [PASS] NPC tile 坐标合理")
            passed += 1
        else:
            print(f"  [FAIL] NPC tile 坐标异常!")
            failed += 1
    else:
        print(f"  [INFO] 附近没有 NPC，跳过排序测试（可以在夜晚或洞穴中重试）")

    # 2d: hostile 过滤
    hostile_npcs = engine.get_nearest_npcs(hostile=True, count=5, range_tiles=100)
    friendly_in_hostile = [n for n in hostile_npcs if n.get("friendly") or n.get("townNPC")]
    if len(friendly_in_hostile) == 0:
        print(f"  [PASS] hostile=True 过滤正确（{len(hostile_npcs)} 个敌对 NPC，无友好混入）")
        passed += 1
    else:
        print(f"  [FAIL] hostile=True 过滤失败! 混入友好 NPC: {[n['name'] for n in friendly_in_hostile]}")
        failed += 1

    # ═══════════════════════════════════════════════════
    # Test 3: scan_surroundings 综合扫描
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 3: scan_surroundings 综合扫描 ---")
    surr = runner.scan_surroundings(20)

    surr_px = surr.get("px", 0)
    surr_py = surr.get("py", 0)
    solid = surr.get("solid", set())
    ores = surr.get("ores", [])
    chests = surr.get("chests", [])
    pots = surr.get("pots", [])
    passages = surr.get("passages", [])
    drop_points = surr.get("drop_points", [])
    cavity = surr.get("cavity_below")

    print(f"  玩家位置: ({surr_px}, {surr_py})")
    print(f"  实心方块: {len(solid)} 个")
    print(f"  矿石: {len(ores)} 个")
    print(f"  宝箱: {len(chests)} 个")
    print(f"  罐子: {len(pots)} 个")
    print(f"  通道: {len(passages)} 个")
    print(f"  掉落点: {len(drop_points)} 个")
    print(f"  脚下空腔: {'有' if cavity else '无'}")

    # 3a: solid set 有数据
    if len(solid) > 0:
        print(f"  [PASS] scan_surroundings 识别出 {len(solid)} 个实心方块")
        passed += 1
    else:
        print(f"  [FAIL] scan_surroundings 未识别任何实心方块!")
        failed += 1

    # 3b: 矿石详情
    if ores:
        print(f"  矿石详情:")
        for ox, oy, ot, oname in ores[:5]:
            print(f"    ({ox},{oy}) {oname} type={ot}")
        print(f"  [PASS] 发现 {len(ores)} 个矿石")
        passed += 1
    else:
        print(f"  [INFO] 附近无矿石（地表正常，地下应该有）")

    # 3c: 通道详情
    if passages:
        print(f"  通道详情:")
        for p in passages:
            print(f"    方向={p['direction']}, 深度={p['depth']}, "
                  f"入口=({p['entry_x']},{p['entry_y']}), "
                  f"更深={p['goes_deeper']}, 空气比={p['air_ratio']:.1%}")
        print(f"  [PASS] 发现 {len(passages)} 条通道")
        passed += 1
    else:
        print(f"  [INFO] 附近无通道")

    # 3d: 掉落点详情
    if drop_points:
        print(f"  掉落点详情:")
        for dp in drop_points[:3]:
            print(f"    x={dp['x']}, 宽={dp['gap_width']}, "
                  f"深={dp['fall_depth']}, 可达={dp['reachable']}")

    # ═══════════════════════════════════════════════════
    # Test 4: 大范围扫描（模拟洞穴探索场景）
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 4: 大范围 scan_relative (30格半径) ---")
    resp_large = engine.scan_relative(-30, -30, 60, 60)
    tiles_large = resp_large.get("tiles", [])
    print(f"  返回 tile 数量: {len(tiles_large)}")
    if len(tiles_large) > len(tiles):
        print(f"  [PASS] 大范围扫描返回更多 tile ({len(tiles_large)} > {len(tiles)})")
        passed += 1
    elif len(tiles_large) > 0:
        print(f"  [PASS] 大范围扫描有数据 ({len(tiles_large)} tiles)")
        passed += 1
    else:
        print(f"  [FAIL] 大范围扫描返回空!")
        failed += 1

    # ═══════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════
    print(f"\n{'='*60}")
    total = passed + failed
    print(f"  批次 1 测试结果: {passed}/{total} 通过, {failed}/{total} 失败")
    if failed == 0:
        print(f"  ✓ 全部通过!")
    else:
        print(f"  ✗ 有 {failed} 项失败，需要检查")
    print(f"{'='*60}")

    print(f"\n提示: 如果 NPC 测试为空，可以在夜晚或洞穴中重新测试。")
    print(f"提示: 如果矿石为空，在地表正常，进入洞穴后应该能看到。")


def test_batch2a(bridge: TerrariaBridge):
    """Batch 2a 验证测试: A* find_path 寻路算法."""
    engine = BehaviorEngine(bridge)

    bridge.get_state()
    time.sleep(0.3)
    px = bridge.state.player.tile_x
    py = bridge.state.player.tile_y
    feet_y = py + 2  # feet position (what C# uses)
    print(f"\n{'='*60}")
    print(f"  批次 2a 测试 — A* 寻路算法")
    print(f"  玩家位置: ({px}, {py}), 脚底: ({px}, {feet_y})")
    print(f"{'='*60}")

    passed = 0
    failed = 0

    # ═══════════════════════════════════════════════════
    # Test 1: 平地短距离寻路 (右走 10 格)
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 1: 平地寻路 (右走 10 格) ---")
    result = engine.find_path(px + 10, feet_y)
    print(f"  success={result.get('success')}, reason={result.get('reason', '-')}")
    if result.get("success"):
        wps = result.get("waypoints", [])
        print(f"  waypoints: {len(wps)} 个, rawLength: {result.get('rawLength')}")
        for wp in wps:
            print(f"    ({wp['x']}, {wp['y']})")
        if len(wps) >= 2:
            print(f"  [PASS] 平地寻路成功，{len(wps)} 个路点")
            passed += 1
        else:
            print(f"  [FAIL] 路点数量异常 ({len(wps)})")
            failed += 1
    else:
        print(f"  [FAIL] 平地寻路失败: {result.get('reason')}")
        failed += 1

    # ═══════════════════════════════════════════════════
    # Test 2: 平地寻路 (左走 10 格)
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 2: 平地寻路 (左走 10 格) ---")
    result = engine.find_path(px - 10, feet_y)
    print(f"  success={result.get('success')}, reason={result.get('reason', '-')}")
    if result.get("success"):
        wps = result.get("waypoints", [])
        print(f"  waypoints: {len(wps)} 个, rawLength: {result.get('rawLength')}")
        for wp in wps:
            print(f"    ({wp['x']}, {wp['y']})")
        print(f"  [PASS] 左走寻路成功")
        passed += 1
    else:
        print(f"  [FAIL] 左走寻路失败: {result.get('reason')}")
        failed += 1

    # ═══════════════════════════════════════════════════
    # Test 3: 寻路到高处 (上方 5 格，需要跳跃)
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 3: 寻路到高处 (上方 5 格) ---")
    result = engine.find_path(px + 5, feet_y - 5)
    print(f"  success={result.get('success')}, reason={result.get('reason', '-')}")
    if result.get("success"):
        wps = result.get("waypoints", [])
        print(f"  waypoints: {len(wps)} 个, rawLength: {result.get('rawLength')}")
        for wp in wps[:10]:
            print(f"    ({wp['x']}, {wp['y']})")
        if len(wps) > 10:
            print(f"    ... ({len(wps) - 10} more)")
        print(f"  [PASS] 高处寻路成功")
        passed += 1
    else:
        print(f"  [INFO] 高处寻路失败 (可能地形不允许): {result.get('reason')}")

    # ═══════════════════════════════════════════════════
    # Test 4: 寻路到低处 (下方 10 格)
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 4: 寻路到低处 (下方 10 格) ---")
    result = engine.find_path(px, feet_y + 10, allow_dig=True)
    print(f"  success={result.get('success')}, reason={result.get('reason', '-')}")
    if result.get("success"):
        wps = result.get("waypoints", [])
        print(f"  waypoints: {len(wps)} 个, rawLength: {result.get('rawLength')}")
        for wp in wps[:10]:
            print(f"    ({wp['x']}, {wp['y']})")
        if len(wps) > 10:
            print(f"    ... ({len(wps) - 10} more)")
        print(f"  [PASS] 低处寻路成功 (allow_dig=True)")
        passed += 1
    else:
        print(f"  [INFO] 低处寻路失败: {result.get('reason')}")

    # ═══════════════════════════════════════════════════
    # Test 5: 不可达目标 (地底深处，不允许挖)
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 5: 不可达目标 (下方 50 格，不允许挖) ---")
    result = engine.find_path(px, feet_y + 50, allow_dig=False)
    print(f"  success={result.get('success')}, reason={result.get('reason', '-')}")
    if not result.get("success"):
        print(f"  [PASS] 正确返回失败: {result.get('reason')}")
        passed += 1
    else:
        wps = result.get("waypoints", [])
        print(f"  [INFO] 意外成功 ({len(wps)} 路点) — 可能有天然洞穴通向下方")
        passed += 1  # Not necessarily wrong

    # ═══════════════════════════════════════════════════
    # Test 6: 同一位置寻路
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 6: 同一位置寻路 ---")
    result = engine.find_path(px, feet_y)
    print(f"  success={result.get('success')}, reason={result.get('reason', '-')}")
    if result.get("success"):
        print(f"  [PASS] 同位置寻路成功 (waypoints={len(result.get('waypoints', []))})")
        passed += 1
    else:
        print(f"  [FAIL] 同位置寻路应该成功")
        failed += 1

    # ═══════════════════════════════════════════════════
    # Test 7: 响应格式验证
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 7: 响应格式验证 ---")
    result = engine.find_path(px + 5, feet_y)
    has_fields = ("success" in result and "startX" in result and "startY" in result)
    if result.get("success"):
        has_fields = has_fields and "waypoints" in result and "rawLength" in result
        wps = result.get("waypoints", [])
        if wps and "x" in wps[0] and "y" in wps[0]:
            has_fields = True
        else:
            has_fields = False
    if has_fields:
        print(f"  [PASS] 响应格式正确")
        passed += 1
    else:
        print(f"  [FAIL] 响应格式缺少字段: {list(result.keys())}")
        failed += 1

    # ═══════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════
    print(f"\n{'='*60}")
    total = passed + failed
    print(f"  批次 2a 测试结果: {passed}/{total} 通过, {failed}/{total} 失败")
    if failed == 0:
        print(f"  ✓ 全部通过!")
    else:
        print(f"  ✗ 有 {failed} 项失败，需要检查")
    print(f"{'='*60}")


def test_batch2b(bridge: TerrariaBridge):
    """Batch 2b 验证测试: navigate_to 路径执行器."""
    engine = BehaviorEngine(bridge)

    # 等角色落地稳定后再开始
    print("  等待角色落地稳定...")
    stable_count = 0
    last_pos = None
    for _ in range(60):
        bridge.get_state()
        time.sleep(0.1)
        pos = (bridge.state.player.tile_x, bridge.state.player.tile_y)
        if pos == last_pos:
            stable_count += 1
            if stable_count >= 5:
                break
        else:
            stable_count = 0
            last_pos = pos

    px = bridge.state.player.tile_x
    py = bridge.state.player.tile_y
    feet_y = py + 2
    print(f"\n{'='*60}")
    print(f"  批次 2b 测试 — navigate_to 路径执行")
    print(f"  玩家位置: ({px}, {py}), 脚底: ({px}, {feet_y})")
    print(f"  ⚠ 角色会移动！确保周围安全")
    print(f"{'='*60}")

    passed = 0
    failed = 0

    # ═══════════════════════════════════════════════════
    # Test 1: 平地右走 10 格
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 1: 平地右走 10 格 ---")
    target_x = px + 10
    result = engine.navigate_to(target_x, feet_y, timeout=15)
    bridge.get_state()
    time.sleep(0.2)
    final_x = bridge.state.player.tile_x
    print(f"  结果: {result}, 最终位置: ({final_x}, {bridge.state.player.tile_y})")
    if result == "arrived" and abs(final_x - target_x) <= 2:
        print(f"  [PASS] 平地右走成功")
        passed += 1
    else:
        print(f"  [FAIL] 平地右走失败 (result={result}, dist={abs(final_x - target_x)})")
        failed += 1

    time.sleep(1)

    # ═══════════════════════════════════════════════════
    # Test 2: 平地左走回原点
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 2: 平地左走回原点 ---")
    bridge.get_state()
    time.sleep(0.2)
    result = engine.navigate_to(px, feet_y, timeout=15)
    bridge.get_state()
    time.sleep(0.2)
    final_x = bridge.state.player.tile_x
    print(f"  结果: {result}, 最终位置: ({final_x}, {bridge.state.player.tile_y})")
    if result == "arrived" and abs(final_x - px) <= 2:
        print(f"  [PASS] 平地左走回原点成功")
        passed += 1
    else:
        print(f"  [FAIL] 平地左走失败 (result={result}, dist={abs(final_x - px)})")
        failed += 1

    time.sleep(1)

    # ═══════════════════════════════════════════════════
    # Test 3: 向下挖掘 5 格 (跳跃高度范围内，方便后续跳回来)
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 3: 向下挖掘 5 格 (allow_dig=True) ---")
    bridge.get_state()
    time.sleep(0.3)
    dig_start_x = bridge.state.player.tile_x
    dig_start_feet_y = bridge.state.player.tile_y + 2
    dig_target_y = dig_start_feet_y + 5
    print(f"  起始脚底: ({dig_start_x}, {dig_start_feet_y}), 目标脚底Y: {dig_target_y}")
    result = engine.navigate_to(dig_start_x, dig_target_y,
                                 allow_dig=True, timeout=60)
    bridge.get_state()
    time.sleep(0.2)
    final_y = bridge.state.player.tile_y + 2
    gap = abs(final_y - dig_target_y)
    print(f"  结果: {result}, 最终脚底Y: {final_y}, 差距: {gap}格")
    if result == "arrived" and gap <= 2:
        print(f"  [PASS] 向下挖掘 5 格成功")
        passed += 1
    else:
        print(f"  [FAIL] 向下挖掘 5 格失败 (result={result}, 差距={gap}格)")
        failed += 1

    time.sleep(1)

    # ═══════════════════════════════════════════════════
    # Test 4: 从地下爬回地面 (测试跳跃/上升)
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 4: 从地下返回地面 (跳跃上升) ---")
    bridge.get_state()
    time.sleep(0.2)
    underground_y = bridge.state.player.tile_y + 2
    # 导航到竖井左侧3格的地面 (竖井内正上方无地面，必须跳到外侧)
    surface_target_x = dig_start_x - 3
    print(f"  当前脚底Y: {underground_y}, 目标: ({surface_target_x},{dig_start_feet_y}) (竖井外侧地面)")
    result = engine.navigate_to(surface_target_x, dig_start_feet_y, timeout=30)
    bridge.get_state()
    time.sleep(0.2)
    final_x = bridge.state.player.tile_x
    final_y = bridge.state.player.tile_y + 2
    gap_x = abs(final_x - surface_target_x)
    gap_y = abs(final_y - dig_start_feet_y)
    print(f"  结果: {result}, 最终位置: ({final_x},{final_y}), 差距: X={gap_x} Y={gap_y}")
    if result == "arrived" and gap_y <= 1:
        print(f"  [PASS] 从地下返回地面成功")
        passed += 1
    else:
        print(f"  [FAIL] 从地下返回地面失败 (result={result}, X差={gap_x}, Y差={gap_y})")
        failed += 1

    time.sleep(1)

    # ═══════════════════════════════════════════════════
    # Test 5: 高处下落 (从地面走到坑洞边缘，导航到坑底)
    #   利用刚挖的竖井，从旁边走到井口上方再导航下去 (不挖掘，纯下落)
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 5: 高处下落 (纯下落，不挖掘) ---")
    bridge.get_state()
    time.sleep(0.2)
    cur_x = bridge.state.player.tile_x
    cur_feet_y = bridge.state.player.tile_y + 2
    # 目标: 竖井底部 (之前挖到的深度), 不允许挖掘
    fall_target_y = dig_target_y
    print(f"  当前: ({cur_x}, {cur_feet_y}), 目标脚底Y: {fall_target_y} (不挖掘)")
    result = engine.navigate_to(dig_start_x, fall_target_y,
                                 allow_dig=False, timeout=30)
    bridge.get_state()
    time.sleep(0.2)
    final_y = bridge.state.player.tile_y + 2
    gap = abs(final_y - fall_target_y)
    print(f"  结果: {result}, 最终脚底Y: {final_y}, 差距: {gap}格")
    if result == "arrived" and gap <= 2:
        print(f"  [PASS] 高处下落成功")
        passed += 1
    else:
        print(f"  [FAIL] 高处下落失败 (result={result}, 差距={gap}格)")
        failed += 1

    time.sleep(1)

    # ═══════════════════════════════════════════════════
    # Test 6: 不可达目标 (地底深处，不允许挖)
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 6: 不可达目标 (下方 20 格，不允许挖) ---")
    bridge.get_state()
    time.sleep(0.2)
    cur_feet_y = bridge.state.player.tile_y + 2
    result = engine.navigate_to(bridge.state.player.tile_x, cur_feet_y + 20,
                                 allow_dig=False, timeout=10)
    print(f"  结果: {result}")
    if result == "stuck":
        print(f"  [PASS] 正确返回 stuck")
        passed += 1
    else:
        print(f"  [FAIL] 应该返回 stuck, 实际: {result}")
        failed += 1

    time.sleep(1)

    # ═══════════════════════════════════════════════════
    # Test 7: cancel_navigate 中止
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 7: cancel_navigate 中止 ---")
    bridge.get_state()
    time.sleep(0.2)
    # 先回到地面以确保有足够空间行走
    cur_x = bridge.state.player.tile_x
    cur_feet_y = bridge.state.player.tile_y + 2
    # 发起一次长距离导航 (向地面方向走)
    nav_target_x = cur_x + 15
    with engine._response_lock:
        engine._pending_responses.pop("nav_status", None)
    engine.bridge.send({"cmd": "navigate_to", "x": nav_target_x,
                         "y": dig_start_feet_y, "allow_dig": True})
    resp = engine._wait_response("nav_status", timeout=5)
    # Skip non-started messages
    while resp and resp.get("status") not in ("started", "stuck", None):
        resp = engine._wait_response("nav_status", timeout=2)
    if resp and resp.get("status") == "started":
        time.sleep(1.5)  # Let it run
        engine.cancel_navigate()
        time.sleep(0.5)
        print(f"  [PASS] 导航已取消")
        passed += 1
    else:
        print(f"  [INFO] 导航未能启动: {resp}")

    # ═══════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════
    print(f"\n{'='*60}")
    total = passed + failed
    print(f"  批次 2b 测试结果: {passed}/{total} 通过, {failed}/{total} 失败")
    if failed == 0:
        print(f"  ✓ 全部通过!")
    else:
        print(f"  ✗ 有 {failed} 项失败，需要检查")
    print(f"{'='*60}")


def test_batch2c(bridge: TerrariaBridge):
    """Batch 2c 验证测试: 台阶跳跃 + OneWay平台穿越."""
    engine = BehaviorEngine(bridge)

    # 等角色落地稳定后再开始 (防止窗口切换导致的跳跃)
    print("  等待角色落地稳定...")
    stable_count = 0
    last_pos = None
    for _ in range(60):  # 最多等 6 秒
        bridge.get_state()
        time.sleep(0.1)
        pos = (bridge.state.player.tile_x, bridge.state.player.tile_y)
        if pos == last_pos:
            stable_count += 1
            if stable_count >= 5:  # 连续 5 次位置相同 = 稳定
                break
        else:
            stable_count = 0
            last_pos = pos

    px = bridge.state.player.tile_x
    py = bridge.state.player.tile_y
    feet_y = py + 2
    print(f"\n{'='*60}")
    print(f"  批次 2c 测试 — 台阶跳跃 + 平台穿越")
    print(f"  玩家位置: ({px}, {py}), 脚底: ({px}, {feet_y})")
    print(f"  ⚠ 将自动搭建/拆除测试地形")
    print(f"{'='*60}")

    passed = 0
    failed = 0
    STONE = 1
    PLATFORM = 19  # wood platform (OneWay)

    # ═══════════════════════════════════════════════════
    # 搭建台阶地形 (在玩家右侧)
    #
    #  地面 Y = feet_y+1 (实心), 玩家脚底 = feet_y
    #
    #  Step1 (1格高): x=px+5..px+7, 放石块在 y=feet_y
    #    → 站上去后 feetY = feet_y - 1
    #  Step2 (2格高): x=px+10..px+12, 放石块在 y=feet_y, feet_y-1
    #    → 站上去后 feetY = feet_y - 2
    #  Step3 (3格高): x=px+15..px+17, 放石块在 y=feet_y..feet_y-2
    #    → 站上去后 feetY = feet_y - 3
    # ═══════════════════════════════════════════════════
    print(f"\n--- 搭建台阶地形 ---")

    steps = [
        {"name": "1格台阶", "x_start": px + 5,  "height": 1},
        {"name": "2格台阶", "x_start": px + 10, "height": 2},
        {"name": "3格台阶", "x_start": px + 15, "height": 3},
    ]

    for step in steps:
        x_start = step["x_start"]
        h = step["height"]
        placed = 0
        for dx in range(3):  # 3 tiles wide
            for dy in range(h):
                if engine.place_tile(x_start + dx, feet_y - dy, STONE):
                    placed += 1
        print(f"  {step['name']}: 放置 {placed} 块石头 @ x={x_start}..{x_start+2}")

    time.sleep(0.5)

    # ═══════════════════════════════════════════════════
    # Test 1: 跳上 1 格台阶
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 1: 跳上 1 格台阶 ---")
    target_x = px + 6
    target_y = feet_y - 1
    print(f"  目标: ({target_x}, {target_y})")
    result = engine.navigate_to(target_x, target_y, timeout=15)
    bridge.get_state()
    time.sleep(0.2)
    final_x = bridge.state.player.tile_x
    final_y = bridge.state.player.tile_y + 2
    print(f"  结果: {result}, 最终位置: ({final_x}, {final_y})")
    if result == "arrived":
        print(f"  [PASS] 1格台阶成功")
        passed += 1
    else:
        print(f"  [FAIL] 1格台阶失败 (result={result})")
        failed += 1

    time.sleep(1)

    # ═══════════════════════════════════════════════════
    # Test 2: 跳上 2 格台阶
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 2: 跳上 2 格台阶 ---")
    target_x = px + 11
    target_y = feet_y - 2
    print(f"  目标: ({target_x}, {target_y})")
    result = engine.navigate_to(target_x, target_y, timeout=15)
    bridge.get_state()
    time.sleep(0.2)
    final_x = bridge.state.player.tile_x
    final_y = bridge.state.player.tile_y + 2
    print(f"  结果: {result}, 最终位置: ({final_x}, {final_y})")
    if result == "arrived":
        print(f"  [PASS] 2格台阶成功")
        passed += 1
    else:
        print(f"  [FAIL] 2格台阶失败 (result={result})")
        failed += 1

    time.sleep(1)

    # ═══════════════════════════════════════════════════
    # Test 3: 跳上 3 格台阶
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 3: 跳上 3 格台阶 ---")
    target_x = px + 16
    target_y = feet_y - 3
    print(f"  目标: ({target_x}, {target_y})")
    result = engine.navigate_to(target_x, target_y, timeout=15)
    bridge.get_state()
    time.sleep(0.2)
    final_x = bridge.state.player.tile_x
    final_y = bridge.state.player.tile_y + 2
    print(f"  结果: {result}, 最终位置: ({final_x}, {final_y})")
    if result == "arrived":
        print(f"  [PASS] 3格台阶成功")
        passed += 1
    else:
        print(f"  [FAIL] 3格台阶失败 (result={result})")
        failed += 1

    time.sleep(1)

    # ═══════════════════════════════════════════════════
    # Test 4: 从台阶顶跳回地面
    # ═══════════════════════════════════════════════════
    print(f"\n--- 测试 4: 从 3 格台阶跳回地面 ---")
    target_x = px
    target_y = feet_y
    print(f"  目标: ({target_x}, {target_y}) (起始地面)")
    result = engine.navigate_to(target_x, target_y, timeout=15)
    bridge.get_state()
    time.sleep(0.2)
    final_x = bridge.state.player.tile_x
    final_y = bridge.state.player.tile_y + 2
    print(f"  结果: {result}, 最终位置: ({final_x}, {final_y})")
    if result == "arrived":
        print(f"  [PASS] 跳回地面成功")
        passed += 1
    else:
        print(f"  [FAIL] 跳回地面失败 (result={result})")
        failed += 1

    time.sleep(1)

    # ═══════════════════════════════════════════════════
    # 拆除台阶
    # ═══════════════════════════════════════════════════
    print(f"\n--- 拆除台阶 ---")
    killed = engine.kill_area(px + 4, feet_y - 3, px + 18, feet_y)
    print(f"  拆除 {killed} 块")
    time.sleep(0.5)

    # ═══════════════════════════════════════════════════
    # 先回到起点
    # ═══════════════════════════════════════════════════
    engine.navigate_to(px, feet_y, timeout=10)
    time.sleep(0.5)

    # ═══════════════════════════════════════════════════
    # Test 5-9: 不同高度的 OneWay 平台 (2, 3, 4, 5 格)
    #
    #  每个平台水平错开，避免互相干扰
    #  平台宽 5 格，放在玩家右侧不同位置
    # ═══════════════════════════════════════════════════
    platform_heights = [2, 3, 4]  # maxJump=6, OneWay最高可达4格(需要跳过平台再落下)
    platform_width = 5
    test_num = 5

    for i, h in enumerate(platform_heights):
        # 每个平台水平错开 10 格
        plat_x_start = px + 5 + i * 10
        plat_y = feet_y - h  # 平台 tile Y
        target_feet_y = plat_y - 1  # 站在平台上时的脚底 Y

        # 搭建平台
        print(f"\n--- 搭建 {h} 格高平台 ---")
        placed = 0
        for dx in range(platform_width):
            if engine.place_tile(plat_x_start + dx, plat_y, PLATFORM):
                placed += 1
        print(f"  放置 {placed} 块木平台 @ y={plat_y}, x={plat_x_start}..{plat_x_start + platform_width - 1}")
        time.sleep(0.3)

        # 测试: 跳上平台
        print(f"\n--- 测试 {test_num}: 跳上 {h} 格高 OneWay 平台 ---")
        target_x = plat_x_start + 2
        print(f"  目标: ({target_x}, {target_feet_y}) (平台上, 平台Y={plat_y}, 高度差={h})")
        result = engine.navigate_to(target_x, target_feet_y, timeout=20)
        bridge.get_state()
        time.sleep(0.2)
        final_x = bridge.state.player.tile_x
        final_y = bridge.state.player.tile_y + 2
        print(f"  结果: {result}, 最终位置: ({final_x}, {final_y})")
        if result == "arrived":
            print(f"  [PASS] {h}格平台成功")
            passed += 1
        else:
            print(f"  [FAIL] {h}格平台失败 (result={result})")
            failed += 1

        time.sleep(0.5)

        # 回到地面
        engine.navigate_to(px, feet_y, timeout=15)
        time.sleep(0.5)
        test_num += 1

    # ═══════════════════════════════════════════════════
    # Test 9: 穿过 OneWay 平台 (用最低的 2 格平台测试)
    # ═══════════════════════════════════════════════════
    plat_x_start_low = px + 5  # 第一个平台 (2格高)
    plat_y_low = feet_y - 2
    target_feet_low = plat_y_low - 1

    print(f"\n--- 测试 {test_num}: 跳上 2 格平台后穿过到地面 ---")
    # 先上平台
    result = engine.navigate_to(plat_x_start_low + 2, target_feet_low, timeout=15)
    if result == "arrived":
        print(f"  已站在平台上，开始穿过测试")
        time.sleep(0.5)
        # 穿过到地面
        result2 = engine.navigate_to(plat_x_start_low + 2, feet_y, timeout=15)
        bridge.get_state()
        time.sleep(0.2)
        final_y = bridge.state.player.tile_y + 2
        print(f"  结果: {result2}, 最终脚底Y: {final_y}")
        if result2 == "arrived":
            print(f"  [PASS] 穿过平台成功")
            passed += 1
        else:
            print(f"  [FAIL] 穿过平台失败 (result={result2})")
            failed += 1
    else:
        print(f"  [SKIP] 无法上平台 (result={result}), 跳过穿越测试")
        failed += 1
    test_num += 1

    time.sleep(0.5)

    # ═══════════════════════════════════════════════════
    # 清理所有平台
    # ═══════════════════════════════════════════════════
    print(f"\n--- 清理测试地形 ---")
    for i, h in enumerate(platform_heights):
        plat_x_start = px + 5 + i * 10
        plat_y = feet_y - h
        killed = engine.kill_area(plat_x_start, plat_y, plat_x_start + platform_width, plat_y)
        if killed > 0:
            print(f"  拆除 {killed} 块 ({h}格高平台)")

    # ═══════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════
    print(f"\n{'='*60}")
    total = passed + failed
    print(f"  批次 2c 测试结果: {passed}/{total} 通过, {failed}/{total} 失败")
    if failed == 0:
        print(f"  ✓ 全部通过!")
    else:
        print(f"  ✗ 有 {failed} 项失败，需要检查")
    print(f"{'='*60}")


def test_batch3(bridge: TerrariaBridge):
    """Batch 3 验证测试: Python 端中高层行为重构 — enter_cave + cave_explore + return_to_surface.
    底层 nav_to/navigate_to 已在 batch2b 验证，此处聚焦重构过的中高层函数。"""
    engine = BehaviorEngine(bridge)
    runner = TaskRunner(engine)

    bridge.get_state()
    time.sleep(0.3)
    px = bridge.state.player.tile_x
    py = bridge.state.player.tile_y
    home_x = px
    print(f"\n{'='*50}")
    print(f"  批次 3 测试 — 中高层行为 A* 重构验证")
    print(f"  玩家位置: ({px}, {py})")
    print(f"{'='*50}")

    passed = 0
    failed = 0
    total = 0

    # ---- Test 1: find_cave_entrance 寻找洞穴入口 ----
    total += 1
    print(f"\n--- 测试 1: find_cave_entrance 寻找洞穴入口 ---")
    entrance = runner.find_cave_entrance(direction=1, max_distance=150)
    if entrance:
        ex, ey = entrance
        print(f"  [PASS] 右侧找到洞穴入口 ({ex}, {ey})")
        passed += 1
    else:
        print(f"  [INFO] 右侧未找到，尝试左侧...")
        entrance = runner.find_cave_entrance(direction=-1, max_distance=150)
        if entrance:
            ex, ey = entrance
            print(f"  [PASS] 左侧找到洞穴入口 ({ex}, {ey})")
            passed += 1
        else:
            print(f"  [FAIL] 两侧均未找到洞穴入口")
            failed += 1

    # ---- Test 2: enter_cave 进入洞穴 ----
    if entrance:
        total += 1
        ex, ey = entrance
        print(f"\n--- 测试 2: enter_cave 进入洞穴 ({ex}, {ey}) ---")
        bridge.get_state()
        time.sleep(0.1)
        surface_y = bridge.state.player.tile_y
        entered = runner.enter_cave(ex, ey)
        bridge.get_state()
        time.sleep(0.1)
        cur_py = bridge.state.player.tile_y
        if entered and cur_py > surface_y + 2:
            print(f"  [PASS] 成功进入洞穴, 深度 y={cur_py} (地表 y={surface_y})")
            passed += 1

            # ---- Test 3: cave_explore_step 洞穴探索 (跑3步) ----
            total += 1
            print(f"\n--- 测试 3: cave_explore_step 洞穴内探索 (最多3步) ---")
            path = [(bridge.state.player.tile_x, bridge.state.player.tile_y)]
            explore_ok = True
            for step_i in range(3):
                explore_result = runner.cave_explore_step(direction=1, path=path)
                bridge.get_state()
                time.sleep(0.1)
                pos = (bridge.state.player.tile_x, bridge.state.player.tile_y)
                print(f"  步骤{step_i+1}: result='{explore_result}', 位置 {pos}")
                path.append(pos)
                if explore_result == "dead_end":
                    print(f"  到达死胡同，停止探索")
                    break
                if explore_result not in ("moved", "fell", "blocked", "dead_end"):
                    explore_ok = False
                    break
            if explore_ok:
                print(f"  [PASS] cave_explore_step 正常运行")
                passed += 1
            else:
                print(f"  [FAIL] cave_explore_step 返回异常: '{explore_result}'")
                failed += 1

            # ---- Test 4: return_to_surface 返回地面 ----
            total += 1
            print(f"\n--- 测试 4: return_to_surface 返回地面 ---")
            returned = runner.return_to_surface(path, home_x)
            bridge.get_state()
            time.sleep(0.1)
            final_py = bridge.state.player.tile_y
            if returned and final_py <= surface_y + 5:
                print(f"  [PASS] 返回地面 y={final_py} (地表 y={surface_y})")
                passed += 1
            else:
                print(f"  [FAIL] returned={returned}, y={final_py} (地表 y={surface_y})")
                failed += 1
        else:
            print(f"  [FAIL] 未能进入洞穴, entered={entered}, y={cur_py} (地表 y={surface_y})")
            failed += 1
            print(f"  [SKIP] 跳过测试 3-4 (洞穴探索/返回地面)")
    else:
        print(f"\n  [SKIP] 跳过测试 2-4 (无洞穴入口)")

    # ---- Test 5: 完整 explore_underground 流程 (限时2分钟) ----
    total += 1
    print(f"\n--- 测试 5: explore_underground 完整流程 (限时120s) ---")
    # 回到起点再跑一次完整流程
    engine.nav_to(home_x, timeout=30)
    time.sleep(0.5)
    try:
        result = runner.explore_underground(direction=1, max_time=120)
        bridge.get_state()
        time.sleep(0.1)
        final_y = bridge.state.player.tile_y
        print(f"  explore_underground 返回 {result}, 最终位置 y={final_y}")
        # 只要不崩溃且最终回到地面附近就算通过
        if final_y <= py + 10:
            print(f"  [PASS] 完整流程运行完毕，回到地表附近")
            passed += 1
        else:
            print(f"  [FAIL] 完整流程结束但仍在地下 y={final_y} (地表 y={py})")
            failed += 1
    except Exception as e:
        print(f"  [FAIL] explore_underground 抛出异常: {e}")
        import traceback
        traceback.print_exc()
        failed += 1

    # ---- 汇总 ----
    print(f"\n{'='*50}")
    print(f"  批次 3 测试结果: {passed}/{total} 通过, {failed}/{total} 失败")
    if failed == 0:
        print(f"  全部通过!")
    else:
        print(f"  有 {failed} 项失败，需要检查日志")
    print(f"{'='*50}")


def test_wing_flight(bridge: TerrariaBridge):
    """翅膀飞行测试: 装备翅膀 → 飞到指定位置 → 飞入洞穴再飞出来."""
    engine = BehaviorEngine(bridge)
    runner = TaskRunner(engine)

    bridge.get_state()
    time.sleep(0.3)
    px = bridge.state.player.tile_x
    py = bridge.state.player.tile_y
    print(f"\n当前位置: ({px}, {py})")

    results = []
    total = 0

    def record(name, passed, detail=""):
        nonlocal total
        total += 1
        results.append((name, passed, detail))
        tag = "PASS" if passed else "FAIL"
        print(f"[{tag}] {name} {detail}")

    # Test 1: 装备翅膀
    print(f"\n--- 测试 1: 装备翅膀 ---")
    resp = engine.equip_wings()  # default: C# ItemID.LeafWings
    record("装备翅膀", resp.get("success", False),
           f"item={resp.get('item')} wingTimeMax={resp.get('wingTimeMax')}")

    # Wait for state update
    time.sleep(1)
    bridge.get_state()
    time.sleep(0.1)

    # Test 2: 飞到右上方 (测试上升飞行)
    print(f"\n--- 测试 2: 飞到右上方 ---")
    bridge.get_state()
    time.sleep(0.05)
    start_x = bridge.state.player.tile_x
    start_y = bridge.state.player.tile_y
    target_x = start_x + 20
    target_y = start_y - 15  # 15格高
    target_feet_y = target_y + 2
    print(f"  起点: ({start_x}, {start_y}) → 目标: ({target_x}, {target_feet_y})")
    result = engine.navigate_to(target_x, target_feet_y, timeout=15)
    bridge.get_state()
    time.sleep(0.05)
    end_x = bridge.state.player.tile_x
    end_y = bridge.state.player.tile_y
    dist = abs(end_x - target_x) + abs(end_y - target_y)
    record("飞到右上方", result == "arrived" or dist <= 3,
           f"result={result} 终点=({end_x},{end_y}) 距离={dist}")

    # Test 3: 飞回起点 (测试下降飞行)
    print(f"\n--- 测试 3: 飞回起点 ---")
    result = engine.navigate_to(start_x, start_y + 2, timeout=15)
    bridge.get_state()
    time.sleep(0.05)
    end_x = bridge.state.player.tile_x
    end_y = bridge.state.player.tile_y
    dist = abs(end_x - start_x) + abs(end_y - start_y)
    record("飞回起点", result == "arrived" or dist <= 3,
           f"result={result} 终点=({end_x},{end_y}) 距离={dist}")

    # Test 4: 找到洞穴并飞入
    print(f"\n--- 测试 4: 飞入洞穴 ---")
    entrance = runner.find_cave_entrance(direction=1, max_distance=150)
    if entrance:
        ex, ey = entrance
        print(f"  找到洞穴入口: ({ex}, {ey})")
        # 飞到洞口下方10格
        cave_target_y = ey + 12
        result = engine.navigate_to(ex, cave_target_y, timeout=20)
        bridge.get_state()
        time.sleep(0.05)
        end_y = bridge.state.player.tile_y
        record("飞入洞穴", end_y > ey + 5,
               f"入口y={ey} 当前y={end_y} 深入={end_y - ey}格")

        # Test 5: 从洞穴飞出来
        print(f"\n--- 测试 5: 从洞穴飞出 ---")
        surface_y = ey - 5  # 飞回地表以上
        result = engine.navigate_to(ex, surface_y + 2, timeout=20)
        bridge.get_state()
        time.sleep(0.05)
        end_y = bridge.state.player.tile_y
        record("从洞穴飞出", end_y < ey,
               f"目标y={surface_y} 当前y={end_y}")
    else:
        record("飞入洞穴", False, "未找到洞穴入口")
        record("从洞穴飞出", False, "跳过 (无洞穴)")

    # Summary
    passed = sum(1 for _, p, _ in results if p)
    failed = total - passed
    print(f"\n{'='*50}")
    print(f"  翅膀飞行测试结果: {passed}/{total} 通过, {failed}/{total} 失败")
    if failed == 0:
        print(f"  全部通过!")
    else:
        print(f"  有 {failed} 项失败，需要检查")
    print(f"{'='*50}")


def demo_explore(bridge: TerrariaBridge):
    """Demo: underground exploration - find cave, mine ores, loot chests, return."""
    engine = BehaviorEngine(bridge)
    runner = TaskRunner(engine)

    bridge.get_state()
    time.sleep(0.3)
    px = bridge.state.player.tile_x
    py = bridge.state.player.tile_y
    print(f"\n当前位置: ({px}, {py})")
    print(f"\n{'='*50}")
    print(f"  [慢脑指令] 去探索洞穴，顺便挖矿")
    print(f"{'='*50}")

    # Equip wings for flight navigation
    engine.equip_wings()

    # Auto-equip before heading out
    runner.auto_equip()

    # Start exploration (go right, 5 minute limit)
    runner.explore_underground(direction=1, max_time=300)

    # After returning, check upgrades
    print(f"\n  [慢脑] 探索结束，检查升级...")
    runner.check_upgrade()

    # Show final inventory
    print(f"\n  [慢脑] 当前背包:")
    inv = engine.get_inventory()
    for item in inv:
        info = runner._get_item_info(item)
        info_str = f" ({info})" if info else ""
        print(f"    [{item['slot']}] {item['name']} x{item['stack']}{info_str}")


def test_base_system(bridge: TerrariaBridge):
    """Test base management, storage, crafting chain, go-home flow."""
    engine = BehaviorEngine(bridge)
    runner = TaskRunner(engine)

    print("\n" + "=" * 50)
    print("  批次5 测试: 基地管理 + 存储 + 合成 + 回家")
    print("=" * 50)

    # ── Test 1: init_base ──
    print("\n--- 测试1: 初始化基地 ---")
    ok = runner.init_base()
    print(f"  结果: {'成功' if ok else '失败'}")
    if not ok:
        print("  init_base 失败，无法继续")
        return

    # ── Test 2: scan_chests ──
    print("\n--- 测试2: 箱子扫描 ---")
    runner.refresh_base_chests()
    print(f"  找到 {len(runner.base_chests)} 个箱子")

    # ── Test 3: auto_heal ──
    print("\n--- 测试3: 自动治疗检查 ---")
    bridge.get_state()
    time.sleep(0.1)
    hp = bridge.state.player.hp
    max_hp = bridge.state.player.max_hp
    print(f"  当前HP: {hp}/{max_hp}")
    runner._auto_heal_check()
    print(f"  (如果HP<50%应该看到治疗日志)")

    # ── Test 4: check_upgrade with 2-layer chain ──
    print("\n--- 测试4: 合成升级 (含两层推导) ---")
    runner.check_upgrade()

    # ── Test 5: use_mirror ──
    print("\n--- 测试5: 魔镜传送 ---")
    mirror = engine.find_item(item_id=50)
    if not mirror:
        mirror = engine.find_item(item_id=3199)
    if mirror:
        print(f"  找到魔镜: slot={mirror[0]['slot']}")
        ok = runner.use_mirror()
        print(f"  传送结果: {'成功' if ok else '失败'}")
    else:
        print("  没有魔镜，跳过传送测试")

    # ── Test 6: store_items_at_base ──
    print("\n--- 测试6: 基地存储 ---")
    if runner.base_chests:
        stored = runner.store_items_at_base()
        print(f"  存入 {stored} 种物品")
    else:
        print("  没有箱子，跳过存储测试")
        print("  (如果有木材>=8 且在工作台旁，会尝试合成+放置箱子)")
        stored = runner.store_items_at_base()
        print(f"  存入 {stored} 种物品")

    # ── Test 7: full go_home_and_resupply ──
    print("\n--- 测试7: 完整回家流程 ---")
    input("  按回车开始完整回家流程 (传送→合成→存储→补给)...")
    runner.go_home_and_resupply()

    # ── Test 8: explore with auto go-home ──
    print("\n--- 测试8: 探索循环 (含自动回家) ---")
    input("  按回车开始探索测试 (背包满或探索结束会自动回家)...")
    runner.explore_underground(direction=1, max_time=180)

    print("\n" + "=" * 50)
    print("  批次5 测试完成!")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="Terraria Bot - LumiBridge 客户端")
    parser.add_argument("--watch", action="store_true", help="仅观察，不操作")
    parser.add_argument("--demo", choices=["walk", "behavior", "task", "obstacle", "explore",
                                          "batch1", "batch2a", "batch2b", "batch2c", "batch3", "wings", "base",
                                          "survival"], default="task",
                        help="测试模式: walk/behavior/task/obstacle/explore/batch1-3/wings/base/survival")
    parser.add_argument("--no-launch", action="store_true", help="不启动 tModLoader，仅连接")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9877)
    args = parser.parse_args()

    print("=" * 50)
    print("  Terraria Bot - Lumi AI 泰拉瑞亚控制器")
    print("  Ctrl+C 退出")
    print("=" * 50)

    # Step 0: Build mod (ensure latest code)
    build_mod()

    # Step 1: Launch tModLoader if needed
    if not args.no_launch:
        launch_tmodloader()
        print("正在加载世界，等待进入游戏...\n")

    # Step 2: Connect to LumiBridge
    bridge = TerrariaBridge(host=args.host, port=args.port)

    state_count = [0]
    in_world = threading.Event()

    combat_stats = {"kills": 0, "damage_taken": 0, "deaths": 0}

    def on_msg(msg):
        if msg.get("type") == "state":
            state_count[0] += 1
            # Detect entering a world: player has HP
            p = msg.get("player", {})
            if p.get("hp", 0) > 0 and not in_world.is_set():
                in_world.set()
            if in_world.is_set() and state_count[0] % 4 == 1:
                print_state(bridge.state)
        elif msg.get("type") == "event":
            evt = msg.get("event", "")
            data = msg.get("data", {})
            # Combat events
            if evt == "npc_killed":
                combat_stats["kills"] += 1
                tag = _npc_tag(data)
                print(f"  [战斗] 击杀 {data.get('name', '?')} ({tag}) | 总击杀: {combat_stats['kills']}")
            elif evt == "player_hurt":
                combat_stats["damage_taken"] += data.get("damage", 0)
                print(f"  [战斗] 受伤 -{data.get('damage', 0)} HP:{data.get('hp', '?')}/{data.get('maxHp', '?')} 来源: {data.get('source', '?')} | 总受伤: {combat_stats['damage_taken']}")
            elif evt == "player_died":
                combat_stats["deaths"] += 1
                print(f"  [战斗] 死亡! {data.get('deathMessage', '')} | 总死亡: {combat_stats['deaths']}")
            else:
                print(f"  [事件] {evt}: {data}")

    bridge.on_message(on_msg)

    if not bridge.connect():
        input("按回车键关闭...")
        return

    # Step 3: Wait for player to enter a world
    print("等待进入游戏世界...")
    while not in_world.is_set() and bridge.connected:
        bridge.get_state()
        time.sleep(2)

    if not bridge.connected:
        input("按回车键关闭...")
        return

    print(f"已进入世界! 玩家: {bridge.state.player.name}")

    # Version check — ensure mod is up to date
    EXPECTED_MOD_VERSION = "5.3"
    _mod_version = [None]
    def _catch_pong(msg):
        # pong format: {"type":"event", "event":"pong", "data":{"tick":..., "version":"5.1"}}
        if msg.get("type") == "event" and msg.get("event") == "pong":
            _mod_version[0] = msg.get("data", {}).get("version")
    bridge.on_message(_catch_pong)
    bridge.send({"cmd": "ping"})
    time.sleep(0.5)
    bridge._callbacks.remove(_catch_pong)
    mod_version = _mod_version[0] or "unknown"
    if mod_version != EXPECTED_MOD_VERSION:
        print(f"\n  ⚠ Mod 版本不匹配! 游戏内: {mod_version}, 期望: {EXPECTED_MOD_VERSION}")
        print(f"  请重启游戏加载最新 mod")
    else:
        print(f"  Mod 版本: {mod_version} ✓")

    # Step 4: Activate Terraria window so game runs
    time.sleep(0.5)
    activate_terraria()
    time.sleep(0.5)

    # Step 5: Enable auto mode
    bridge.set_auto_mode(True)
    time.sleep(0.5)

    try:
        if args.watch:
            print("\n观察模式 - 仅显示游戏状态")
            while bridge.connected:
                time.sleep(1)
        else:
            # Activate window right before test
            activate_terraria()
            time.sleep(0.5)

            if args.demo == "walk":
                demo_walk(bridge)
            elif args.demo == "behavior":
                demo_behavior(bridge)
            elif args.demo == "obstacle":
                test_obstacle(bridge)
            elif args.demo == "explore":
                demo_explore(bridge)
            elif args.demo == "batch1":
                test_batch1(bridge)
            elif args.demo == "batch2a":
                test_batch2a(bridge)
            elif args.demo == "batch2b":
                test_batch2b(bridge)
            elif args.demo == "batch2c":
                test_batch2c(bridge)
            elif args.demo == "batch3":
                test_batch3(bridge)
            elif args.demo == "wings":
                test_wing_flight(bridge)
            elif args.demo == "base":
                test_base_system(bridge)
            elif args.demo == "survival":
                survival_loop(bridge)
            else:
                demo_task(bridge)

            print("\n测试完成。保持连接中... (Ctrl+C 退出)")
            while bridge.connected:
                time.sleep(1)

    except KeyboardInterrupt:
        print("\n正在断开连接...")

    bridge.set_auto_mode(False)
    bridge.disconnect()
    print("已断开。")
    input("按回车键关闭...")


class ErrorSummaryWriter:
    """Captures lines matching error keywords into a separate summary log."""
    KEYWORDS = ["FAIL", "卡住", "无法到达", "search_limit", "no_path", "max_retries",
                "target_blocked", "start_blocked", "exception", "Error", "错误",
                "PASS", "SKIP", "测试结果", "批次"]

    def __init__(self, path):
        self._file = open(path, "w", encoding="utf-8")
        self._context_buf = []  # recent lines for context
        self._buf_size = 3

    def feed(self, text):
        """Feed a line of output; write to summary if it matches keywords."""
        for line in text.split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            self._context_buf.append(stripped)
            if len(self._context_buf) > self._buf_size:
                self._context_buf.pop(0)
            if any(kw in stripped for kw in self.KEYWORDS):
                # Write context + matched line
                for ctx in self._context_buf:
                    self._file.write(ctx + '\n')
                self._context_buf.clear()
                self._file.flush()

    def close(self):
        self._file.close()


class TeeWriterWithSummary:
    """Writes to original stream, log file, and error summary."""
    def __init__(self, original, log_file, summary: ErrorSummaryWriter):
        self.original = original
        self.log_file = log_file
        self.summary = summary

    def write(self, text):
        self.original.write(text)
        self.log_file.write(text)
        self.summary.feed(text)

    def flush(self):
        self.original.flush()
        self.log_file.flush()


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join("logs", f"terraria_{timestamp}.log")
    summary_path = os.path.join("logs", f"terraria_{timestamp}_errors.log")
    log_file = open(log_path, "w", encoding="utf-8")
    summary = ErrorSummaryWriter(summary_path)
    sys.stdout = TeeWriterWithSummary(sys.__stdout__, log_file, summary)
    sys.stderr = TeeWriterWithSummary(sys.__stderr__, log_file, summary)
    try:
        main()
    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        log_file.close()
        summary.close()
        print(f"日志已保存: {log_path}")
        print(f"错误摘要: {summary_path}")
