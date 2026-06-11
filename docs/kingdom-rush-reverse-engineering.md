# Kingdom Rush AI Bot 逆向工程笔记

## 引擎信息

- **引擎**: LÖVE (love2d), LuaJIT 字节码
- **可用版本**: KR1, KR2 (Frontiers), KR3 (Origins) — 都是 LÖVE
- **不可用**: KR4 (Vengeance) — Cocos2d-x
- **解包方式**: Python `zipfile.ZipFile("Kingdom Rush.exe")` 直接读取
- **注入方式**: 修改 exe 内嵌 ZIP，替换 main.lua + 注入 bridge_server.lua
- **TCP 端口**: 9878
- **游戏自带**: luasocket (love.dll内嵌), lib/json.lua

## 注入架构

```
patch_kingdom_rush.py → 修改 exe 内嵌 ZIP:
  main.lua (原始) → _kr_main_orig.lua (重命名保留)
  main.lua (新wrapper) → 加载原始 + hook love.update
  bridge_server.lua → TCP Server 端口 9878

kingdom_rush_bot.py → Python TCP 客户端（交互探测/观察/AI）
kingdom_rush_bridge.lua → Lua TCP 桥接代码源文件
```

## 游戏核心全局对象

| 全局变量 | 类型 | 说明 |
|----------|------|------|
| `game` | table | 主游戏对象 |
| `game.store` | table | **核心状态容器** |
| `game.simulation` | table | ECS 实体管理 (insert/remove/update) |
| `game.game_gui` | table | GUI 管理 |
| `director` | table | 游戏场景/状态管理 |
| `simulation` | table | 全局引用 (同 game.simulation) |

## game.store 状态字段 (关卡内)

| 字段 | 示例值 | 说明 |
|------|--------|------|
| `player_gold` | 31 | 当前金币 |
| `lives` | 20 | 剩余生命 |
| `wave_group_number` | 7 | 当前波次 |
| `wave_group_total` | 7 | 总波次 |
| `waves_finished` | true | 所有波次是否已完成 |
| `force_next_wave` | false | 强制下一波 |
| `send_next_wave` | false | 发送下一波 |
| `early_wave_reward` | 1 | 提前出波奖励 |
| `entity_count` | 34 | 场上实体数 |
| `entity_max` | 58 | 历史最大实体 ID |
| `level_idx` | 1 | 关卡索引 |
| `level_name` | "level01" | 关卡名 |
| `level_mode` | 1 | 模式 (1=campaign) |
| `level_difficulty` | 2 | 难度 |
| `paused` | false | 是否暂停 |
| `tick` | 13088 | 游戏 tick |
| `tick_ts` | 218.13 | tick 时间戳 |
| `gems_collected` | 71 | 宝石收集 |
| `entities` | table | **所有实体的 table** |

## game.simulation_systems (系统列表)

1=level, 2=wave_spawn, 3=mod_lifecycle, 4=main_script, 5=timed,
6=tween, 7=health, 8=count_groups, 9=hero_xp_tracking, 10=pops,
11=goal_line, 12=tower_upgrade, 13=game_upgrades, 14=texts,
15=particle_system, 16=render, 17=sound_events, 18=seen_tracker

## 实体结构 (game.store.entities)

### 实体类型

| template_name | 类型 | 说明 |
|--------------|------|------|
| `tower_holder_grass` | 塔位 | 可建塔的位置 |
| `tower_archer_1/2` | 弓箭塔 | 等级 1/2 |
| `tower_engineer_1` | 工程塔 | 等级 1 |
| `tower_barrack_1/2` | 兵营 | 等级 1/2 |
| `tower_mage_1` | 法师塔 | 等级 1 |
| `soldier_militia` | 士兵 | 兵营产出 |
| `enemy_fat_orc` | 胖兽人 | 敌人 |
| `enemy_wolf_small` | 小狼 | 敌人 |
| `enemy_goblin` | 哥布林 | 敌人 |
| `decal_*` | 装饰 | 背景/特效 |
| `arrow_1` | 箭矢 | 投射物 |

### 塔实体字段 (以 tower_engineer_1 id=25 为例)

