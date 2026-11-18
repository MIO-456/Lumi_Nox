# 测试

> **何时读**：要新增 / 修改测试，或需要判断"这次改动算不算验证通过"。
> **何时不用读**：只改文档，或只想跑一次 `python main.py`。
> **复核**：测试入口、覆盖范围或 CI 配置变化时；具体数量以对应提交的运行结果为准。

## 自动检查的边界

自动检查不访问真实云模型、生产服务、声卡、VTS 或商业游戏，也不读取真实 `.env` 和私人配置。使用 Fake、mock 和临时目录隔离这些资源。安装 aiohttp 后，五子棋集成测试会在自有回环端口运行仓库自带的真实规则、网页和整场流程，模型与声音仍为明确的无声 demo；缺少 aiohttp 时跳过该组，CI 会安装它。

**允许测试自己创建的本机 loopback HTTP 服务**：使用临时端口、假响应和明确的启停清理，验证实际 HTTP 请求与宿主接口。它不需要外网或真实凭据；受限环境若禁止本机端口，明确报告环境限制并按该环境流程申请运行，不能删除测试或把未运行写成通过。

## 测试怎么分组

| 文件 | 覆盖什么 | 隔离方式 |
|---|---|---|
| `test_core_refresh.py` | 事件总线超时与迟到响应、仲裁器取消语义、观众名归一化 | 真实模块 + 可控回调 |
| `test_public_core.py` | 零安装演示、记忆衰减过滤、成员身份回填幂等 | `FakeStorage`、演示子进程 |
| `test_package_layout.py` | 从其他目录运行演示、轻量导入、资源定位 | 子进程 + 临时 cwd |
| `test_voice_refresh.py` | TTS / ASR / 播放契约 | SDK 与设备替身 |
| `test_memory_refresh.py` | 记忆构建、检索、落盘、待处理会话 | 临时 SQLite + mock 模型 |
| `test_prototype_contracts.py` | 游戏 / 绘画 / 演唱样本的交接形状 | 离线 fixture |
| `test_game_refresh_buckshot.py` | 知识库、协议、生命周期 | 离线局面 |
| `test_game_refresh_kingdom_rush.py` | 启动超时、套接字生命周期、握手、决策 | `FakeSocket` |
| `test_terraria_package.py` | mod 源码完整、焦点复位、协议版本 | 源码静态检查 |
| `test_game_refresh_word_solvers.py` | 求解器工作量上限 | 固定采样 |
| `test_public_host_services.py` | 本机配置、服务协议、WAV 与播放结果 | 临时配置、本机假 HTTP 服务、假声卡 |
| `test_public_host_session.py` | 角色轮换、取消、历史与完成事件；入队/合成前后文本、取消清屏、事件序号与新场轨迹重置 | 假模型、合成器、播放器与皮套 |
| `test_public_host_telemetry.py` | 请求阶段耗时、已结束记录淘汰与活动请求保留、终态后拒绝迟到更新 | 可注入时钟、内存请求记录 |
| `test_public_host_avatar.py` | VTS 授权、模型核对、嘴型与清理 | 假 WebSocket，不使用真实 token |
| `test_public_host_server.py` | 本机 HTTP、配置脱敏、运行互斥与试听收尾；临时 demo 配置隔离、五子棋依赖预检、运行 ID 过期返回 409 | 临时 HTTP 服务、假模型与播放器 |
| `test_repo_verification.py` | 文档检查和验证脚本的行为 | 临时文件、脚本子进程与静态检查 |
| `test_public_room.js` | 舞台初始化、字幕与取消、断线、单角色、棋盘更新、输入与导出白名单 | Node 内置测试与轻量 DOM 替身，无浏览器或第三方依赖 |

定位方式：先按"改的是哪个子系统"选文件；纯调度 / 仲裁的回归放 `test_core_refresh.py`，跨模块的公开行为放 `test_public_core.py`。

## 怎么跑

