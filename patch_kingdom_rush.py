"""
Kingdom Rush Mod 注入工具
将 TCP Bridge 注入到 LÖVE 引擎的 Kingdom Rush exe 中

用法:
    python patch_kingdom_rush.py [game_folder]

示例:
    python patch_kingdom_rush.py "C:\Games\Kingdom Rush 1"

效果:
    - 备份原始 exe 为 *.exe.bak
    - 注入 bridge_server.lua（TCP 服务端）
    - 注入 wrapper main.lua（hook love.update）
    - 生成修改后的 exe
"""

import zipfile
import struct
import io
import os
import sys
import shutil
import zlib
import binascii


def _make_placeholder_png(corrupt_png_bytes):
    """损坏 PNG → 同尺寸全透明合法 PNG（从损坏数据的 IHDR 读宽高，IHDR 在开头通常完好）。
    游戏能正常加载，对应精灵显示透明而非崩溃。"""
    try:
        # 签名(8) + len(4) + 'IHDR'(4) + w(4) + h(4)
        w, h = struct.unpack('>II', corrupt_png_bytes[16:24])
        if not (0 < w <= 8192 and 0 < h <= 8192):
            return None
    except Exception:
        return None

    def _chunk(typ, body):
        return (struct.pack('>I', len(body)) + typ + body
                + struct.pack('>I', binascii.crc32(typ + body) & 0xffffffff))

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)  # 8bit RGBA
    rawimg = (b'\x00' + b'\x00' * (w * 4)) * h            # 每行: filter0 + 全透明
    idat = zlib.compress(rawimg, 9)
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr)
            + _chunk(b'IDAT', idat) + _chunk(b'IEND', b''))

# ========== Wrapper main.lua ==========
# 加载原始游戏代码，然后 hook love.update 注入 bridge
WRAPPER_MAIN_LUA = '''
-- [BridgeMod] Wrapper main.lua
-- 加载原始游戏 → hook love.update → 注入 TCP bridge

-- 1. 加载原始游戏代码（重命名的字节码 main.lua）
local chunk, err = love.filesystem.load("_kr_main_orig.lua")
if chunk then
    chunk()
else
    error("[BridgeMod] Failed to load original main: " .. tostring(err))
end

-- 2. 加载 bridge 模块
local bridge_ok, bridge = pcall(require, "bridge_server")
if not bridge_ok then
    print("[BridgeMod] WARNING: bridge_server failed to load: " .. tostring(bridge))
    return
end

-- 3. Hook love.update
--    策略: 如果 love.update 已定义，直接 wrap
--          否则 wrap love.run，在游戏循环开始前 hook
if love.update then
    print("[BridgeMod] Hooking love.update directly")
    bridge.init()
    local _orig_update = love.update
    love.update = function(dt)
        _orig_update(dt)
        bridge.update(dt)
    end
elseif love.run then
    print("[BridgeMod] Hooking via love.run wrapper")
    local _orig_run = love.run
    love.run = function()
        bridge.init()
        -- love.update 应该在 love.load 之后被定义
        local _hooked = false
        local _check_hook = function()
            if not _hooked and love.update then
                local _orig_update = love.update
                love.update = function(dt)
                    _orig_update(dt)
                    bridge.update(dt)
                end
                _hooked = true
                print("[BridgeMod] love.update hooked successfully")
            end
        end

        -- Wrap love.load to hook after initialization
        local _orig_load = love.load
        love.load = function(...)
            if _orig_load then _orig_load(...) end
            _check_hook()
        end

        return _orig_run()
    end
else
    print("[BridgeMod] WARNING: Neither love.update nor love.run found!")
    bridge.init()
end

print("[BridgeMod] Wrapper loaded successfully")
'''


def find_zip_start(exe_data):
    """找到 exe 中 ZIP 数据的起始偏移
    LÖVE fused exe 格式: [love.exe PE binary][ZIP data]
    ZIP 内部偏移已包含 PE 头大小，所以用 zipfile 读取第一个文件的 header_offset 即可
    """
    z = zipfile.ZipFile(io.BytesIO(exe_data))
    first_info = z.infolist()[0]
    z.close()
    return first_info.header_offset