```
id = 25
template_name = "tower_engineer_1"

tower.type = "engineer"       -- 塔类型
tower.level = 1               -- 等级
tower.price = 125             -- 造价
tower.spent = 125             -- 已投入金币
tower.refund_factor = 0.6     -- 卖出退款比例
tower.holder_id = 1           -- 所在塔位的 nav_mesh_id
tower.size = 1                -- 塔大小
tower.terrain_style = 1       -- 地形类型
tower.can_be_sold = true
tower.can_be_mod = true
tower.can_do_magic = true
tower.can_hover = true
tower.damage_factor = 1
tower.block_count = 0

pos.x = 624                  -- 屏幕坐标 X
pos.y = 569                  -- 屏幕坐标 Y

attacks.range = 160           -- 攻击范围

ui.nav_mesh_id = 1
ui.can_select = true
ui.can_click = true
ui.clicked = true
ui.has_nav_mesh = true
ui.z = 0

info.enc_icon = 4
info.portrait = "info_portraits_towers_0003"

sound_events.sell = "GUITowerSell"
sound_events.insert = "EngineerTaunt"
```

### 敌人实体字段 (以 enemy_goblin id=2248 为例)

```
id = 2248
template_name = "enemy_goblin"

health.hp = 5                 -- 当前血量
health.hp_max = 20            -- 最大血量
health.dead = false           -- 是否死亡
health.armor = 0              -- 物理护甲
health.magic_armor = 0        -- 魔法护甲
health.poison_armor = 0
health.spiked_armor = 0
health.immune_to = 0
health.ignore_damage = false
health.damage_factor = 1
health.dead_lifetime = 2
health.death_ts = 0
health.last_damage_types = 0

enemy.gold = 3                -- 击杀金币
enemy.gold_bag = 1
enemy.lives_cost = 1          -- 通过终点扣生命
enemy.gems = 0
enemy.can_do_magic = true
enemy.can_accept_magic = true
enemy.valid_terrains = 1
enemy.remove_at_goal_line = true

pos.x = 652.6                -- 屏幕坐标 X
pos.y = 663.5                -- 屏幕坐标 Y

motion.arrived = false        -- 是否到达终点
motion.max_speed = 46.08      -- 最大速度

nav_path.pi = 1               -- 路径索引
nav_path.spi = 1              -- 子路径索引
nav_path.ni = 94              -- 节点索引 (路径进度, 越大越接近终点)
nav_path.dir = 1              -- 方向

heading.angle = 2.83          -- 朝向角度

unit.level = 0
unit.size = 1
unit.blood_color = "red"
unit.can_disintegrate = true
unit.can_explode = true
unit.damage_factor = 1
unit.stun_count = 0

vis.flags = 2048              -- 可见性标志
vis.bans = 0

info.i18n_key = "ENEMY_GOBLIN"
info.enc_icon = 1
```

### 塔位 (tower_holder) 字段 (以 tower_holder_grass id=15 为例)

```
id = 15
template_name = "tower_holder_grass"

tower_holder.blocked = false   -- 是否被阻挡 (需花钱解锁)
tower_holder.unblock_price = 0 -- 解锁价格

pos.x = 564                   -- 塔位屏幕坐标 X
pos.y = 138                   -- 塔位屏幕坐标 Y

tower.type = "holder"          -- 类型为 holder (空塔位)
tower.level = 1
tower.price = 0
tower.spent = 0
tower.holder_id = 5            -- 自身的 holder_id (= ui.nav_mesh_id)
tower.can_be_sold = true
tower.can_be_mod = false
tower.refund_factor = 0.6
tower.size = 1
tower.terrain_style = 1

ui.nav_mesh_id = 5             -- 网格 ID, 建塔时用来关联
ui.can_select = true
ui.can_click = true
ui.has_nav_mesh = true
```

**塔位 → 塔的关联**: 建塔后, 塔的 `tower.holder_id` = 塔位的 `ui.nav_mesh_id`

## 操作接口 (从 tower_menus_data.lua 逆向)

游戏操作通过菜单 action 系统驱动。每个菜单按钮有:
- `action` — 操作类型 (tw_upgrade, tw_sell, tw_rally, tw_buy_soldier, upgrade_power, tw_unblock)
- `action_arg` — 操作参数 (模板名)

### 建塔 (从空塔位 holder 菜单)

| action | action_arg | 说明 |
|--------|-----------|------|
| `tw_upgrade` | `tower_build_archer` | 建弓箭塔 |
| `tw_upgrade` | `tower_build_barrack` | 建兵营 |
| `tw_upgrade` | `tower_build_mage` | 建法师塔 |
| `tw_upgrade` | `tower_build_engineer` | 建工程塔 |

