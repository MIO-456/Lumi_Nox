# AGENTS.md

> 这是给编码 agent 的**入口文件**，不是手册。它只回答三件事：这个项目是什么、什么绝对不能违反、下一步该读哪份文档。
> 完整文档地图见 [docs/README.md](docs/README.md)。保持入口简短；相关文档过时应随任务修正。用户明确需求优先于本文和历史状态记录。

## 1. 项目是什么

Lumi 与 Nox 是两位 AI 虚拟主播，共同主持一场直播。本仓库是 **open-core 快照**：公开双角色对话调度（点名路由、轮换、发言权仲裁）、实时语音链路、记忆与游戏接入；**不包含**角色人设、真实声线、Live2D 资产、观众数据与商业游戏本体。

- 运行时：Python，**3.12 为 CI 验证基线**
- 入口：`python -m public_host`（本机配置与运行台）、`main.py`（零依赖终端演示）、`python -m examples.prototype_contracts`、各游戏独立入口。
- 手动对话 demo 只用标准库；UI 默认节目模式，零依赖上手需主动选“手动对话”。五子棋网页需 aiohttp，真实声音/VTS 依赖 `requirements-host.txt`。其他游戏、ASR、B 站与 OBS 自动控制尚未接入。

## 2. 确认基线

```bash
python main.py                          # 期望结尾：=== demo complete ===
python -m unittest discover -s tests    # 期望结尾：OK，数量随当前版本变化
python -m compileall -q .               # 无输出即通过
```

这些检查不需要真实凭据、声卡或商业游戏；宿主测试会启动仅绑定回环地址的假 HTTP 服务。安装 aiohttp 后还运行自有临时端口的真实五子棋整场测试，未安装时跳过该可选集成组。失败时记录基线、区分环境与当前任务问题，不自动扩大修复或回退他人改动。
统一验证入口是 `./init.ps1`（Windows）或 `bash ./init.sh`（macOS / Linux / Git Bash）：编译、文档检查、测试。仅文档改动可先做文档检查；代码提交前运行相关检查，合并前完成全量验证。

## 3. 修改边界

