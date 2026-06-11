# 泰拉瑞亚行为树 V2 — 原子行为架构（调研融合版）

> 设计原则：**Python 负责"去哪"，C# 负责"怎么走"**。
> 定义少量原子行为，每个简单、可测试、稳定不改。复杂行为 = 原子行为的组合。
>
> 调研来源：
> - Terraritone: A* + JumpLength 跳跃编码（→ C# 端寻路）
> - TerrarAI: 相对坐标扫描 + 聚合查询（→ 桥接层升级）
> - 2D 平台寻路教程: 挖掘代价 + 单向平台处理（→ A* 设计）

---

## 一、架构总览

```
┌─────────────────────────────────────────┐
│           高层任务 (Python)              │
│  survival_loop (慢脑LLM驱动)            │
│  explore_underground                    │
│  "去哪里" — 战略目标(LLM) + 战术决策     │
│  ↓ 只调用中层接口                        │
├─────────────────────────────────────────┤
│           中层行为 (Python)              │
│  follow_cave, mine_vein, fight_enemy    │
│  scan_surroundings, manage_inventory    │
│  "做什么" — 组合原子行为实现具体功能      │
│  ↓ 调用原子接口                          │
├─────────────────────────────────────────┤
│           原子行为 (Python→C# 命令)      │
│  navigate_to, mine_tile, scan_area,     │
│  get_state, use_item, place_tile        │
│  "怎么做" — 每个只做一件事               │
│  ↓ TCP JSON 通信                        │
├─────────────────────────────────────────┤
│           C# Mod 端 (LumiBridge)        │
│  A* 寻路引擎, 路径执行, 世界状态读取     │
│  "怎么走" — 处理跳跃/下落/挖掘/平台穿越  │
└─────────────────────────────────────────┘
```

### 与 V2 旧版的关键区别

**旧版**: Python 负责 "去哪" + "怎么走"，C# 只是手脚执行器
**新版**: Python 负责 "去哪"，C# 负责 "怎么走"（A* 寻路）

这从根本上解决了之前"不停打补丁"的问题——最容易出 bug 的导航逻辑（跳台阶、绕障碍、穿平台、下落判定）从 Python 手写规则变成 C# 端经过验证的 A* 算法。

---

## 二、C# Mod 端新增能力

### 2.1 A* 寻路引擎（核心新增）

> 来源: Terraritone 的 JumpLength 编码 + 2D 平台寻路教程

在 LumiBridge 中实现带跳跃感知的 A* 寻路：

#### Tile 三分类
```csharp
enum TileType { Empty, Block, OneWay }

// Empty:   空气 + 装饰物（非实心）
// Block:   实心方块
// OneWay:  平台（tileSolid && tileSolidTop），可站上去，可按↓穿过
```

#### JumpLength 状态编码
```
每个 A* 节点 = (x, y, JumpLength)
同一个 tile 位置可以有多个不同 JumpLength 的节点

JumpLength 含义:
  0         站在地面，可任意移动
  奇数      上升阶段，只能垂直移动（不能水平！）
  偶数 > 0  可水平移动
  ≥ maxJump*2  过了跳跃顶点，开始下落，不能再向上
```

#### 跳跃状态转移
```
地面 (onGround)     → 0
撞天花板 (atCeiling) → max(maxJump*2+1, curr+1)  立刻下落
空中上升, jumpLen<2  → 3  起跳第一步
空中上升, 偶数       → curr + 2  继续上升
空中上升, 奇数       → curr + 1  上升中水平步
空中下落, 偶数       → max(maxJump*2, curr+2)
空中下落, 奇数       → max(maxJump*2, curr+1)
空中水平             → curr + 1
```