### 升级塔 (从已建塔菜单)

| action | action_arg | 说明 |
|--------|-----------|------|
| `tw_upgrade` | `tower_archer_2` | 弓箭塔 1→2 |
| `tw_upgrade` | `tower_archer_3` | 弓箭塔 2→3 |
| `tw_upgrade` | `tower_ranger` | 弓箭塔 3→游侠 (特化) |
| `tw_upgrade` | `tower_musketeer` | 弓箭塔 3→火枪手 (特化) |
| `tw_upgrade` | `tower_barrack_2` | 兵营 1→2 |
| `tw_upgrade` | `tower_barrack_3` | 兵营 2→3 |
| `tw_upgrade` | `tower_paladin` | 兵营 3→圣骑士 (特化) |
| `tw_upgrade` | `tower_barbarian` | 兵营 3→蛮族 (特化) |
| `tw_upgrade` | `tower_mage_2` | 法师塔 1→2 |
| `tw_upgrade` | `tower_mage_3` | 法师塔 2→3 |
| `tw_upgrade` | `tower_arcane_wizard` | 法师塔 3→奥术 (特化) |
| `tw_upgrade` | `tower_sorcerer` | 法师塔 3→巫师 (特化) |
| `tw_upgrade` | `tower_engineer_2` | 工程塔 1→2 |
| `tw_upgrade` | `tower_engineer_3` | 工程塔 2→3 |
| `tw_upgrade` | `tower_tesla` | 工程塔 3→特斯拉 (特化) |
| `tw_upgrade` | `tower_bfg` | 工程塔 3→BFG (特化) |

### 卖塔

| action | 说明 |
|--------|------|
| `tw_sell` | 卖塔 (退 tower.spent * tower.refund_factor 金币) |

### 其他操作

| action | action_arg | 说明 |
|--------|-----------|------|
| `tw_rally` | — | 设置集结点 (兵营/特殊兵) |
| `tw_buy_soldier` | `soldier_elf` / `soldier_sasquash` | 购买特殊兵 |
| `tw_unblock` | `tower_holder` | 解锁被阻挡的塔位 |
| `upgrade_power` | `poison`/`thorn`/`sniper`/... | 特化塔技能升级 |
| `send_next_wave` | — | 提前出波 (game_gui 函数) |

### 操作执行方式 (已验证 ✓)

#### 建塔/升级 — 原生 upgrade_to 机制 (v4.0+ ✓)

**重要发现**: 游戏的 `tw_upgrade` action 不是创建新实体，而是**原地升级**现有实体。
通过设置 `tower.upgrade_to` 属性，`tower_upgrade` 系统（simulation system #12）会自动：
- 执行模板转换（保留 pos、nav mesh 等所有空间数据）
- 扣除金币（从模板 price 字段）
- 触发 `tower_build` 脚本 → `nearest_nodes` → 设置 `default_rally_pos`
- 播放建造动画

```lua
-- 建塔：在 holder 上原地升级为建造动画实体
local holder = store.entities[holder_id]
holder.tower.upgrade_to = "tower_build_barrack"
-- 不需要手动扣金！tower_upgrade 系统自动扣

-- 升级塔：在现有塔上原地升级
local tower = store.entities[tower_id]
tower.tower.upgrade_to = "tower_archer_2"
-- 同样不需要手动扣金
```

**踩坑记录**: 之前用 `db:create_entity()` + 手动拷贝字段 → 新实体没有 nav mesh 注册 → `nearest_nodes` 失败 → 兵营 `default_rally_pos` 为 (0,0) → 士兵全跑到左下角原点。改用 `upgrade_to` 后一切正常。

#### 旧方式（已弃用，仅卖塔还在用 create_entity）

```lua
local edb = require("entity_db")
-- create_entity 是方法调用 (edb:create_entity，不是 edb.create_entity)
local e = edb:create_entity("tower_build_archer")
e.pos.x = hx  -- 注意：设个别字段，不要替换整个 pos 组件
e.pos.y = hy
e.tower.holder_id = mesh_id
sim:queue_remove_entity(holder)
sim:queue_insert_entity(e)
```

**注意**: 此方式创建的实体缺少 nav mesh 注册，不适合建塔/升级。仅用于卖塔时恢复 holder。