| # | 约束 | 原因 |
|---|---|---|
| 1 | 不提交正式角色的私人人设、真实 voice id、观众数据、Live2D 资产或商业游戏二进制；通用示例角色可以公开 | open-core 边界，见 [ARCHITECTURE](docs/ARCHITECTURE.md#whats-here-and-whats-not) |
| 2 | 旧模块按需读环境变量；宿主使用独立 `.local-host/` 本机配置。实际凭据、token、数据库与运行记录永不提交，不自动继承私人配置 | 保护凭据和用户数据，见 `.gitignore` 与 [PUBLIC_HOST](docs/PUBLIC_HOST.md) |
| 3 | 自动测试不访问真实云服务、声卡或商业游戏；允许临时回环假服务、数据库及源码自带的五子棋，结束时清理自身资源 | 无凭据、可复现；真实服务和设备验收单独说明 |
| 4 | 保持核心调度、占位配置和包初始化轻量，避免让零依赖入口导入可选 SDK 或设备；运行接线按既有边界延迟加载 | 保护零依赖入口，不能把整个目录的所有路径都宣称零依赖 |
| 5 | 只用全限定导入（`from core.event_bus import EventBus`），不在根目录新增模块 | 2026-11-12 已完成目录迁移，见 [PACKAGE_LAYOUT](docs/PACKAGE_LAYOUT.md) |
| 6 | 保持 Python 3.12 兼容，不使用仅 3.13+ 的语法或标准库 | CI 基线 |
| 7 | 一次改动只解决一个主题，不夹带无关重构或整文件格式化 | 评审成本；见 [CONTRIBUTING](CONTRIBUTING.md) |
| 8 | 声称"完成"必须附实际执行的命令与结果，未验证部分明确写出 | 防止提前宣告完成 |

## 4. 文档路由（按任务读，不要通读 `docs/`）

| 你要做的事 | 读这份 | 什么时候不用读 |
|---|---|---|
| 第一次把项目跑起来 | [GETTING_STARTED](docs/GETTING_STARTED.md) | 已能跑通 `main.py` |
| 改公开宿主、服务适配、配置或会话 | [PUBLIC_HOST](docs/PUBLIC_HOST.md) → [ARCHITECTURE](docs/ARCHITECTURE.md) | 不涉及宿主 |
| 改节目中控、共用导演或五子棋活动 | [PUBLIC_DIRECTOR](docs/PUBLIC_DIRECTOR.md) → [ARCHITECTURE](docs/ARCHITECTURE.md) | 只改手动对话 |
| 接自己的 VTS 模型 | [VTS_EXAMPLE](docs/VTS_EXAMPLE.md) | 不用皮套 |
| 改 `core/` 的调度、仲裁、事件、状态机 | [ARCHITECTURE](docs/ARCHITECTURE.md) → [CONVENTIONS](docs/CONVENTIONS.md) | 只改文档或纯文案 |
| 写 / 改测试 | [TESTING](docs/TESTING.md) | 不涉及测试改动 |
| 装依赖、配 `.env`、接真实模型或语音 | [DEVELOPMENT](docs/DEVELOPMENT.md) → [VOICE_MODULES](docs/VOICE_MODULES.md) | 只跑零依赖演示 |
| 找模块该放哪、修旧导入路径 | [PACKAGE_LAYOUT](docs/PACKAGE_LAYOUT.md) | 不动导入 |
| 看不懂术语（快脑、发言权、镜像…） | [GLOSSARY](docs/GLOSSARY.md) | 术语已熟悉 |
| 接游戏、改游戏桥 | [games/README](games/README.md) → `docs/games/*.md` | 不碰游戏模块 |
| 做绘画 / 演唱 / 新游戏原型 | [PROTOTYPE_INTERFACES](docs/PROTOTYPE_INTERFACES.md) → [CONTRIBUTION_TASKS](docs/CONTRIBUTION_TASKS.md) | 不做原型 |
| 提交 PR | [CONTRIBUTING](CONTRIBUTING.md) | — |

## 5. 启动工作流（Startup Workflow）

写代码之前，按顺序做完：

1. 确认工作目录是仓库根（`Get-Location` / `pwd`）
2. 读完本文件
3. 先确认用户要求、Issue 或维护者已确认的范围，再确定本轮验证；失败按第 2 节处理
4. 需要了解里程碑时参考 `feature_list.json`、`progress.md`，不要据此擅自选择或替换用户任务
5. 按第 4 节读对应专题文档
6. 看最近改动：`git log --oneline -5`

- **每个 PR 聚焦一个主题**：允许不同贡献者、分支和工作树并行，避免同时改同一文件。
- **守住范围**：只改完成当前任务必需的代码；其他问题记录到相应 Issue/PR，不顺手修。
- **需要交接时**：可使用 `session-handoff.md` 模板在对应 Issue/PR 或任务记录中写目标、已做、卡点和下一步，不强制每个会话改共享文件。

## 6. 完成的定义（Definition of Done）

一项任务只有在**全部**满足时才算完成，缺一项就继续做或明确标注未完成：

- [ ] 目标行为符合用户要求或已确认的 Issue/PR 范围
- [ ] 与改动相称的检查实际跑过；行为修复覆盖原问题，文案修改无需机械新增测试
- [ ] 在 PR 或任务记录中写清命令、结果与未验证范围；里程碑变化时再更新共享索引
- [ ] 相关文档与注释已同步；不一致就当场改文档，不留"以后补"
- [ ] 提交只包含本次变更，临时文件不入库；不为让工作树干净而删除或提交他人改动

"测试通过"不等于"真实链路可用"：真实语音、游戏或使用模型的记忆构建需要对应服务与运行环境；临时数据库可用于离线落盘测试。必须在说明里写出实际验证到哪一步。

## 7. 结束会话（End of Session）

1. 记录当前代码对应的验证结果；没有新改动或新疑点时不重复全量检查
2. 在对应 PR/任务记录说明完成范围、未验证项和后续工作
3. 发布里程碑改变时更新 `progress.md` 和可选 `feature_list.json`，日常任务无需重复登记
4. 核对待提交文件不含凭据或临时产物；已忽略的 `__pycache__` 无需专门清除

## 8. 目录速查

| 路径 | 内容 |
|---|---|
| `core/` | 事件总线、状态机、发言调度器、语音仲裁器及 `program/` 共用节目推进/转场判断 |
| `public_host/` | 本机节目中控与手动对话、服务适配、播放、VTS 和五子棋活动接线 |
| `minigames/` | 五子棋真实规则与本地算法、整场计分、结果发言和网页底座 |
| `dialogue/` | 文本链路回合编排、快脑、点名归一化、联网搜索 |
| `audio/` | 实时语音会话、TTS、ASR、音频播放与音量 |
| `settings/` | 角色配置与本地声线注册表（零依赖，占位值） |
| `memory/` | 观众与自我记忆（SQLite，构建 / 检索 / 衰减 / 落盘） |
| `games/` | Buckshot、Terraria、Kingdom Rush、Wordle、Handle 的桥接与决策 |
| `interfaces/`、`examples/` | 游戏 / 绘画 / 演唱的最小协议与离线样本 |
| `tests/` | 不依赖外部服务的自动检查，覆盖范围见 [TESTING](docs/TESTING.md) |
| `docs/` | 专题文档，索引见 [docs/README](docs/README.md) |

## 9. 状态与验证文件

| 文件 | 作用 |
|---|---|
| `feature_list.json` | 可选里程碑索引，不替代 Issue/PR 或用户需求 |
| `progress.md` | 公开发布进度与验证边界，不是所有并行任务的全局锁 |
| `session-handoff.md` | 按需复制使用的交接模板 |
| `init.sh` / `init.ps1` | 标准验证入口：编译 + 测试 + 文档体检 |
| `scripts/check_docs.py` | 内部链接、索引登记检查；篇幅与元信息为建议提示 |

## 10. 不要做的事

- 不要把规则继续堆进本文件：能写进 `docs/` 专题的，就不要写在这里（入口文件只做路由）
- 不要凭记忆改行为：先读对应模块与测试，再动手
- 不要为了绿灯访问真实云服务或设备；按第 3 节使用可复现的假服务
- 不要提交 `/tmp` 路径、个人绝对路径或含凭据的日志