#### 挖掘代价（关键设计）
```csharp
// Block 节点有代价而非不可通行
// A* 自动权衡"绕路 vs 挖穿"
byte GetTileWeight(Tile tile, bool allowDig) {
    if (!tile.active()) return 1;                       // 空气
    if (TileID.Sets.Platforms[tile.type]) return 1;     // 平台 (OneWay)
    if (!allowDig) return 0;                            // 不允许挖 → 不可通行
    int digTime = CalcDigTime(tile.type, pickPower);
    return (byte)Math.Min(255, 1 + digTime);            // 代价 = 1 + 挖掘时间
}
```

#### 代价函数
```
G = parent.G + tileWeight + jumpLength / 4
// 跳跃代价略高 → 算法偏好走平地而非跳跃
// 挖掘代价更高 → 能绕就绕，实在绕不了才挖
```

#### maxJumpHeight 动态计算
```csharp
int GetMaxJumpHeight() {
    float jumpSpeed = Player.jumpSpeed;  // 基础 5.01
    // + 各种装备加成 (云瓶、火箭靴等)
    float gravity = Player.defaultGravity;  // 0.4
    int height = 0;
    float vel = jumpSpeed;
    while (vel > 0) { vel -= gravity; height++; }
    return height;  // 基础约 12-13 格
}
```

#### 局部计算
```
以玩家为中心取 200x200 区域做 A*
超出范围时重新扫描
不需要加载整个世界地图
```

### 2.2 路径执行器

A* 找到路径后，C# 端自动执行：

```
路径简化: 只保留关键转折点（跳跃起飞/着陆/方向变化/平台边缘）
执行循环 (每帧):
├── 清空按键
├── 到达当前路点 → 前进到下一路点
├── 目标在左/右 → 按←/→
├── 需要跳跃 → 按跳跃键 N 帧
├── 目标在下方 + 站在 OneWay 平台 → 按↓穿过
├── 路径经过 Block → 对准方块 + 使用镐子
├── 卡住检测: 30帧不动 → 重新寻路
└── 定期向 Python 报告进度
```

### 2.3 新增/升级的 C# 命令

| 命令 | 说明 | 来源 |
|------|------|------|
| `navigate_to(x, y, allow_dig)` | **A\* 寻路 + 自动执行**，返回进度状态 | Terraritone + 2D寻路 |
| `scan_relative(rx, ry, w, h)` | 以玩家中心为原点的相对坐标扫描 | TerrarAI |
| `get_state(include[])` | 一次返回多种状态（玩家+NPC+地形） | TerrarAI |
| `get_nearest_npcs(filter, count, range)` | 距离排序 + 取最近N个，已转为tile坐标 | TerrarAI |
| `scan_area` 升级 | 返回数据增加 tile 分类 (Block/OneWay/Ore) | Terraritone |

---

## 三、原子行为清单（Python 端）

### 3.1 导航原子（最大的变化）

| 原子 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `navigate_to(x, y, allow_dig)` | 目标坐标 + 是否允许挖 | 状态流(moving/arrived/stuck) | **C# A\* 寻路**。自动处理跳跃/下落/挖掘/平台穿越。Python 只等结果 |
| `mine_tile(x, y)` | 坐标 | bool(挖掉?) | 挖一格方块。不变 |
| `place_tile(x, y, type)` | 坐标+类型 | bool | 放一格方块。不变 |
| `use_item(x, y)` | 目标坐标 | 无 | 对准某坐标使用当前物品 |
| `select_slot(n)` | 快捷栏编号 | 无 | 切换手持物品 |

**注意**: `move_horizontal`, `dig_down_to`, `climb_up_to`, `jump` **全部被 `navigate_to` 吸收**。
Python 端不再需要知道"跳台阶"、"挖隧道"、"搭平台上去"这些细节。

### 3.2 感知原子

| 原子 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `get_state(include[])` | 要什么数据 | 聚合结果 | 一次返回玩家+NPC+地形，减少网络往返 |
| `check_tile(x, y)` | 坐标 | {hasTile, type, tileClass} | 增加 tileClass: Block/OneWay/Empty |
| `scan_area(x, y, w, h)` | 绝对坐标矩形 | [{x, y, t, class}, ...] | 增加 tile 分类 |
| `scan_relative(rx, ry, w, h)` | 相对玩家中心的偏移 | 同 scan_area | 不用先获取玩家位置再算 |
| `get_nearest_npcs(filter, n, range)` | 过滤条件 | [NPC...] 已按距离排序 | C# 端排序，tile坐标 |