#### 卖塔流程 (已验证 ✓)

```lua
local tower = store.entities[tower_id]
local refund = math.floor(tower.tower.spent * tower.tower.refund_factor)
store.player_gold = store.player_gold + refund

-- 恢复塔位
local h = edb:create_entity("tower_holder_grass")
h.pos = {x = tower.pos.x, y = tower.pos.y}
h.ui.nav_mesh_id = tower.tower.holder_id
h.tower.holder_id = tower.tower.holder_id

sim:queue_remove_entity(tower)
sim:queue_insert_entity(h)
```

**注意**: 开战前卖塔应全额退款，开战后按 `refund_factor` (0.6) 折算

#### 提前出波 (已验证 ✓)

```lua
game.store.send_next_wave = true
```

### entity_db 模块

通过 `require("entity_db")` 获取，包含：

| 方法/字段 | 类型 | 说明 |
|-----------|------|------|
| `create_entity(self, template_name)` | function | 创建实体 (方法调用!) |
| `clone_entity` | function | 克隆实体 |
| `get_template` | function | 获取模板 |
| `set_template` | function | 设置模板 |
| `add_comps` | function | 添加组件 |
| `filter` | function | 过滤实体 |
| `search_entity` | function | 搜索实体 |
| `filter_templates` | function | 过滤模板 |
| `get_component` | function | 获取组件 |
| `entities` | table | 所有模板定义 |
| `last_id` | number | 最后分配的实体 ID |

### game.simulation 方法

| 方法 | 说明 |
|------|------|
| `queue_insert_entity(self, entity)` | 队列插入实体 |
| `queue_remove_entity(self, entity)` | 队列移除实体 |
| `insert_entity(self, entity)` | 立即插入实体 |
| `remove_entity(self, entity)` | 立即移除实体 |
| `do_tick` | 执行一个 tick |
| `init` | 初始化 |
| `update` | 更新 |

## 塔造价表

**注意**: price 是该步骤的升级费，不是累计总价。建塔/升级直接扣 price 金额。

| 模板名 | 升级费 | 说明 |
|--------|--------|------|
| `tower_archer_1` | 70 | 弓箭塔 Lv1 (建造) |
| `tower_archer_2` | 110 | 弓箭塔 Lv2 (升级费) |
| `tower_archer_3` | 160 | 弓箭塔 Lv3 |
| `tower_ranger` | 230 | 游侠 (弓箭特化A) |
| `tower_musketeer` | 230 | 火枪手 (弓箭特化B) |
| `tower_barrack_1` | 70 | 兵营 Lv1 |
| `tower_barrack_2` | 110 | 兵营 Lv2 |
| `tower_barrack_3` | 160 | 兵营 Lv3 |
| `tower_paladin` | 230 | 圣骑士 (兵营特化A) |
| `tower_barbarian` | 230 | 蛮族 (兵营特化B) |
| `tower_mage_1` | 100 | 法师塔 Lv1 |
| `tower_mage_2` | 160 | 法师塔 Lv2 |
| `tower_mage_3` | 240 | 法师塔 Lv3 |
| `tower_arcane_wizard` | 300 | 奥术法师 (法师特化A) |
| `tower_sorcerer` | 300 | 巫师 (法师特化B) |
| `tower_engineer_1` | 125 | 工程塔 Lv1 |
| `tower_engineer_2` | 220 | 工程塔 Lv2 |
| `tower_engineer_3` | 320 | 工程塔 Lv3 |
| `tower_tesla` | 375 | 特斯拉 (工程特化A) |
| `tower_bfg` | 400 | BFG (工程特化B) |
| `tower_elf` | 100 | 精灵塔 (特殊) |
| `tower_sunray` | 500 | 阳光塔 (特殊) |

## 英雄模板列表

hero_alleria, hero_bolin, hero_denas, hero_elora, hero_gerald,
hero_hacksaw, hero_ignus, hero_ingvar, hero_magnus, hero_malik,
hero_oni, hero_thor

## 英雄控制

### 英雄组件 (以 hero_gerald 为例)

```
hero.level = 1               -- 英雄等级
hero.xp = 0                  -- 经验值
hero.skills = table           -- 技能表
hero.fixed_stat_attack = 6
hero.fixed_stat_health = 8
hero.fixed_stat_speed = 5

motion.max_speed = 66         -- 移动速度
nav_rally.pos = {x, y}        -- 集结点位置
nav_rally.new = false          -- 设为 true 触发移动
nav_rally.requires_node_nearby = true
```