```bash
python -m unittest discover -s tests -v          # 全量（-v 便于看用例名）
python -m unittest discover -s tests -p test_core_refresh.py -v  # 单个文件
python -m compileall -q .                        # 编译检查，无输出即通过
node --test tests/test_public_room.js             # 改动直播间前端时另跑（Node 18+）
node --check public_host/web/app.js
node --check public_host/web/room.js
```

`test_public_core.py` 会实际运行 `main.py`。也可用 `./init.ps1` 或 `bash ./init.sh` 汇总编译、文档和测试检查；脚本不安装依赖、不填真实配置。开发中先跑针对性检查，交付前按改动范围完成必要检查，已经通过且代码未变时不必反复全量运行。

## 新增测试的规则

1. **按行为归组**，优先扩展对应文件；新增独立子系统测试时更新本页覆盖说明，不要求维护固定用例数量。
2. **隔离外部资源**：使用替身或临时目录；HTTP 集成测试可自建本机假服务，并确保退出时关闭。
3. **不依赖时序巧合**：不要用 `sleep` 赌竞态；用 `threading.Event`、`ManualTimer` 或可注入的时钟。
4. **不写死环境**：不假设 Windows、不假设存在 `role_volumes.json`、不假设 cwd 是仓库根。
5. **替身命名直白**：`FakeStream`、`FakeStorage` 这类名字让人一眼看出"这里没有真实现"。
6. **一个用例一个行为**：失败信息要能直接指出哪条契约破了。
7. **不使用真实凭据、生产地址或个人绝对路径**；本机回环地址及明确无效的测试地址可用于协议验证。

## 什么算验证通过

| 结论 | 需要的证据 |
|---|---|
| "改好了" | 对照需求与实际行为，相关检查实际通过，关键新行为或修复有必要的回归证据 |
| "只改文档" | `python scripts/check_docs.py` 通过，并人工核对命令、描述与代码一致；行数提示不等于错误 |
| "演示可跑" | `python main.py` 输出到 `=== demo complete ===` |
| "语音 / VTS / 游戏链路可用" | **上述自动检查无法证明**——需要另行安排真实服务或设备验收，并说明环境与实际覆盖范围 |

本地虚拟直播间还需单独记录浏览器检查：体验聊天的双角色、状态字幕与观众输入；打断/停止清屏；刷新和双页恢复同一运行；完整节目的同屏五子棋与返回聊天；窄窗口和断线恢复。调试记录应可区分请求轨迹与完成历史，导出只含元数据、不含文本/配置/Key，默认记录仅在内存中有界保留。这是验收清单，不能从 Python 测试推断浏览器已经通过。真实音频仍由 Python 单播放器输出；SVG 舞台不验证 Live2D 渲染或 VTS 视频捕获。

不要用“测试通过”代替真实链路验收。遇到基线失败先记录提交、命令和错误，判断是否与本次改动有关；无关的既有问题或环境限制不要求贡献者顺手全部修复，也不能掩盖失败或自动回退其他人的工作。

## 与 CI 的一致性

[CI](../.github/workflows/ci.yml) 配置为在 Ubuntu 和 Windows / Python 3.12 上直接运行统一入口；实际通过情况以对应提交的 Actions 结果为准：

```text
Ubuntu:  bash ./init.sh
Windows: ./init.ps1  (PowerShell)
```

两个脚本内部都执行编译、文档检查和 unittest。具体参数见脚本；本地可使用同一入口，避免 CI 与手工验证逐渐分叉。

本地能过不代表 CI 能过：路径大小写、换行符、默认编码都可能只在另一种系统上暴露。涉及这些的改动，至少跑一遍全量测试。

## 失败排查

| 现象 | 先查 |
|---|---|
| `ERROR: ModuleNotFoundError` | 真实后端是否被测试无意导入；自动检查不应要求安装 `requirements.txt` |
| 本机 HTTP 绑定失败 | 运行环境是否禁止 loopback 或端口被占用；测试应使用临时端口，不连接真实服务 |
| 只在 Windows 失败 | 路径分隔符、文件编码、`\r\n` |
| 偶发失败 | 时序依赖，检查是否用了 `sleep` 或真实线程调度 |
| 改了行为但测试仍绿 | 缺用例：契约没有被断言，补一条 |