### 3.3 查询原子

| 原子 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `get_inventory()` | 无 | [Item, ...] | 不变 |
| `find_item(name/id)` | 物品名/id | [slot, ...] | 不变 |

### 原子行为的关键约束

1. **每个原子行为只做一件事**，不包含策略/决策
2. **navigate_to 是"怎么走"的唯一接口** — Python 不关心路径上发生什么
3. 原子行为的 bug 修完后**锁定不改**，上层通过组合解决复杂场景
4. NPC 数据**统一使用 tile 坐标**，C# 端完成像素→tile 转换

---

## 四、中层行为（大幅简化）

### 4.1 感知类

#### scan_surroundings(radius)
```
一次扫描，返回周围所有有用信息：

result = scan_relative(-r, -r, 2r, 2r)
解析出：
├── solid_set: set((x,y))   — 快速查实心
├── ores: [(x,y,type)]      — 矿石位置
├── chests: [(x,y)]         — 箱子位置
├── pots: [(x,y)]           — 罐子位置
├── platforms: [(x,y)]      — 平台位置（新增，区分 OneWay）
├── passages: [Passage]      — 可通行方向
└── open_spaces: [Space]     — 大空腔（潜在探索目标）
```

**关键设计**：一次 scan 调用，解析出所有信息。不再多次请求。

#### check_threats()
```
检查附近是否有可达的敌人：
├── get_nearest_npcs(hostile=true, count=3, range=40)
│   C# 端已按距离排序、已转tile坐标
├── 过滤垂直距离 ≤ 10 格
└── 返回最近可达敌人（或 None）
```

### 4.2 战斗类

#### fight_enemy(npc)
```
和单个敌人战斗：
├── 判断可达性（垂直距离 ≤ 10 格）
│   └── 不可达 → 立即返回 False
├── select_slot(0) 切武器
├── 战斗循环：
│   ├── navigate_to(npc.x, npc.y) 追击
│   │   A* 自动处理追击路径上的障碍
│   ├── 距离 ≤ 3 → 面朝敌人 + use_item 攻击
│   ├── 距离 ≤ 1 → 后退
│   └── 敌人移动 → 更新 navigate_to 目标
└── 击杀 → True，超时/消失 → False
```

### 4.3 采矿类

#### mine_vein(start_x, start_y, ore_type)
```
BFS 采集矿脉：
├── navigate_to(start_x, start_y)  A* 自动走过去
│   └── 到不了 → 返回 0
├── BFS 循环：
│   ├── mine_tile 挖当前矿石
│   ├── 检查4邻居是否同类矿石
│   │   ├── 在镐子范围内 → 加入队列
│   │   └── 超出范围 → navigate_to 走过去（A* 处理路径）
│   └── 队列空 → 结束
└── 返回挖了多少格
```

### 4.4 洞穴探索类（大幅简化）

#### follow_cave(path, max_steps)
```
沿洞穴走，核心循环（每步）：

scan = scan_surroundings(20)

优先级决策：
├── P0: check_threats() → fight_enemy()
├── P1: scan.ores 有矿 → mine_vein()
├── P2: scan.open_spaces 有更深的空间
│   └── navigate_to(space.x, space.y, allow_dig=true)
│       A* 自动决定: 走过去/跳下去/挖过去
├── P3: scan.passages 有未探索通道
│   └── navigate_to(passage.end_x, passage.end_y)
│       A* 自动处理通道中的台阶/缝隙/平台
├── P4: 所有方向已探索 → dead_end, 回溯
└── 每步：记录位置到 path，标记 visited chunk
```