### 移动英雄

```lua
-- 找到英雄实体
local hero_entity = nil
for id, e in pairs(game.store.entities) do
    if e.hero then hero_entity = e; break end
end

-- 移动到目标位置
if hero_entity then
    hero_entity.nav_rally.pos.x = target_x
    hero_entity.nav_rally.pos.y = target_y
    hero_entity.nav_rally.new = true
end
```

### game_gui 英雄方法

| 方法 | 说明 |
|------|------|
| `game_gui.add_hero(self, ...)` | 添加英雄到关卡 |
| `game_gui.select_hero(self, ...)` | 选中英雄 |
| `game_gui.deselect_heroes(self, ...)` | 取消选中 |
| `game_gui.list_heroes(self, ...)` | 列出英雄 |
| `game_gui.heroes` | 当前关卡英雄表 |

## 技能 (用户法术)

### 火雨 (Power 1)

- **GUI 按钮**: `game_gui.power_1`
- **模板**: `user_power_1`（含 `fireball_count`, `cataclysm_count`, `max_spread`）
- **冷却**: 80 秒
- **GUI 模式**: `POWER_1`（选中后点击地图释放）
- **关联模板**: `power_fireball`, `power_fireball_control`, `power_scorched_earth`

### 增援 (Power 2)

- **GUI 按钮**: `game_gui.power_2`
- **模板**: `user_power_2`
- **冷却**: 10 秒
- **GUI 模式**: `POWER_2`
- **关联模板**: `power_reinforcements_control`, `soldier_militia`

### 技能释放方式

#### 方法1：GUI模拟（mousepressed）— 有坐标系问题 ⚠️

```lua
game.game_gui:set_mode("POWER_1")
game.game_gui:mousepressed(x, y, 1)  -- x,y 必须是 UI/屏幕坐标，不是游戏坐标！
```

**核心问题：mousepressed 期望 UI 坐标，不是游戏坐标。**
与集结点的问题完全一致（见下方"集结点设置方式"：mousepressed 模拟失败）。

- `e.pos.x/y` = 游戏世界坐标（y向上递增，范围约1600×1000）
- `mousepressed(x,y)` = UI/屏幕坐标（y向下递增，范围约1228×768）
- `g2u()` 转换：`x_ui ≈ x_game + 102.4`, `y_ui ≈ 768 - y_game`
  - 验证数据：g2u(400,300)=(502.4,468), g2u(237,434)=(340,333)
- `u2g()` 反向：`x_game ≈ x_ui - 102.4`, `y_game ≈ 768 - y_ui`

**踩坑记录：**
1. 直接传游戏坐标给mousepressed → 技能不触发（btn.mode始终=unlocked，无cooldown）
2. 用g2u转换后传 → 技能能触发但落点偏移（"打得太靠前"）
3. 去掉g2u直接传游戏坐标 → 完全不触发（诊断确认btn_mode_after=unlocked）
4. **结论：mousepressed方案不可靠，应改用直接实体创建（方法2）**

#### 方法2：直接创建实体（绕过GUI）— 进行中 🔄

与英雄移动、集结点相同的思路：绕过GUI，直接操作游戏内部对象。

**火雨实体创建（已验证可触发 ✓）：**

```lua
local db = require("entity_db")
local sim = game.simulation
local e = db:create_entity("power_fireball_control")
e.pos.x = target_x  -- 游戏坐标
e.pos.y = target_y
e.user_selection.allowed = true
sim:queue_insert_entity(e)
```

**实体字段：**
```
power_fireball_control:
  pos             — {x, y}，默认(0,0)
  fireball_count  — 5（火球数量）
  cataclysm_count — 0
  max_spread      — 20（扩散半径）
  cooldown        — 80
  user_selection  — {allowed=bool, can_select_point_fn=function}
  user_power      — {level=2}
  main_script     — {runs=1, update=function}（执行火雨逻辑的脚本）
  template_name   — "power_fireball_control"
  id              — number
```

**已解决：精准打击 ✓（2026-03-15）**

关键发现：落点字段是 `user_selection.pos`（不是 `e.pos`，不是 `target_pos`）