def patch_exe(game_folder, bridge_lua_path=None):
    """注入 bridge 到 Kingdom Rush exe"""

    # 找到 exe 文件
    exe_files = [f for f in os.listdir(game_folder)
                 if f.endswith('.exe') and 'unins' not in f.lower()]

    if not exe_files:
        print("错误: 找不到游戏 exe 文件")
        return False

    exe_name = exe_files[0]
    exe_path = os.path.join(game_folder, exe_name)
    backup_path = exe_path + '.bak'

    print(f"目标: {exe_path}")

    # 检查是否已有备份（说明已经 patch 过）
    if os.path.exists(backup_path):
        print(f"发现备份文件，使用原始备份: {backup_path}")
        source_path = backup_path
    else:
        # 创建备份
        print(f"备份原始文件: {backup_path}")
        shutil.copy2(exe_path, backup_path)
        source_path = backup_path

    # 读取原始 exe
    with open(source_path, 'rb') as f:
        exe_data = f.read()

    print(f"原始 exe 大小: {len(exe_data):,} bytes")

    # 找到 ZIP 起始偏移
    zip_start = find_zip_start(exe_data)
    love_prefix = exe_data[:zip_start]
    print(f"LOVE binary 大小: {len(love_prefix):,} bytes")
    print(f"ZIP 起始偏移: {zip_start}")

    # 读取原始 ZIP 内容
    # 破解版可能有 CRC 不一致的文件，monkey-patch 跳过校验
    _orig_update_crc = zipfile.ZipExtFile._update_crc
    zipfile.ZipExtFile._update_crc = lambda self, newdata: None
    original_zip = zipfile.ZipFile(io.BytesIO(exe_data))
    file_list = original_zip.namelist()
    print(f"原始 ZIP 文件数: {len(file_list)}")

    # 检查是否已经被 patch 过
    if '_kr_main_orig.lua' in file_list:
        print("警告: exe 已经被 patch 过，将基于备份重新 patch")

    # 读取 bridge lua 代码
    if bridge_lua_path is None:
        bridge_lua_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        'kingdom_rush_bridge.lua')

    with open(bridge_lua_path, 'r', encoding='utf-8') as f:
        bridge_lua = f.read()

    # 创建新 ZIP
    new_zip_buffer = io.BytesIO()
    with zipfile.ZipFile(new_zip_buffer, 'w', zipfile.ZIP_DEFLATED) as new_zip:
        patched_files = set()

        for item in original_zip.infolist():
            data = original_zip.read(item.filename)

            # 自动修复破解版自带的损坏资源：CRC 不符的 PNG → 同尺寸透明占位图，
            # 否则游戏画到它(如废土关怪)会因解码失败拿 nil 贴图崩溃。
            if (binascii.crc32(data) & 0xffffffff) != item.CRC:
                if item.filename.lower().endswith('.png'):
                    ph = _make_placeholder_png(data)
                    if ph is not None:
                        data = ph
                        print(f"  [修复] 损坏PNG {item.filename} → 透明占位图")
                    else:
                        print(f"  [警告] 损坏PNG无法生成占位 {item.filename}")
                else:
                    print(f"  [警告] 损坏非PNG文件 {item.filename}（原样写入）")

            if item.filename == 'main.lua':
                # 重命名原始 main.lua
                new_zip.writestr('_kr_main_orig.lua', data)
                # 写入我们的 wrapper
                new_zip.writestr('main.lua', WRAPPER_MAIN_LUA.encode('utf-8'))
                patched_files.add('main.lua')
                print(f"  [OK] main.lua -> _kr_main_orig.lua (原始代码重命名)")
                print(f"  [OK] main.lua (注入 wrapper)")
            else:
                new_zip.writestr(item, data)

        # 添加 bridge server
        new_zip.writestr('bridge_server.lua', bridge_lua.encode('utf-8'))
        print(f"  [OK] bridge_server.lua (TCP 桥接服务端)")

    original_zip.close()
    # 恢复 CRC 校验
    zipfile.ZipExtFile._update_crc = _orig_update_crc

    # 写入新 exe
    new_zip_data = new_zip_buffer.getvalue()

    with open(exe_path, 'wb') as f:
        f.write(love_prefix)
        f.write(new_zip_data)

    new_size = len(love_prefix) + len(new_zip_data)
    print(f"\n新 exe 大小: {new_size:,} bytes")
    print(f"大小变化: +{new_size - len(exe_data):,} bytes")
    print(f"\n[OK] Patch 完成!")
    print(f"  运行游戏后 TCP 服务将监听 127.0.0.1:9878")
    print(f"  如需还原，删除 {exe_name} 并将 {exe_name}.bak 重命名回来")

    return True


def unpatch_exe(game_folder):
    """还原 patch"""
    exe_files = [f for f in os.listdir(game_folder)
                 if f.endswith('.exe.bak')]

    if not exe_files:
        print("找不到备份文件，无法还原")
        return False

    backup_name = exe_files[0]
    exe_name = backup_name[:-4]  # 去掉 .bak

    backup_path = os.path.join(game_folder, backup_name)
    exe_path = os.path.join(game_folder, exe_name)

    shutil.copy2(backup_path, exe_path)
    print(f"[OK] 已还原: {exe_name}")
    return True


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        # 默认使用 KR1
        game_folder = r"C:\Games\Kingdom Rush 1"
        if os.path.exists(game_folder):
            print(f"\n使用默认路径: {game_folder}")
        else:
            print("请提供游戏文件夹路径")
            sys.exit(1)
    else:
        if sys.argv[1] == '--unpatch':
            game_folder = sys.argv[2] if len(sys.argv) > 2 else r"C:\Games\Kingdom Rush 1"
            unpatch_exe(game_folder)
            sys.exit(0)
        game_folder = sys.argv[1]

    if not os.path.exists(game_folder):
        print(f"错误: 路径不存在: {game_folder}")
        sys.exit(1)

    patch_exe(game_folder)
