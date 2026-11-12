# 给独立原型用的最小接口

这些结构从现有系统核对后提取，目的是让贡献者可以先准备数据、规则或渲染器。**它们不是完整插件系统，不含直播宿主、画板服务、演唱播放器或角色素材。**选定具体功能后，维护者仍需负责正式接线和实机验收。

在仓库根目录运行，无需依赖、密钥或声卡：

```bash
python -m examples.prototype_contracts
```

示例打印游戏决策与回应、读取合成画作数据，并在临时目录生成一秒测试音及歌词文件，检查后自动清理。它不播放声音、不启动游戏、不请求模型，也不会把示例完成回执送到任何服务。

## 新游戏：规则与公开局面

[interfaces/minigame.py](../interfaces/minigame.py) 提供真实的 `MinigameAdapter` 和 `DecisionRequest` 结构。

| 方法或字段 | 原型应提供的内容 |
|---|---|
| `reset(seed)` | 重置规则状态；需要复现实验时使用传入种子。 |
| `snapshot(viewer)` | 该观察者允许看到的局面。隐藏信息游戏不能把完整内部状态直接交给角色。 |
| `legal_actions(actor)` | 当前角色允许执行的动作列表。动作的字段由该游戏定义。 |
| `current_player_slot()` | 当前玩家的位置索引，由宿主映射到角色；现有双人规则使用 0 / 1。 |
| `apply_action(actor, action)` | 执行动作，返回该动作造成的事件信息。 |
| `is_terminal()` / `result()` | 是否结束，以及结束后的结果。运行中的 `result()` 是否可调用由规则定义；现有实现会拒绝。 |
| `describe_decision(request)` | 把局面解释为决策输入；现有实现包含 `state_text`、`tools`、`spectator_text`。 |
| `decision_id` / `session_id` / `revision` | 本次决策、游戏会话、状态版本的关联字段。 |

流程是宿主选定当前角色 → 取该角色可见局面与合法动作 → 发起决策 → 核对仍是当前待处理请求、角色和动作 → 执行动作。回应示例包含 `decision_id`、`actor`、`action`，与现有宿主方法参数一致。`DecisionRequest` 是 Python 数据结构，不等同于完整事件总线消息。

**上述检查不由 Protocol 自动完成。**迟到、重复、错误角色和非法动作必须由原型宿主明确拒绝；正式宿主未包含在本仓库。结构中嵌套的字典也不是不可变数据。示例只构造一个小棋盘的决策输入，没有实现游戏规则，亦不代表本轮选定了五子棋或其他游戏。

可独立交付：候选游戏的适配器、可见画面和一局演示，以及隐藏信息、非法动作、重置与结束的行为测试。新游戏选型和直播启停另行确认，见[任务草稿](issue-drafts/new-game.md)。

## 绘画：画作消息与完成回执

结构在 [interfaces/drawing.py](../interfaces/drawing.py)，输入样本在 [drawing.json](../examples/fixtures/drawing.json)。TypedDict 用于阅读和类型检查，不自动校验外部 JSON。

- `loading`：主题和起始时间；`draw`：画作与动画参数；`clear`：清除当前画作。
- `strokes` 是已经插值后的**画布像素点**，层次为“笔画 → 点 → `[x,y]`”；不是模型生成时的网格坐标。
- `canvas_width` / `canvas_height` 表示源画布；`draw_rect` 是 `[x0,y0,x1,y1]` 两个角的位置。不同大小的预览需要自行换算。
- `stroke_palette` 是颜色列表，现有消费者按笔画序号循环取色；`anim_interval_ms` / `stroke_pause_ms` 控制线段和笔画间隔。
- `display_duration_ms` 是画完后的展示时间；`visual_timeout_ms` 供服务端处理没有回执时的等待上限，不能拿它冒充正常完成。
- 重连消息可能有 `snapshot: true`：立即还原最终画面，不重播动画，**不发送完成通知**。

正常逐笔绘制结束后发送一次 `{"type":"draw_complete","draw_id":"当前编号"}`。严格舞台模式还要求 `consumer: "stage"` 且 `visible: true`，后端只接受仍在等待的画作编号。普通画板的基础回执不足以完成严格舞台等待。独立预览应如实报告自己的可见性，正式 `stage` 身份和可见性由维护者接入舞台时提供，不能仅把字段写成 true 作为验收。

新画作、清除或取消到来时，旧动画定时器和旧完成通知都必须失效。示例脚本只打印回执形状，没有渲染器，因此不构成“已经画完”的证据。

这些是**现有曲线画板**的接口。像素画、分镜或其他形式仍需确定自己的表现与数据扩展，不能把样本渲染成功当成模型已经会生成该形式。见[任务草稿](issue-drafts/drawing-presentation.md)。

## 演唱：资产交接

[interfaces/singing.py](../interfaces/singing.py) 提供 `SongAssets`、`SongRecord` 和 `SongValidation` 数据结构。曲库 JSON 示例在 [singing_catalog.json](../examples/fixtures/singing_catalog.json)。稳定 `id` 用于引用歌曲，`basename` 用于定位同名资产；目录由调用者提供。

| 文件名 | 用途与现有校验条件 |
|---|---|
| `{basename}.wav` | 混合母带，必需。 |
| `{basename}人声.wav` | 独立人声，必需。 |
| `{basename}伴奏.wav` | 独立伴奏，路径字段存在，文件缺失只警告。 |
| `{basename}.lrc` | 带时间戳歌词，必需。 |
| `{basename}.zh.txt` | 可选翻译；`subtitle_mode="bilingual"` 时必需，非空有效译文行数需匹配 LRC 中带时间戳的非空歌词行数。 |

实际演唱还检查歌曲启用、角色受支持且处于当前节目、`meaning` 非空等条件；本包只提取结构，**没有发布曲库校验器或演唱服务**。字段可读、文件存在不代表音频合格或已经接入直播。

离线样本的 `speaker_a` 是占位角色，`enabled` 保持 `false`。母带和“人声”都是一秒合成测试音，伴奏是等长静音，用于测试文件交接，不是演唱或音色转换结果。要在自己的临时目录保留样本，可调用 `examples.prototype_contracts.write_synthetic_song(Path("自己新建的空目录"))`；不要传入现有曲库。

这份资产结构同时可供“已有曲库点唱”和“按请求制作新演唱版本”原型参考，没有预设排队、插播、自动入库或制作技术路线。路线 B 仍需真正生成此前未预制的目标演唱音轨，并实听验收；合成测试音不算路线 B 成果。见[任务草稿](issue-drafts/requested-singing.md)。