```lua
local db = require("entity_db")
local sim = game.simulation
local e = db:create_entity("power_fireball_control")
e.pos.x = target_x              -- 游戏坐标
e.pos.y = target_y
e.user_selection.pos = {x = target_x, y = target_y}  -- ★ 这才是落点
e.user_selection.allowed = true
sim:queue_insert_entity(e)
```

- 三次测试均精准命中敌人头顶 ✓
- 无需坐标转换，直接使用 `e.pos.x/y` 游戏坐标 ✓
- 可绕过路径限制，任意位置都能释放 ✓
- `main_script.update` 定义在 `all/scripts.lua` 第6131-6180行

**踩坑过程：**
- `e.pos` 单独设置 → 落点固定不变（pos不是落点字段）
- `target_pos` 字段 → 实体上不存在，设了也无效
- `user_selection.pos` → ★ 正确！控制实际落点

**增援同样适用 ✓（2026-03-15）**

```lua
local e = db:create_entity("power_reinforcements_control")
e.pos.x = target_x
e.pos.y = target_y
e.user_selection.pos = {x = target_x, y = target_y}
e.user_selection.allowed = true
sim:queue_insert_entity(e)
```

- 增援 cooldown=99（模板默认值，实际游戏内10秒）
- 四次测试均精准落在敌人附近 ✓

**待解决：冷却管理**
- 绕过GUI不会自动触发power按钮冷却
- 需要bridge侧记录上次释放时间，按cooldown时间拒绝重复释放
- 火雨cooldown=80秒，增援cooldown=10秒

**冷却检查**: `game.game_gui.power_1.mode` — `unlocked`/`default`=可用, `cooldown`=冷却中, `locked`=未解锁
**注意**: 游戏必须在运行状态（非暂停）才能释放技能

## 兵营与士兵控制 (源码逆向 ✓)

### 核心发现：游戏原生绑定机制

通过提取 `Kingdom Rush.exe` 中的 LuaJIT 字节码字符串常量，还原了兵营-士兵的原生绑定机制。

**关键组件字段（从 `all/components.lua` 字节码提取）：**

```
barrack 组件 (兵营实体上):
  soldiers        — table, 值是士兵实体引用 (不是ID)，key 为 "1","2","3"
  max_soldiers    — number, 最大士兵数 (一般 3)
  soldier_type    — string, 如 "soldier_militia"
  rally_pos       — {x, y}, 当前集结点位置
  rally_range     — number, 集结范围 (如 145)
  rally_new       — bool, 设为 true 触发 change_rally_point 脚本
  rally_anywhere  — bool
  rally_terrains  — 允许集结的地形
  rally_angle_offset — 阵型角度偏移
  rally_radius    — 阵型半径
  default_rally_pos — 默认集结点

soldier 组件 (士兵实体上):
  tower_holder    — 所属塔位
  holder_id       — 塔位 ID
  spawner_id      — 生成者 ID (指向兵营)
```

**游戏原生集结流程（从 `all/scripts.lua`、`all/script_utils.lua`、`all-desktop/game_gui.lua` 还原）：**

1. GUI 触发 `tw_rally` → 进入 `GUI_MODE_RALLY_TOWER`
2. 玩家点击地图 → `u2g` 坐标转换 → 设置 `barrack.rally_pos`
3. 触发 `change_rally_point` 脚本（通过 queue 系统）
4. `change_rally_point` 遍历 `barrack.soldiers`（pairs），对每个士兵：
   - 调用 `rally_formation_position` 计算阵型偏移
   - 设置 `soldier.nav_rally.pos` 和 `soldier.nav_rally.new = true`
5. 士兵的 `y_soldier_new_rally` 协程响应移动

**集结点设置方式（直接设字段 + 触发原生脚本）：**

GUI 模拟方式（set_mode + mousepressed）尝试失败——`tw_rally` action 会设置额外内部状态，
直接 set_mode 后 mousepressed 无法触发 rally 逻辑。

改用直接设置字段，触发游戏原生的 `change_rally_point` 脚本：

```lua
-- 设置集结点（游戏源码中 GUI 处理完点击后就是设这两个字段）
entity.barrack.rally_pos.x = target_x
entity.barrack.rally_pos.y = target_y
entity.barrack.rally_new = true
-- 游戏的 change_rally_point 脚本自动：遍历 soldiers → rally_formation_position → 移动
```

**原则：只做用户能做的操作（选哪个兵营、点哪个位置），其余全交给游戏引擎。**