**对比旧版 follow_cave 的简化**：
```
旧版: scan → 分析落点/空腔/通道 → 手动 move_horizontal → 手动 dig_down_to → 手动判断掉落
新版: scan → 选目标 → navigate_to(target)  就一行。A* 处理一切。
```

`navigate_to` 吸收了：
- ~~move_horizontal 到落点 → 等待掉落 → 没掉就扩大缝隙~~ → A* 自动找下落路径
- ~~cavity_below → dig_down_to~~ → A* 看到挖掘代价更低就自动挖
- ~~判断台阶/跳/挖~~ → A* 全局最优路径自动处理

### 4.5 背包管理类

#### manage_inventory()
```
├── clean_trash()  丢弃低价值方块
├── auto_equip()   更好装备自动替换
└── count_empty_slots() < 3 → 标记需要回家存箱
```

---

## 五、高层任务（基本不变）

### 5.1 地下探索流程

```
explore_underground(direction, max_time)

阶段1: 找洞穴
├── navigate_to(方向上的目标点)  A* 处理地面行走
├── 每走一段，scan_surroundings 看脚下
│   └── 发现大空腔 → 记录入口
└── 走到 max_distance 没找到 → 返回

阶段2: 进入洞穴
├── navigate_to(entrance_x, entrance_y, allow_dig=true)
│   A* 自动选择最优进入方式:
│   ├── 有侧面入口 → 走进去
│   ├── 头顶有缝隙 → 跳下去
│   └── 需要挖 → 挖最薄的覆盖层进入
├── 判断是否已进入地下
└── 记录 surface_y

阶段3: 洞穴探索循环
├── follow_cave(path, max_steps)
│   内部已处理：战斗、挖矿、导航（全靠 navigate_to）
├── 每分钟检查：
│   ├── 背包满 → manage_inventory() → 还满 → 回家
│   ├── 血量低 + 没药 → 回家
│   └── 超时 → 回家
└── 触发返回条件

阶段4: 返回
├── 沿 path 逆序回走
│   └── 每个路径点：navigate_to(x, y)
│       A* 自动处理回程路径（可能和来时不同）
├── 回到地面
└── navigate_to(home_x, home_y) 走回家
```

### 5.2 生存循环（慢脑驱动）

```
survival_loop()  — 已实现

架构:
┌─────────────────────────────┐
│  StrategicBrain (后台线程)   │
│  LLM (ARK API, medium思考)  │
│  输入: 状态摘要 + 进度指南   │
│  输出: set_goal → Queue      │
└──────────┬──────────────────┘
           │ goal_queue (maxsize=1)
┌──────────▼──────────────────┐
│  战术层 (规则循环)           │
│  有目标 → execute_goal()     │
│  无目标 → fallback 巡逻+探洞 │
└─────────────────────────────┘

主循环:
├── 初始化: init_base → auto_equip → 启动慢脑 → 请求首个目标
├── loop:
│   ├── HP < 30% → quick_heal + 逃跑 (最高优先级)
│   ├── HP < 50% → quick_heal
│   ├── 附近有敌人 → fight_nearest_enemy()
│   ├── goal_queue 有目标 → execute_goal()
│   │   ├── gather: explore_underground → 回家 → count_item 检查
│   │   ├── craft: 基地 check_upgrade + 直接合成
│   │   ├── explore: explore_underground(direction)
│   │   └── boss_prep: 检查召唤物+装备达标
│   ├── 目标完成/失败 → build_status_summary → brain.request_goal
│   └── 无目标 → fallback (巡逻 + 定时探洞)
└── 慢脑失败重试: 3次 × 3秒间隔，全失败则等下次触发
```

---

## 六、关键设计决策

### 6.1 navigate_to 是一切导航的入口

**旧版问题**：move_horizontal + dig_down_to + climb_up_to + enter_cave 各自有复杂逻辑，互相调用，bug 层出不穷。

**新版**：Python 只说 `navigate_to(x, y)`，C# 端 A\* 全局规划最优路径。

