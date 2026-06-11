-- Kingdom Rush TCP Bridge Server
-- 注入到游戏中，提供状态推送和命令接收
-- 端口: 9878

local socket = require("socket")

local M = {}
local BRIDGE_VERSION = "v5.7"  -- 界面检测 + 关卡启动 + 关卡列表
local server = nil
local clients = {}
local push_interval = 0  -- 默认不自动推送，客户端可通过 set_push_interval 开启
local push_timer = 0
local initialized = false
local edb = nil  -- entity_db 模块缓存
local _last_lives = -1      -- 上帧生命值，用于检测扣命
local _life_lost_queue = {}  -- 累积扣命事件，由 get_state 消费
-- (已移除自定义士兵追踪系统，改用游戏原生 barrack.soldiers 绑定)

-- 内嵌极简 JSON 编码器（不依赖游戏自带的 json.lua）
local json = {}

local function json_encode_value(val, depth)
    depth = depth or 0
    if depth > 10 then return '"[max depth]"' end
    local t = type(val)
    if t == "nil" then return "null"
    elseif t == "boolean" then return tostring(val)
    elseif t == "number" then
        if val ~= val then return "null" end -- NaN
        if val == math.huge or val == -math.huge then return "null" end
        return tostring(val)
    elseif t == "string" then
        return '"' .. val:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t') .. '"'
    elseif t == "table" then
        -- 检测是否为数组
        local is_array = true
        local max_i = 0
        for k, _ in pairs(val) do
            if type(k) ~= "number" or k < 1 or math.floor(k) ~= k then
                is_array = false
                break
            end
            if k > max_i then max_i = k end
        end
        if max_i == 0 then is_array = false end

        local parts = {}
        if is_array then
            for i = 1, max_i do
                parts[#parts + 1] = json_encode_value(val[i], depth + 1)
            end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            for k, v in pairs(val) do
                local key = type(k) == "string" and k or tostring(k)
                parts[#parts + 1] = json_encode_value(key, depth + 1) .. ":" .. json_encode_value(v, depth + 1)
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    else
        return '"[' .. t .. ']"'
    end
end

function json.encode(val)
    return json_encode_value(val)
end

function json.decode(str)
    -- 极简 JSON 解码：用 Lua 加载（安全性对本地 bot 足够）
    str = str:gsub('"([^"]-)"%s*:', '["%1"]=')
    str = str:gsub('%[%s*%]', '{}')
    str = str:gsub('null', 'nil')
    str = str:gsub('true', 'true')
    str = str:gsub('false', 'false')
    local f = loadstring("return " .. str)
    if f then
        local ok, result = pcall(f)
        if ok then return result end
    end
    return nil
end

-- 安全获取 entity_db
local function get_edb()
    if not edb then
        local ok, result = pcall(require, "entity_db")
        if ok then edb = result end
    end
    return edb
end

-- TCP 服务器初始化
function M.init()
    if initialized then return end
    local ok, err = pcall(function()
        server = socket.tcp()
        server:setoption("reuseaddr", true)
        server:bind("127.0.0.1", 9878)
        server:listen(5)
        server:settimeout(0) -- 非阻塞
    end)
    if ok then
        initialized = true
        print("[BridgeMod] TCP server listening on 127.0.0.1:9878")
    else
        print("[BridgeMod] Failed to start TCP server: " .. tostring(err))
    end
end

-- 发送消息给指定客户端
local function send_to(client, data)
    local payload = json.encode(data) .. "\n"
    local ok, err = client:send(payload)
    if not ok then
        return false
    end
    return true
end

-- 发送消息给所有客户端
local function broadcast(data)
    local payload = json.encode(data) .. "\n"
    for i = #clients, 1, -1 do
        local ok, err = clients[i]:send(payload)
        if not ok then
            table.remove(clients, i)
        end
    end
end

-- ========== 路径工具 ==========

-- 找到距离某位置最近的路径点（在 max_dist 范围内）
local function find_nearest_path_point(px, py, max_dist)
    max_dist = max_dist or 200
    local best_x, best_y = px, py
    local best_dist_sq = max_dist * max_dist
    local ok, pd = pcall(require, "path_db")
    if ok and pd and pd.paths then
        for _, path in ipairs(pd.paths) do
            for _, seg in ipairs(path) do
                for ni = 1, #seg, 3 do
                    local n = seg[ni]
                    local dx, dy = n.x - px, n.y - py
                    local d2 = dx * dx + dy * dy
                    if d2 < best_dist_sq then
                        best_dist_sq = d2
                        best_x, best_y = n.x, n.y
                    end
                end
            end
        end
    end
    return best_x, best_y
end

-- ========== 游戏状态收集 ==========

-- 获取某位置附近经过的路径编号（与波次 path_index 对应）
local function get_nearby_paths(px, py, radius)
    radius = radius or 150
    local radius_sq = radius * radius
    local found = {}
    local ok, pd = pcall(require, "path_db")
    if ok and pd and pd.paths then
        for pi, path in ipairs(pd.paths) do
            for _, seg in ipairs(path) do
                for ni = 1, #seg, 5 do
                    local n = seg[ni]
                    local dx, dy = n.x - px, n.y - py
                    if dx * dx + dy * dy < radius_sq then
                        found[pi] = true
                        break
                    end
                end
                if found[pi] then break end
            end
        end
    end
    local result = {}
    for pi in pairs(found) do
        result[#result + 1] = pi
    end
    table.sort(result)
    return result
end

-- 计算某位置在每条附近路径上的节点位置（全局序号）和路径总长
-- 返回 {[pi] = {ni = 全局节点序号, total = 总节点数}}
local function get_path_distances(px, py, radius)
    radius = radius or 150
    local radius_sq = radius * radius
    local result = {}
    local ok, pd = pcall(require, "path_db")
    if ok and pd and pd.paths then
        for pi, path in ipairs(pd.paths) do
            -- 计算该路径总节点数 + 找最近节点的全局序号
            local total = 0
            local best_ni = nil
            local best_dist_sq = radius_sq
            local global_ni = 0
            for _, seg in ipairs(path) do
                for ni = 1, #seg do
                    global_ni = global_ni + 1
                    local n = seg[ni]
                    local dx, dy = n.x - px, n.y - py
                    local d2 = dx * dx + dy * dy
                    if d2 < best_dist_sq then
                        best_dist_sq = d2
                        best_ni = global_ni
                    end
                end
            end
            total = global_ni
            if best_ni then
                result[pi] = {ni = best_ni, total = total}
            end
        end
    end
    return result
end

-- 获取所有路径的入口坐标（第一段第一个节点）
local function get_path_entries()
    local entries = {}
    local ok, pd = pcall(require, "path_db")
    if ok and pd and pd.paths then
        for pi, path in ipairs(pd.paths) do
            if path[1] and path[1][1] then
                local n = path[1][1]
                -- 总节点数
                local total = 0
                for _, seg in ipairs(path) do
                    total = total + #seg
                end
                entries[pi] = {x = n.x, y = n.y, total = total}
            end
        end
    end
    return entries
end

-- 计算某位置的路径覆盖度（附近有多少条路径段经过）
local function calc_path_score(px, py)
    local score = 0
    local ok, pd = pcall(require, "path_db")
    if ok and pd and pd.paths then
        local radius_sq = 150 * 150
        for _, path in ipairs(pd.paths) do
            for _, seg in ipairs(path) do
                local near = false
                for ni = 1, #seg, 5 do
                    local n = seg[ni]
                    local dx, dy = n.x - px, n.y - py
                    if dx * dx + dy * dy < radius_sq then
                        near = true
                        break
                    end
                end
                if near then score = score + 1 end
            end
        end
    end
    return score
end

local function collect_towers()
    local towers = {}
    local store = game and game.store
    if not store or not store.entities then return towers end

    for id, e in pairs(store.entities) do
        if e.tower and e.tower.type ~= "holder" and e.tower.type ~= "build_animation" then
            local tx, ty = e.pos and e.pos.x or 0, e.pos and e.pos.y or 0
            -- 计算最近路径点（兵营用于集结点，英雄用于驻守点）
            local npx, npy = find_nearest_path_point(tx, ty, 200)
            -- 检查是否为关卡特殊塔（不可卖/不可改造）
            local is_special = not e.tower.can_be_sold or not e.tower.can_be_mod
            local t = {
                id = id,
                template = e.template_name,
                type = e.tower.type,
                level = e.tower.level,
                spent = e.tower.spent,
                holder_id = e.tower.holder_id,
                nearest_path_x = npx,
                nearest_path_y = npy,
                x = tx,
                y = ty,
                range = e.attacks and e.attacks.range,
                path_score = calc_path_score(tx, ty),
                nearby_paths = get_nearby_paths(tx, ty, 100),
                path_distances = get_path_distances(tx, ty, 150),
                is_special = is_special,
            }
            -- 兵营额外信息：士兵数量、集结点
            if e.barrack then
                local sc = 0
                if e.barrack.soldiers then
                    for _ in pairs(e.barrack.soldiers) do sc = sc + 1 end
                end
                t.soldier_count = sc
                t.max_soldiers = e.barrack.max_soldiers
                t.rally_range = e.barrack.rally_range
                if e.barrack.rally_pos then
                    t.rally_x = e.barrack.rally_pos.x
                    t.rally_y = e.barrack.rally_pos.y
                end
            end
            towers[#towers + 1] = t
        end
    end
    return towers
end

local function collect_holders()
    local holders = {}
    local store = game and game.store
    if not store or not store.entities then return holders end

    for id, e in pairs(store.entities) do
        if e.tower_holder then
            local hx, hy = e.pos and e.pos.x or 0, e.pos and e.pos.y or 0
            local h = {
                id = id,
                template = e.template_name,
                blocked = e.tower_holder.blocked,
                unblock_price = e.tower_holder.unblock_price,
                mesh_id = e.ui and e.ui.nav_mesh_id,
                path_score = calc_path_score(hx, hy),
                nearby_paths = get_nearby_paths(hx, hy, 100),
                path_distances = get_path_distances(hx, hy, 150),
                x = hx,
                y = hy,
            }
            holders[#holders + 1] = h
        end
    end
    return holders
end

local function collect_enemies()
    local enemies = {}
    local store = game and game.store
    if not store or not store.entities then return enemies end

    for id, e in pairs(store.entities) do
        if e.enemy and e.health and not e.health.dead then
            local en = {
                id = id,
                template = e.template_name,
                hp = e.health.hp,
                hp_max = e.health.hp_max,
                x = e.pos and e.pos.x,
                y = e.pos and e.pos.y,
                speed = e.motion and e.motion.max_speed,
                gold = e.enemy.gold,
                lives_cost = e.enemy.lives_cost,
                armor = e.health.armor,
                magic_armor = e.health.magic_armor,
            }
            -- 路径进度与所属路径
            if e.nav_path then
                en.path_ni = e.nav_path.ni        -- 节点进度
                en.path_spi = e.nav_path.spi      -- 段索引
                en.path_index = e.nav_path.pi     -- 所属路径编号
                en.path_dir = e.nav_path.dir      -- 方向
            end
            enemies[#enemies + 1] = en
        end
    end
    return enemies
end

local function collect_heroes()
    local heroes = {}
    local store = game and game.store
    if not store or not store.entities then return heroes end

    for id, e in pairs(store.entities) do
        if e.hero then
            local h = {
                id = id,
                template = e.template_name,
                level = e.hero.level,
                xp = e.hero.xp,
                hp = e.health and e.health.hp,
                hp_max = e.health and e.health.hp_max,
                dead = e.health and e.health.dead,
                x = e.pos and e.pos.x,
                y = e.pos and e.pos.y,
                rally_x = e.nav_rally and e.nav_rally.pos and e.nav_rally.pos.x,
                rally_y = e.nav_rally and e.nav_rally.pos and e.nav_rally.pos.y,
            }
            heroes[#heroes + 1] = h
        end
    end
    return heroes
end

-- 敌人模板属性缓存（避免每 tick 重复查询 entity_db）
local enemy_stats_cache = {}

local function get_enemy_stats(template_name)
    if enemy_stats_cache[template_name] then
        return enemy_stats_cache[template_name]
    end
    local db = get_edb()
    if not db then return nil end
    local ok, tmpl = pcall(db.get_template, db, template_name)
    if not ok or not tmpl then return nil end

    local stats = {
        template = template_name,
        hp = tmpl.health and tmpl.health.hp_max or tmpl.health and tmpl.health.hp or 0,
        armor = tmpl.health and tmpl.health.armor or 0,
        magic_armor = tmpl.health and tmpl.health.magic_armor or 0,
        speed = tmpl.motion and tmpl.motion.max_speed or 0,
        gold = tmpl.enemy and tmpl.enemy.gold or 0,
        lives_cost = tmpl.enemy and tmpl.enemy.lives_cost or 1,
    }
    enemy_stats_cache[template_name] = stats
    return stats
end

local function collect_next_wave()
    local store = game and game.store
    if not store then return nil end

    local nw = store.next_wave_group_ready
    if not nw or not nw.waves then return nil end

    local result = {
        group_idx = nw.group_idx,
        paths = {},
    }

    for _, wave in ipairs(nw.waves) do
        local path_info = {
            path_index = wave.path_index,
            spawns = {},
        }
        if wave.spawns then
            for _, sp in ipairs(wave.spawns) do
                local creep = sp.creep
                local count = sp.max or 1
                local spawn_info = {
                    template = creep,
                    count = count,
                }
                -- 附加敌人战斗属性
                local stats = get_enemy_stats(creep)
                if stats then
                    spawn_info.hp = stats.hp
                    spawn_info.armor = stats.armor
                    spawn_info.magic_armor = stats.magic_armor
                    spawn_info.speed = stats.speed
                    spawn_info.gold = stats.gold
                    spawn_info.lives_cost = stats.lives_cost
                end
                path_info.spawns[#path_info.spawns + 1] = spawn_info
            end
        end
        result.paths[#result.paths + 1] = path_info
    end

    return result
end

local function collect_game_state()
    local state = {
        type = "game_state",
        timestamp = socket.gettime(),
    }

    local store = game and game.store
    if not store then
        state.error = "game.store not found"
        return state
    end

    -- 核心状态
    state.gold = store.player_gold
    state.lives = store.lives
    state.wave = store.wave_group_number
    state.wave_total = store.wave_group_total
    state.waves_finished = store.waves_finished
    state.paused = store.paused
    state.tick = store.tick
    state.level_name = store.level_name
    state.level_idx = store.level_idx

    -- 扣命检测：生命值减少时，找最接近出口的敌人作为突破者
    if _last_lives >= 0 and store.lives < _last_lives then
        local lost = _last_lives - store.lives
        -- 找路径进度最大的存活敌人（最接近终点 = 最可能是突破者）
        local breaker = nil
        local max_ni = -1
        if store.entities then
            for _, e in pairs(store.entities) do
                if e.enemy and e.health and not e.health.dead and e.nav_path then
                    if e.nav_path.ni > max_ni then
                        max_ni = e.nav_path.ni
                        breaker = e
                    end
                end
            end
        end
        local event = {
            wave = store.wave_group_number,
            lives_lost = lost,
            lives_remaining = store.lives,
        }
        if breaker then
            event.enemy_template = breaker.template_name
            event.enemy_hp = breaker.health.hp
            event.enemy_armor = breaker.health.armor
            event.enemy_magic_armor = breaker.health.magic_armor
            event.enemy_lives_cost = breaker.enemy.lives_cost
        end
        _life_lost_queue[#_life_lost_queue + 1] = event
    end
    _last_lives = store.lives

    -- 游戏结束检测
    state.game_over = store.game_over or false
    state.level_won = store.level_won or false
    state.level_lost = store.level_lost or false
    if game.game_gui then
        state.gui_mode = game.game_gui.mode
    end

    -- 关卡塔锁定信息（不可升级到的塔模板）
    local level = store.level
    if level then
        local locked = {}
        if level.locked_towers then
            for _, v in pairs(level.locked_towers) do
                locked[#locked + 1] = v
            end
        end
        state.locked_towers = locked
        state.max_upgrade_level = level.max_upgrade_level
    end

    -- 路径入口坐标（每条路径的起点）
    state.path_entries = get_path_entries()

    -- 下一波预览（敌人类型+抗性+路径分配）
    state.next_wave = collect_next_wave()

    -- 当前波是否还在刷怪（next_wave_group_ready 为 nil = 怪还没出完）
    state.wave_spawning = (store.wave_group_number > 0
        and not store.waves_finished
        and store.next_wave_group_ready == nil)

    -- 实体汇总
    state.towers = collect_towers()
    state.holders = collect_holders()
    state.enemies = collect_enemies()
    state.heroes = collect_heroes()

    -- 数量统计
    state.tower_count = #state.towers
    state.holder_count = #state.holders
    state.enemy_count = #state.enemies
    state.hero_count = #state.heroes

    -- 附带扣命事件（消费后清空）
    if #_life_lost_queue > 0 then
        state.life_lost_events = _life_lost_queue
        _life_lost_queue = {}
    end

    return state
end

-- ========== 游戏操作 ==========

-- 塔造价表
local TOWER_COSTS = {
    tower_archer_1 = 70, tower_archer_2 = 110, tower_archer_3 = 160,
    tower_ranger = 230, tower_musketeer = 230,
    tower_barrack_1 = 70, tower_barrack_2 = 110, tower_barrack_3 = 160,
    tower_paladin = 230, tower_barbarian = 230,
    tower_mage_1 = 100, tower_mage_2 = 160, tower_mage_3 = 240,
    tower_arcane_wizard = 300, tower_sorcerer = 300,
    tower_engineer_1 = 125, tower_engineer_2 = 220, tower_engineer_3 = 320,
    tower_tesla = 375, tower_bfg = 400,
    tower_elf = 100, tower_sunray = 500,
}

-- 建塔模板映射 (基础塔类型 → 建造动画模板)
local BUILD_TEMPLATES = {
    archer = "tower_build_archer",
    barrack = "tower_build_barrack",
    mage = "tower_build_mage",
    engineer = "tower_build_engineer",
}

-- 从模板名推断基础塔类型
local function get_tower_base_type(template)
    if template:find("archer") or template:find("ranger") or template:find("musketeer") then
        return "archer"
    elseif template:find("barrack") or template:find("paladin") or template:find("barbarian") then
        return "barrack"
    elseif template:find("mage") or template:find("arcane") or template:find("sorcerer") then
        return "mage"
    elseif template:find("engineer") or template:find("tesla") or template:find("bfg") then
        return "engineer"
    end
    return nil
end

local function action_build_tower(cmd)
    local store = game.store
    local holder_id = cmd.holder_id  -- entity id of the holder
    local tower_type = cmd.tower_type  -- e.g. "archer", "barrack", "mage", "engineer"

    if not holder_id then return {type = "error", message = "missing holder_id"} end
    if not tower_type then return {type = "error", message = "missing tower_type"} end

    local holder = store.entities[holder_id]
    if not holder then return {type = "error", message = "holder not found: " .. holder_id} end
    if not holder.tower_holder then return {type = "error", message = "entity is not a holder"} end
    if holder.tower_holder.blocked then
        return {type = "error", message = "holder is blocked, unblock_price=" .. tostring(holder.tower_holder.unblock_price)}
    end

    local build_template = BUILD_TEMPLATES[tower_type]
    if not build_template then return {type = "error", message = "unknown tower_type: " .. tower_type} end

    -- 对应的实际塔模板 (用于查造价)
    local tower_template = "tower_" .. tower_type .. "_1"
    if tower_type == "engineer" then tower_template = "tower_engineer_1" end
    local cost = TOWER_COSTS[tower_template] or 0

    if store.player_gold < cost then
        return {type = "error", message = "not enough gold: " .. store.player_gold .. " < " .. cost}
    end

    -- 使用游戏原生的 upgrade_to 机制：在 holder 实体上原地升级
    -- 这与用户点击建塔按钮触发的 tw_upgrade action 完全一致：
    -- holder 的 pos、nav mesh 数据全部保留 → tower_build 脚本能正确
    -- 调用 nearest_nodes → 设置 default_rally_pos → 士兵站在路径上
    -- 注意：不手动扣金币！tower_upgrade 系统会根据模板 price 自动扣
    holder.tower.upgrade_to = build_template

    return {
        type = "ok", action = "build_tower",
        tower_type = tower_type, cost = cost, gold = store.player_gold,
        x = holder.pos.x, y = holder.pos.y,
        method = "upgrade_to",
        bridge_version = BRIDGE_VERSION,
    }
end

local function action_sell_tower(cmd)
    local db = get_edb()
    if not db then return {type = "error", message = "entity_db not available"} end

    local store = game.store
    local sim = game.simulation
    local tower_id = cmd.tower_id

    if not tower_id then return {type = "error", message = "missing tower_id"} end

    local tower = store.entities[tower_id]
    if not tower then return {type = "error", message = "tower not found: " .. tower_id} end
    if not tower.tower or tower.tower.type == "holder" then
        return {type = "error", message = "entity is not a tower"}
    end

    -- 计算退款: 开战前(wave==0)全额退, 开战后按 refund_factor
    local refund
    if store.wave_group_number == 0 then
        refund = tower.tower.spent
    else
        refund = math.floor(tower.tower.spent * tower.tower.refund_factor)
    end
    store.player_gold = store.player_gold + refund

    -- 恢复塔位 (根据地形类型选择 holder 模板)
    local holder_template = "tower_holder_grass"
    local terrain = tower.tower.terrain_style
    if terrain == 2 then holder_template = "tower_holder_snow"
    elseif terrain == 3 then holder_template = "tower_holder_wasteland"
    end

    local hx, hy = tower.pos.x, tower.pos.y
    local mesh_id = tower.tower.holder_id

    sim:queue_remove_entity(tower)

    local h = db:create_entity(holder_template)
    h.pos.x = hx
    h.pos.y = hy
    h.ui.nav_mesh_id = mesh_id
    h.tower.holder_id = mesh_id
    sim:queue_insert_entity(h)

    return {type = "ok", action = "sell_tower", refund = refund, gold = store.player_gold}
end

local function action_upgrade_tower(cmd)
    local store = game.store
    local tower_id = cmd.tower_id
    local target = cmd.target  -- e.g. "tower_archer_2", "tower_ranger"

    if not tower_id then return {type = "error", message = "missing tower_id"} end
    if not target then return {type = "error", message = "missing target template"} end

    local tower = store.entities[tower_id]
    if not tower then return {type = "error", message = "tower not found: " .. tower_id} end

    local upgrade_cost = TOWER_COSTS[target]
    if not upgrade_cost then return {type = "error", message = "unknown target: " .. target} end

    if store.player_gold < upgrade_cost then
        return {type = "error", message = "not enough gold: " .. store.player_gold .. " < " .. upgrade_cost}
    end

    if tower.template_name == target then
        return {type = "error", message = "already at " .. target}
    end

    -- 使用原生 upgrade_to 机制，和用户点击升级按钮一致
    -- 注意：不手动扣金币！tower_upgrade 系统会根据模板 price 自动扣
    tower.tower.upgrade_to = target

    return {type = "ok", action = "upgrade_tower", target = target, cost = upgrade_cost, gold = store.player_gold, method = "upgrade_to", bridge_version = BRIDGE_VERSION}
end

local function action_send_wave()
    if game and game.store then
        game.store.send_next_wave = true
        return {type = "ok", action = "send_wave", wave = game.store.wave_group_number}
    end
    return {type = "error", message = "game.store not found"}
end

local function action_move_hero(cmd)
    local store = game and game.store
    if not store or not store.entities then
        return {type = "error", message = "game.store not found"}
    end

    local target_x = cmd.x
    local target_y = cmd.y
    local hero_id = cmd.hero_id  -- optional, if nil move first hero

    if not target_x or not target_y then
        return {type = "error", message = "missing x,y coordinates"}
    end

    -- 找英雄
    local hero = nil
    if hero_id then
        hero = store.entities[hero_id]
    else
        for id, e in pairs(store.entities) do
            if e.hero then
                hero = e
                break
            end
        end
    end

    if not hero then return {type = "error", message = "no hero found"} end
    if not hero.nav_rally then return {type = "error", message = "hero has no nav_rally"} end

    hero.nav_rally.pos.x = target_x
    hero.nav_rally.pos.y = target_y
    hero.nav_rally.new = true

    return {type = "ok", action = "move_hero", hero_id = hero.id, x = target_x, y = target_y}
end

local function action_set_rally_point(cmd)
    -- 通过模拟 GUI 点击设置集结点，等同于用户手动操作：
    -- 选中兵营 → GUI_MODE_RALLY_TOWER → 点击地图
    -- 游戏原生处理：验证范围 → 设rally_pos → change_rally_point → 阵型散开 → 拦截
    local gg = game and game.game_gui
    if not gg then return {type = "error", message = "game_gui not found"} end

    local store = game and game.store
    if not store or not store.entities then
        return {type = "error", message = "game.store not found"}
    end

    local tower_id = cmd.tower_id
    local target_x, target_y = cmd.x, cmd.y

    if not tower_id or not target_x or not target_y then
        return {type = "error", message = "missing tower_id, x, y"}
    end

    local entity = store.entities[tower_id]
    if not entity then
        return {type = "error", message = "tower not found: " .. tower_id}
    end

    if not entity.barrack then
        return {type = "error", message = "entity has no barrack component: " .. tower_id}
    end

    -- 诊断：记录操作前的状态
    local diag = {}
    diag.tower_template = entity.template_name
    diag.tower_pos = {x = entity.pos.x, y = entity.pos.y}
    diag.rally_before = entity.barrack.rally_pos
        and {x = entity.barrack.rally_pos.x, y = entity.barrack.rally_pos.y} or "nil"
    diag.rally_range = entity.barrack.rally_range

    -- 士兵操作前状态
    local soldiers_before = {}
    if entity.barrack.soldiers then
        for k, s in pairs(entity.barrack.soldiers) do
            if type(s) == "table" and s.pos then
                soldiers_before[tostring(k)] = {
                    x = s.pos.x, y = s.pos.y,
                    rally = s.nav_rally and s.nav_rally.pos
                        and {x = s.nav_rally.pos.x, y = s.nav_rally.pos.y} or "nil",
                }
            end
        end
    end
    diag.soldiers_before = soldiers_before

    -- 范围检查（游戏原生 GUI 也会做这个检查）
    local dx = target_x - entity.pos.x
    local dy = target_y - entity.pos.y
    local dist_to_target = math.sqrt(dx * dx + dy * dy)
    diag.dist_to_target = dist_to_target

    local rally_range = entity.barrack.rally_range
    if not rally_range then
        return {
            type = "error",
            message = "rally_range not initialized yet (barrack still building?)",
            diag = diag,
        }
    end
    if dist_to_target > rally_range then
        return {
            type = "error",
            message = "target out of rally range: " .. dist_to_target .. " > " .. rally_range,
            diag = diag,
        }
    end

    -- 使用游戏源码的原生机制设置集结点：
    -- 源码流程: GUI 点击 → 坐标转换 → 设 rally_pos → rally_new=true → change_rally_point 脚本
    -- 我们直接设最后两步的字段，由游戏的 change_rally_point 脚本完成阵型散开和士兵移动
    entity.barrack.rally_pos.x = target_x
    entity.barrack.rally_pos.y = target_y
    entity.barrack.rally_new = true

    -- 诊断：记录操作后的状态
    diag.rally_after = {x = entity.barrack.rally_pos.x, y = entity.barrack.rally_pos.y}
    diag.rally_new_set = entity.barrack.rally_new

    -- 操作后士兵 rally 目标（change_rally_point 是异步的，可能还没执行）
    local soldiers_after = {}
    if entity.barrack.soldiers then
        for k, s in pairs(entity.barrack.soldiers) do
            if type(s) == "table" then
                soldiers_after[tostring(k)] = {
                    pos = s.pos and {x = s.pos.x, y = s.pos.y} or "nil",
                    rally = s.nav_rally and s.nav_rally.pos
                        and {x = s.nav_rally.pos.x, y = s.nav_rally.pos.y} or "nil",
                    rally_new = s.nav_rally and s.nav_rally.new,
                }
            end
        end
    end
    diag.soldiers_after = soldiers_after

    return {
        type = "ok", action = "set_rally_point",
        tower_id = tower_id,
        x = target_x, y = target_y,
        diag = diag,
    }
end

local function action_use_power(cmd)
    local power = cmd.power  -- 1 or 2
    local target_x = cmd.x   -- 游戏世界坐标
    local target_y = cmd.y

    if not power then return {type = "error", message = "missing power (1 or 2)"} end
    if not target_x or not target_y then return {type = "error", message = "missing x,y"} end

    -- 检查按钮状态（使用游戏原生冷却）
    local gg = game and game.game_gui
    if not gg then return {type = "error", message = "game_gui not found"} end
    local btn = (power == 1) and gg.power_1 or gg.power_2
    if not btn then return {type = "error", message = "power button not found"} end
    if btn.mode == "locked" then
        return {type = "error", message = "power " .. power .. " is locked"}
    end
    if btn.mode == "cooldown" then
        return {type = "error", message = "power " .. power .. " is on cooldown"}
    end

    -- 直接创建技能实体（绕过GUI mousepressed，使用游戏坐标精准打击）
    local db = get_edb()
    if not db then return {type = "error", message = "entity_db not available"} end
    local sim = game and game.simulation
    if not sim then return {type = "error", message = "simulation not available"} end

    local template = (power == 1) and "power_fireball_control" or "power_reinforcements_control"
    local e = db:create_entity(template)
    e.pos.x = target_x
    e.pos.y = target_y
    e.user_selection.pos = {x = target_x, y = target_y}
    e.user_selection.allowed = true
    sim:queue_insert_entity(e)

    -- 触发游戏原生冷却（设置按钮状态 + 冷却视图）
    local store = game.store
    if store and store.ts then
        btn.start_ts = store.ts
        if btn.cooldown_view then
            btn.cooldown_view.start_ts = store.ts
            btn.cooldown_view.hidden = false
        end
        btn.mode = "cooldown"
    end

    return {
        type = "ok", action = "use_power", power = power,
        x = target_x, y = target_y,
    }
end

local function action_get_path_points(cmd)
    -- 返回指定范围内的路径点（用于 AI 选择集结点等）
    -- 模拟用户能看到的路径信息：哪些路径在某个圆形范围内经过
    local cx = cmd.x
    local cy = cmd.y
    local radius = cmd.radius or 150

    if not cx or not cy then
        return {type = "error", message = "missing x, y"}
    end

    local radius_sq = radius * radius
    local points = {}
    local seen = {}  -- 去重：避免太密集的点（量化到 10px 网格）

    local ok, pd = pcall(require, "path_db")
    if ok and pd and pd.paths then
        for _, path in ipairs(pd.paths) do
            for _, seg in ipairs(path) do
                for ni = 1, #seg, 3 do  -- 每隔3个采样，与 find_nearest_path_point 一致
                    local n = seg[ni]
                    local dx, dy = n.x - cx, n.y - cy
                    if dx * dx + dy * dy <= radius_sq then
                        -- 量化去重：10px 网格内只保留一个点
                        local key = math.floor(n.x / 10) .. "," .. math.floor(n.y / 10)
                        if not seen[key] then
                            seen[key] = true
                            points[#points + 1] = {
                                x = n.x,
                                y = n.y,
                                path_score = calc_path_score(n.x, n.y),
                            }
                        end
                    end
                end
            end
        end
    end

    return {type = "ok", action = "get_path_points", points = points, count = #points}
end

local function action_dismiss_popups()
    local gg = game and game.game_gui
    if not gg then return {type = "ok", action = "dismiss_popups", dismissed = 0} end

    local dismissed = 0

    -- 循环关闭所有可见的 notiview 弹窗
    for attempt = 1, 10 do
        local nv = gg.notiview
        if not nv or nv.hidden then break end
        local clicked = false
        if nv.children then
            for _, child in ipairs(nv.children) do
                if child.on_click then
                    child:on_click()
                    dismissed = dismissed + 1
                    clicked = true
                    break
                end
            end
        end
        if not clicked then break end
    end

    return {type = "ok", action = "dismiss_popups", dismissed = dismissed}
end

-- ========== 探测/调试命令 ==========

local function inspect_table(obj, max_depth, current_depth)
    max_depth = max_depth or 2
    current_depth = current_depth or 0
    if current_depth >= max_depth then return "[max depth]" end
    if type(obj) ~= "table" then return type(obj) .. ": " .. tostring(obj) end

    local result = {}
    local count = 0
    for k, v in pairs(obj) do
        count = count + 1
        if count > 50 then
            result["..."] = "truncated"
            break
        end
        local key = tostring(k)
        if type(v) == "table" then
            result[key] = inspect_table(v, max_depth, current_depth + 1)
        elseif type(v) == "function" then
            result[key] = "[function]"
        elseif type(v) == "userdata" then
            result[key] = "[userdata]"
        else
            result[key] = tostring(v)
        end
    end
    return result
end

-- ========== 命令处理 ==========

local function handle_command(cmd, client)
    local action = cmd.action or cmd.type or ""

    if action == "ping" then
        send_to(client, {type = "pong", timestamp = socket.gettime()})

    elseif action == "get_state" then
        send_to(client, collect_game_state())

    -- === 游戏操作 ===
    elseif action == "build_tower" then
        send_to(client, action_build_tower(cmd))

    elseif action == "sell_tower" then
        send_to(client, action_sell_tower(cmd))

    elseif action == "upgrade_tower" then
        send_to(client, action_upgrade_tower(cmd))

    elseif action == "send_wave" then
        send_to(client, action_send_wave())

    elseif action == "move_hero" then
        send_to(client, action_move_hero(cmd))

    elseif action == "use_power" then
        send_to(client, action_use_power(cmd))

    elseif action == "set_rally_point" then
        send_to(client, action_set_rally_point(cmd))

    elseif action == "get_path_points" then
        send_to(client, action_get_path_points(cmd))

    elseif action == "dismiss_popups" then
        send_to(client, action_dismiss_popups())

    elseif action == "dump_barrack" then
        -- 诊断命令：转储兵营的 barrack 组件结构
        local store = game and game.store
        local result = {type = "dump_barrack", barracks = {}}
        if store and store.entities then
            for id, e in pairs(store.entities) do
                if e.barrack then
                    local info = {
                        id = id,
                        template = e.template_name or "?",
                        rally_pos = e.barrack.rally_pos and {x = e.barrack.rally_pos.x, y = e.barrack.rally_pos.y} or "nil",
                        rally_range = e.barrack.rally_range,
                        max_soldiers = e.barrack.max_soldiers,
                        soldier_type = e.barrack.soldier_type,
                        soldiers_type = "nil",
                        soldiers_count = 0,
                        soldiers_detail = {},
                    }
                    if e.barrack.soldiers then
                        info.soldiers_type = type(e.barrack.soldiers)
                        local count = 0
                        for k, v in pairs(e.barrack.soldiers) do
                            count = count + 1
                            local detail = {key = tostring(k), val_type = type(v)}
                            if type(v) == "table" then
                                detail.has_id = v.id ~= nil
                                detail.id = v.id
                                detail.has_nav_rally = v.nav_rally ~= nil
                                detail.has_health = v.health ~= nil
                                detail.template = v.template_name
                                if v.pos then detail.pos = {x = v.pos.x, y = v.pos.y} end
                                if v.nav_rally and v.nav_rally.pos then
                                    detail.rally_pos = {x = v.nav_rally.pos.x, y = v.nav_rally.pos.y}
                                end
                            elseif type(v) == "number" then
                                detail.entity_exists = store.entities[v] ~= nil
                            end
                            info.soldiers_detail[#info.soldiers_detail + 1] = detail
                            if count >= 10 then break end
                        end
                        info.soldiers_count = count
                    end
                    result.barracks[#result.barracks + 1] = info
                end
            end
        end
        send_to(client, result)

    elseif action == "dump_enemy" then
        -- 诊断：探查第一个敌人的 nav_path 完整字段
        local store = game and game.store
        local result = {type = "dump_enemy", enemies = {}}
        if store and store.entities then
            local count = 0
            for id, e in pairs(store.entities) do
                if e.enemy and e.health and not e.health.dead and count < 3 then
                    count = count + 1
                    local info = {
                        id = id,
                        template = e.template_name or "?",
                        pos = e.pos and {x = e.pos.x, y = e.pos.y} or "nil",
                    }
                    -- 探查 nav_path 所有字段
                    if e.nav_path then
                        info.nav_path_fields = {}
                        for k, v in pairs(e.nav_path) do
                            local vt = type(v)
                            if vt == "number" or vt == "string" or vt == "boolean" then
                                info.nav_path_fields[tostring(k)] = v
                            elseif vt == "table" then
                                -- 取表的前几个键
                                local keys = {}
                                local c = 0
                                for kk in pairs(v) do
                                    c = c + 1
                                    keys[#keys+1] = tostring(kk)
                                    if c >= 10 then break end
                                end
                                info.nav_path_fields[tostring(k)] = {type = "table", keys = keys, len = #v}
                            else
                                info.nav_path_fields[tostring(k)] = vt
                            end
                        end
                    else
                        info.nav_path_fields = "nil"
                    end
                    result.enemies[#result.enemies + 1] = info
                end
            end
        end
        send_to(client, result)

    elseif action == "dump_waves" then
        -- 探测波次数据结构：找到 store 中与波次/生成相关的所有数据
        local store = game and game.store
        local result = {type = "dump_waves"}
        if store then
            -- 1) 直接搜索 store 中与 wave/spawn 相关的字段
            local wave_fields = {}
            for k, v in pairs(store) do
                local ks = tostring(k):lower()
                if ks:find("wave") or ks:find("spawn") or ks:find("enemy")
                   or ks:find("group") or ks:find("queue") or ks:find("batch") then
                    wave_fields[tostring(k)] = {
                        type = type(v),
                        value = (type(v) ~= "table" and type(v) ~= "function" and type(v) ~= "userdata")
                            and tostring(v) or nil,
                        -- 如果是 table，取前几个键
                        keys = type(v) == "table" and (function()
                            local keys = {}
                            local c = 0
                            for kk in pairs(v) do
                                c = c + 1
                                keys[#keys+1] = tostring(kk)
                                if c >= 20 then break end
                            end
                            return keys
                        end)() or nil,
                    }
                end
            end
            result.wave_fields = wave_fields

            -- 2) 查找 level_data / level_def / waves 等常见结构
            local level_keys = {}
            for _, name in ipairs({"level_data", "level_def", "level_waves", "waves",
                                   "wave_groups", "wave_data", "spawns", "spawn_queue",
                                   "enemy_waves", "wave_list"}) do
                if store[name] ~= nil then
                    level_keys[name] = type(store[name])
                end
            end
            result.level_keys = level_keys

            -- 3) 尝试遍历 store.entities 找 wave_spawn 类型的实体
            local spawn_entities = {}
            if store.entities then
                local count = 0
                for id, e in pairs(store.entities) do
                    -- 查找含有 spawn/wave 组件的实体
                    if e.wave_spawn or e.spawner or e.spawn then
                        count = count + 1
                        local info = {id = id, template = e.template_name}
                        -- 遍历组件
                        local comps = {}
                        for ck, cv in pairs(e) do
                            comps[tostring(ck)] = type(cv)
                        end
                        info.components = comps
                        -- 如果有 wave_spawn，深入看
                        if e.wave_spawn then
                            info.wave_spawn = inspect_table(e.wave_spawn, 3)
                        end
                        if e.spawner then
                            info.spawner = inspect_table(e.spawner, 3)
                        end
                        if e.spawn then
                            info.spawn = inspect_table(e.spawn, 3)
                        end
                        spawn_entities[#spawn_entities+1] = info
                        if count >= 10 then break end
                    end
                end
            end
            result.spawn_entities = spawn_entities

            -- 4) 检查 _G 中是否有 enemy 模板数据库
            local template_sources = {}
            for _, name in ipairs({"enemy_db", "enemy_templates", "unit_db", "template_db",
                                   "entity_templates", "all_templates"}) do
                if _G[name] ~= nil then
                    template_sources[name] = type(_G[name])
                end
            end
            -- 也检查 entity_db
            local edb_local = get_edb()
            if edb_local then
                template_sources["entity_db"] = "available"
                -- 尝试列出几个 enemy 模板的字段
                local sample_enemies = {"enemy_goblin", "enemy_orc", "enemy_bandit",
                                        "enemy_brigand", "enemy_wolf", "enemy_troll",
                                        "enemy_dark_knight", "enemy_marauder"}
                local enemy_samples = {}
                for _, tname in ipairs(sample_enemies) do
                    local ok, tmpl = pcall(function()
                        return edb_local.templates and edb_local.templates[tname]
                    end)
                    if ok and tmpl then
                        local sample = {exists = true}
                        if type(tmpl) == "table" then
                            -- 提取关键战斗属性
                            if tmpl.health then
                                sample.hp = tmpl.health.hp
                                sample.hp_max = tmpl.health.hp_max
                                sample.armor = tmpl.health.armor
                                sample.magic_armor = tmpl.health.magic_armor
                            end
                            if tmpl.enemy then
                                sample.gold = tmpl.enemy.gold
                                sample.lives_cost = tmpl.enemy.lives_cost
                            end
                            if tmpl.motion then
                                sample.speed = tmpl.motion.max_speed
                            end
                            -- 列出所有组件名
                            local comp_names = {}
                            for ck in pairs(tmpl) do
                                comp_names[#comp_names+1] = tostring(ck)
                            end
                            sample.components = comp_names
                        end
                        enemy_samples[tname] = sample
                    end
                end
                result.enemy_samples = enemy_samples
            end
            result.template_sources = template_sources
        end
        send_to(client, result)

    -- === 调试命令 ===
    elseif action == "inspect_globals" then
        local globals = {}
        local count = 0
        for k, v in pairs(_G) do
            count = count + 1
            if count > 200 then break end
            globals[k] = type(v)
        end
        send_to(client, {type = "globals", data = globals})

    elseif action == "inspect" then
        local path = cmd.path or ""
        local depth = cmd.depth or 2
        local obj = _G
        for part in path:gmatch("[^%.]+") do
            if type(obj) == "table" then
                obj = obj[part]
            else
                obj = nil
                break
            end
        end
        if obj ~= nil then
            send_to(client, {
                type = "inspect_result",
                path = path,
                value_type = type(obj),
                data = inspect_table(obj, depth)
            })
        else
            send_to(client, {type = "inspect_result", path = path, error = "not found"})
        end

    elseif action == "find_game" then
        local found = {}
        local candidates = {"game", "director", "simulation", "store"}
        for _, name in ipairs(candidates) do
            local val = _G[name]
            if val ~= nil then found[name] = type(val) end
        end
        send_to(client, {type = "game_objects", data = found})

    elseif action == "eval" then
        local code = cmd.code or ""
        local f, err = loadstring(code)
        if f then
            local ok, result = pcall(f)
            if ok then
                send_to(client, {type = "eval_result", result = tostring(result)})
            else
                send_to(client, {type = "eval_error", error = tostring(result)})
            end
        else
            send_to(client, {type = "eval_error", error = tostring(err)})
        end

    elseif action == "detect_screen" then
        -- 界面检测：通过 game.store 和 main.handler.last_item_name 判断
        local result = {type = "screen_info"}

        if game and game.store then
            result.screen = "in_level"
            result.level_name = game.store.level_name
            result.level_idx = game.store.level_idx
            result.wave = game.store.wave_group_number
            result.wave_total = game.store.wave_group_total
            result.level_won = game.store.level_won or false
            result.level_lost = game.store.level_lost or false
        elseif main and main.handler then
            local item_name = main.handler.last_item_name
            result.item_name = item_name
            if item_name == "map" then
                result.screen = "map"
            elseif item_name == "slots" then
                result.screen = "main_menu"
            else
                result.screen = "unknown_" .. tostring(item_name)
            end
        else
            result.screen = "unknown"
        end

        send_to(client, result)

    elseif action == "go_to_map" then
        -- 关卡结束后返回地图
        if game and game.game_gui and game.game_gui.go_to_map then
            local ok, err = pcall(function()
                game.game_gui:go_to_map()
            end)
            if ok then
                send_to(client, {type = "ok", message = "returning to map"})
            else
                send_to(client, {type = "error", message = "go_to_map failed: " .. tostring(err)})
            end
        else
            send_to(client, {type = "error", message = "not in level (no game_gui)"})
        end

    elseif action == "restart_level" then
        -- 关卡内重试
        if game and game.game_gui and game.game_gui.restart_game then
            local ok, err = pcall(function()
                game.game_gui:restart_game()
            end)
            if ok then
                _last_lives = -1
                _life_lost_queue = {}
                send_to(client, {type = "ok", message = "restarting level"})
            else
                send_to(client, {type = "error", message = "restart failed: " .. tostring(err)})
            end
        else
            send_to(client, {type = "error", message = "not in level (no game_gui)"})
        end

    elseif action == "load_slot" then
        -- 在主菜单选择存档槽进入地图
        -- cmd.slot: 存档槽索引 (1/2/3), 默认 1
        local slot = cmd.slot or 1
        local ai = main and main.handler and main.handler.active_item
        if not ai then
            send_to(client, {type = "error", message = "no active_item"})
        elseif not ai.handle_slot_button then
            send_to(client, {type = "error", message = "not on main menu (no handle_slot_button)"})
        else
            local ok, err = pcall(function()
                ai:handle_slot_button(slot)
            end)
            if ok then
                send_to(client, {type = "ok", message = "loaded slot " .. slot})
            else
                send_to(client, {type = "error", message = "load_slot failed: " .. tostring(err)})
            end
        end

    elseif action == "start_level" then
        -- 在地图界面启动关卡
        -- cmd.level_idx: 关卡索引 (1-based)
        -- cmd.level_mode: 模式 (1=normal, 2=iron, 3=heroic), 默认 1
        local idx = cmd.level_idx
        local mode = cmd.level_mode or 1
        if not idx then
            send_to(client, {type = "error", message = "missing level_idx"})
        elseif not screen_map or not screen_map.start_level then
            send_to(client, {type = "error", message = "screen_map.start_level not found"})
        else
            local ok, err = pcall(function()
                screen_map:start_level(idx, mode)
            end)
            if ok then
                _last_lives = -1
                _life_lost_queue = {}
                send_to(client, {type = "ok", message = "starting level " .. idx .. " mode " .. mode})
            else
                send_to(client, {type = "error", message = "start_level failed: " .. tostring(err)})
            end
        end

    elseif action == "get_level_list" then
        -- 获取关卡列表和星级信息
        local result = {type = "level_list", levels = {}}
        -- 优先用 active_item.user_data，回退到 screen_map
        local map_item = main and main.handler and main.handler.active_item
        local ud = (map_item and map_item.user_data) or (screen_map and screen_map.user_data)
        local ld = (map_item and map_item.level_data) or (screen_map and screen_map.level_data)
        if ud and ud.levels then
            for i = 1, 100 do
                local lv = ud.levels[i]
                if not lv then break end
                result.levels[#result.levels + 1] = {
                    idx = i,
                    stars = lv.stars or 0,
                    iron_stars = lv.iron_stars or lv.heroic_stars or 0,
                    hero_stars = lv.hero_stars or 0,
                }
            end
        end
        -- 获取关卡名称
        if ld then
            for i, lv in ipairs(result.levels) do
                local d = ld[i]
                if d then
                    lv.name = d.name or d.level_name or nil
                end
            end
        end
        result.count = #result.levels
        send_to(client, result)

    elseif action == "set_push_interval" then
        push_interval = cmd.interval or 0.5
        send_to(client, {type = "ok", message = "push interval set to " .. push_interval})

    else
        send_to(client, {type = "error", message = "unknown action: " .. action})
    end
end

-- ========== 主更新循环 ==========

-- 每帧追踪士兵归属
-- (track_soldiers 已移除：改用游戏原生 barrack.soldiers 绑定)

function M.update(dt)
    if not initialized then return end

    -- (原 track_soldiers 已移除，使用游戏原生 barrack.soldiers)

    -- 接受新连接
    local client, err = server:accept()
    if client then
        client:settimeout(0)
        table.insert(clients, client)
        print("[BridgeMod] Client connected! Total: " .. #clients)
        send_to(client, {
            type = "connected",
            game = "Kingdom Rush",
            version = BRIDGE_VERSION,
            message = "BridgeMod ready",
            commands = {"ping","get_state","build_tower","sell_tower","upgrade_tower","send_wave","move_hero","use_power","set_rally_point","get_path_points","eval","inspect","inspect_globals","find_game","set_push_interval","detect_screen","go_to_map","restart_level","load_slot","start_level","get_level_list"}
        })
    end

    -- 处理客户端消息
    for i = #clients, 1, -1 do
        local c = clients[i]
        local data, err = c:receive("*l")
        if data then
            local ok, cmd = pcall(json.decode, data)
            if ok and cmd then
                handle_command(cmd, c)
            else
                send_to(c, {type = "error", message = "invalid JSON"})
            end
        elseif err == "closed" then
            print("[BridgeMod] Client disconnected. Total: " .. (#clients - 1))
            table.remove(clients, i)
        end
    end

    -- 定期推送游戏状态（仅在客户端请求时开启）
    if push_interval > 0 and #clients > 0 then
        push_timer = push_timer + dt
        if push_timer >= push_interval then
            push_timer = 0
            broadcast(collect_game_state())
        end
    end
end

return M