### 建塔时的默认集结点

游戏的 `tower_build` 脚本在建造完成后会：
1. 调用 `nearest_nodes(pos)` 找塔位附近的路径节点
2. 通过 `set_defend_point_node` + `path_width` 计算 `tower.default_rally_pos`
3. 兵营脚本用 `vclone(default_rally_pos)` 初始化 `barrack.rally_pos`

**因此建兵营后不需要手动设集结点**——游戏会自动把士兵放在路径上的正确位置。
只有需要策略性移动时才调用 set_rally_point。

**运行时验证结果 (dump_barrack 输出)：**

```json
{
  "soldiers_type": "table",
  "soldiers_count": 3,
  "soldiers_detail": [
    {"key": "1", "val_type": "table", "id": 56, "has_nav_rally": true, ...},
    {"key": "2", "val_type": "table", "id": 57, "has_nav_rally": true, ...},
    {"key": "3", "val_type": "table", "id": 58, "has_nav_rally": true, ...}
  ]
}
```

### 踩坑记录：自定义距离匹配的失败

之前尝试自己实现士兵-兵营绑定（每帧扫描全实体，用距离匹配），导致以下问题：
- 士兵重生后位置远离兵营，距离匹配错误 → 绑到别的兵营 → 士兵乱跑
- 多兵营时士兵交叉绑定（flip-flop）
- 升级兵营后实体ID变化，旧绑定失效
- 反复设置 `nav_rally.new = true` 打断士兵战斗AI

**教训：优先查源码原生机制，不要自己发明轮子。**

### 源码提取方法论

LuaJIT 字节码虽然不能直接阅读，但字符串常量以明文存储，可用正则提取：

```python
import zipfile, re
z = zipfile.ZipFile('Kingdom Rush.exe')
data = z.read('all/components.lua')
strings = [s.decode('ascii') for s in re.findall(rb'[\x20-\x7e]{3,}', data)]
```

通过分析字符串常量的上下文顺序（LuaJIT 字节码中常量顺序与代码引用顺序对应），可以大致还原函数逻辑。

## 游戏自动化 (2026-03-15, Bridge v5.7)

### 界面检测

三种界面的区分信号：

| 界面 | 检测方法 |
|------|----------|
| **关卡内** | `game.store ~= nil` → true |
| **地图** | `main.handler.last_item_name == "map"` |
| **主菜单** | `main.handler.last_item_name == "slots"` |
| **加载/过场** | `main.handler.last_item_name == nil` → `"splash"` 等 |

**踩坑记录：**
- `game.store` 在地图和主菜单都是 nil，不能区分这两者
- `screen_map` 全局变量在所有界面都存在，无法用来区分
- `main.handler` 的字段结构在所有界面完全一致
- ★ `main.handler.last_item_name` 才是唯一可靠的区分信号
- ★ `main.handler.active_item` 包含当前界面的实际对象和方法

### 主菜单 → 地图

```lua
-- active_item 上有 handle_slot_button 方法
local ai = main.handler.active_item
ai:handle_slot_button(1)  -- 加载存档槽1，进入地图
```

**踩坑：** `pairs()` 遍历 `show_slots` 表会导致连接断开（大表/自定义 metatable 问题）

### 地图 → 关卡

```lua
-- 用全局 screen_map 对象，不是 active_item
screen_map:start_level(level_idx, level_mode)
-- level_mode: 1=普通, 2=钢铁, 3=英雄
```

**★ 关键发现：`level_mode` 从 1 开始，不是 0！**

**踩坑记录：**
- `start_level(4, 0)` → 蓝屏 `level04_waves_nil.lua`（mode=0 被拼成文件名后缀 `_nil`）
- `start_level(4, "normal")` → 同样蓝屏 `level04_waves_nil.lua`
- `start_level(4)` → 不传 mode → 连接断开
- ★ `start_level(4, 1)` → 正确进入关卡

**mode 值来源：** `string.dump` 提取函数常量得到参数名 `self, level_idx, level_mode`，
加上 `game.store.level_mode = 1`（campaign 模式），确认 1=普通模式

**active_item vs screen_map：**
- `main.handler.active_item` 在地图界面有 `start_level` 函数
- 但实际测试中用 `active_item:start_level()` 不可靠
- ★ 用全局 `screen_map:start_level()` 才稳定可用

### 关卡列表