```
Python 端:
  engine.navigate_to(100, 300, allow_dig=True)
  # 等待回调...

C# 端自动处理:
  1. 建 200x200 局部 Grid（Block=挖掘代价, OneWay=1, Empty=1）
  2. A* 搜索（带 JumpLength 跳跃状态）
  3. 路径简化 → 关键点
  4. 按帧执行: 移动/跳跃/下穿平台/挖掘
  5. 卡住 → 重算
  6. 向 Python 报告: moving → arrived / stuck

Python 收到:
  {"type": "nav_status", "status": "moving", "progress": 0.6, "x": 80, "y": 280}
  {"type": "nav_status", "status": "arrived", "x": 100, "y": 300}
  {"type": "nav_status", "status": "stuck", "reason": "no_path"}
```

### 6.2 一次扫描多次使用（保留）

scan_surroundings 一次 scan_relative 调用，解析出矿石/箱子/通道/空间。
升级点：scan 数据现在包含 tile 分类（Block/OneWay），更准确。

### 6.3 NPC 数据统一（升级）

**旧版**: Python 端手动 `int(npc["x"] / 16)` 转换。
**新版**: C# 端 `get_nearest_npcs` 直接返回 tile 坐标 + 距离排序。Python 端零转换。

### 6.4 挖掘决策自动化（新增）

**旧版**: Python 手动判断"这里该挖还是绕"。
**新版**: A\* 代价函数自动权衡。Block 权重 = 1 + 挖掘时间，路径自然倾向绕过厚墙、挖穿薄壁。

---

## 七、与现有代码的关系

| 现有代码 | 新架构中的位置 | 改动 |
|----------|---------------|------|
| `move_to` (V1+V2 ~500行) | **删除** → 被 C# navigate_to 替代 | Python 不再处理移动细节 |
| `analyze_terrain` (~200行) | **删除** → A\* 不需要特征分析 | 全局搜索替代局部特征 |
| `scan_terrain_ahead` | **删除** → 被 scan_relative 替代 | — |
| `mine_tile` | 不变（原子） | — |
| `check_tile` | 升级：返回 tileClass | C# 端增加分类 |
| `scan_area` | 升级：返回 tileClass + 支持相对坐标 | C# 端增加 |
| `scan_passages + scan_drop_points + scan_cavity_below` | `scan_surroundings`（中层） | 合并 |
| `cave_explore_step` | `follow_cave`（中层） | **大幅简化**: 选目标 → navigate_to |
| `_dig_tunnel_to` | **删除** → 被 navigate_to(allow_dig) 吸收 | — |
| `enter_cave` | **简化** → navigate_to(entrance) 一行 | — |
| `fight_nearest_enemy` | `fight_enemy`（中层） | 微调 |
| `explore_underground` | 高层任务 | 简化 |

**代码量预估**：Python 端减少 ~800 行（move_to + analyze_terrain + dig_tunnel + enter_cave 复杂逻辑）。C# 端增加 ~400 行（A\* 引擎 + 路径执行器）。

---

## 八、迁移计划

### Phase 0: C# 端 A\* 寻路引擎（最关键，先做）
- [ ] LumiBridge 新增 TileType 三分类 (Block/OneWay/Empty)
- [ ] 实现 A\* 核心（参考 Terraritone，加 JumpLength 编码）
- [ ] 实现 Grid 构建（局部 200x200，Block=挖掘代价）
- [ ] 实现 maxJumpHeight 动态计算
- [ ] 实现路径简化（只保留转折点）
- [ ] 实现路径执行器（按帧移动/跳跃/挖掘/穿平台）
- [ ] 实现卡住检测 + 重新寻路
- [ ] 新增 `navigate_to` 命令 + 状态回调
- [ ] **测试**: 地面平走、跳台阶、下落、穿平台、挖穿薄墙

### Phase 1: C# 端感知升级
- [ ] scan_area 返回 tileClass (Block/OneWay/Ore)
- [ ] 新增 scan_relative（相对坐标扫描）
- [ ] 新增 get_state 聚合查询
- [ ] 新增 get_nearest_npcs（距离排序 + tile 坐标）

### Phase 2: Python 端重组
- [ ] 删除 move_to, analyze_terrain, scan_terrain_ahead, _dig_tunnel_to
- [ ] 实现 scan_surroundings（调用 scan_relative，解析所有信息）
- [ ] NPC 数据使用 get_nearest_npcs（不再手动转换坐标）
- [ ] fight_enemy 用 navigate_to 追击

### Phase 3: 中层行为重写
- [ ] follow_cave（选目标 → navigate_to）
- [ ] mine_vein（navigate_to 矿石位置 → mine_tile）
- [ ] enter_cave = navigate_to(entrance, allow_dig=true)

### Phase 4: 高层任务简化
- [ ] explore_underground 用新中层接口
- [ ] 测试完整洞穴探索流程
- [ ] 性能调优（A\* 搜索范围、重算频率）

**每个 Phase 完成后测试，通过后再进下一个。**
Phase 0 是基础，必须先稳定。后续 Phase 可以渐进式推进。

---

## 九、风险与备选方案

### 风险 1: A\* 在 C# 端的性能
200x200 = 40000 格，带 JumpLength 多节点。Terraritone 在全图上跑也没太大问题。
**缓解**: 限制搜索步数上限（如 5000），超限返回 no_path。

### 风险 2: A\* 路径与真实物理不匹配
Terraritone 的已知问题：惯性过冲、1格台阶跳过。
**缓解**: 路径执行器中加 tolerance（1 block 误差），卡住就重算。比 Python 端手动处理简单得多。

### 风险 3: 动态地形导致路径失效
挖矿/战斗破坏方块后，原路径可能不通。
**缓解**: 每 N 帧检查前方路径是否仍然有效，无效则重新寻路。

### 备选方案: 如果 A\* 开发周期太长
先在 Python 端用简化版 V2（只保留 move_horizontal V1 逻辑 + scan_surroundings），
后续再迁移到 C# A\*。两者的中层/高层接口完全相同，只是 navigate_to 的实现不同。

---

## 十、变更记录

### 2026-03-21: 移除搭平台模式，增加空气路径惩罚

**问题**: 角色掉入大空腔后无法脱出，在洞底无限弹跳。

**根因分析**: A* 寻路与路径执行器的"可行"标准不一致。A* 用跳跃高度编码规划路径时，会选择洞壁上的微小凸起作为跳板（代价低）。但路径执行器在实际物理环境下无法精确落到1格宽的凸起上。导致：A* 认为空气路径可行 → 执行失败 → 重新寻路 → 选同样的空气路径 → 循环。

**尝试过的方案**:
1. ~~搭平台模式~~: 路径执行器检测到跳不上去时自动放置木平台往上搭。**失败原因**: 每次只能搭3格，搭完后退出搭建模式→回到普通移动→又检测到跳不上去→重新搭建→无限循环。且天花板检测、边缘判定等衍生问题不断。
2. 下落惩罚: A* 对超过最大跳跃高度的下落增加高代价。**效果**: 能减少主动跳入深洞，但无法解决已经在洞底的情况（从洞底往上是跳跃不是下落）。
3. 路径简化保留落脚点: 路径简化不再删除垂直间距超过最大跳跃高度的中间落脚点。**效果**: 正确但不够——如果空腔内根本没有足够的落脚点，路径本身就不该走空气。

**最终方案**: 重新寻路时根据失败原因调整代价。如果触发原因是"路径点不可达"（空气路径走不通），大幅提高空气格的移动代价，迫使 A* 选择挖掘路线。同时保持现有的逐次加大镐力机制，双管齐下。

**设计原则**: 不在路径执行层打补丁（如搭平台），而是让寻路算法自己修正决策。符合行为树V2"Python负责去哪，C#负责怎么走"的职责划分。