```lua
-- 从 active_item.user_data 读取
local ud = main.handler.active_item.user_data
for i = 1, 100 do
    local lv = ud.levels[i]
    if not lv then break end
    -- lv.stars: 0=未通关, 1/2/3=已通关星数
end
```

**踩坑：** `pairs(ud.levels)` 不工作（自定义 metatable），必须用 `levels[i]` 索引访问

### 关卡内 → 地图 / 重试

```lua
-- 返回地图（胜利或失败后）
game.game_gui:go_to_map()

-- 关卡内重试（不回地图）
game.game_gui:restart_game()
```

### 启动器自动化

LÖVE 启动器窗口（标题 "王国保卫战"）使用自定义控件，Win32 `EnumChildWindows` 找不到标准按钮。

解决方案：
1. `FindWindowW(None, "王国保卫战")` 找到窗口
2. `GetWindowRect` + `ClientToScreen` 获取窗口屏幕坐标
3. "开始"按钮在客户区 (78%, 93%) 位置
4. `SetCursorPos` + `mouse_event` 真实鼠标点击

**注意：** 坐标基于客户区百分比计算，换显示器/分辨率不受影响

### level_data 结构

```lua
screen_map.level_data[i]  -- 只有 iron=table, upgrades=table
-- 没有关卡名称字段（name/level_name 都不存在）
```

## 火雨伤害数据 (运行时提取)

| 参数 | 值 | 来源 |
|------|-----|------|
| fireball_count | 5 | `power_fireball_control` 模板 |
| 单球伤害 | 50-80 | `power_fireball.attacks.dmg_min/max` |
| 直击半径 | 60 | `power_fireball.attacks.range` |
| 火坑 DPS | 10-20 | `power_scorched_earth.aura.aura_damage_min/max` |
| 火坑半径 | 65 | `power_scorched_earth.aura.aura_range` |
| 火坑持续 | 5秒 | `power_scorched_earth.health.hp` (= lifetime) |

**AI 有效伤害计算：**
- 直击区域内敌人：`min(当前HP, 400)`（5球 × 80 最大伤害）
- 同路径后方 `speed × 5秒` 范围内敌人：`min(当前HP, 75)`（仅火坑 DPS × 5秒）

## 待探索

- [x] 升级塔 (已验证 ✓)
- [x] 英雄移动 (已验证 ✓)
- [x] 技能释放 (已验证 ✓ 火雨+增援)
- [x] 兵营士兵控制 (已验证 ✓ 原生 barrack.soldiers)
- [x] 界面检测 (已验证 ✓ main.handler.last_item_name)
- [x] 关卡启动 (已验证 ✓ screen_map:start_level)
- [x] 关卡列表 (已验证 ✓ user_data.levels[i].stars)
- [x] 游戏启动器自动化 (已验证 ✓ mouse_event)
- [ ] 塔位地形类型 (grass/snow/wasteland) 自动识别
- [ ] 卖塔改用原生机制
- [ ] 士兵重生后是否自动继承 barrack.rally_pos（待测试）

## 关键全局常量

```
GAME_MODE_CAMPAIGN = (number)
GAME_MODE_ENDLESS = (number)
GAME_MODE_IRON = (number)
TICK_LENGTH = (number)

-- 伤害类型
DAMAGE_PHYSICAL, DAMAGE_ELECTRICAL, DAMAGE_EXPLOSION,
DAMAGE_MAGICAL_ARMOR, DAMAGE_POISON, DAMAGE_INSTAKILL, etc.

-- GUI 模式
GUI_MODE_TOWER_MENU, GUI_MODE_RALLY_HERO, GUI_MODE_POWER_1,
GUI_MODE_BAG, GUI_MODE_SELECT_POINT, GUI_MODE_DISABLED, etc.

-- 塔/单位大小
TOWER_SIZE_LARGE, UNIT_SIZE_LARGE, UNIT_SIZE_MEDIUM, UNIT_SIZE_NONE
```

## 文件清单

| 文件 | 作用 |
|------|------|
| `patch_kingdom_rush.py` | exe 注入工具 (修改 ZIP, 注入 bridge) |
| `kingdom_rush_bridge.lua` | Lua TCP 桥接服务端源码 |
| `kingdom_rush_bot.py` | Python TCP 客户端 (交互/观察/AI) |
| `kingdom_rush_notes.md` | 本文件 — 逆向工程笔记 |
