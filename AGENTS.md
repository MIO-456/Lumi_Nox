# AGENTS.md

> 这是给编码 agent 的**入口文件**，不是手册。它只回答三件事：这个项目是什么、什么绝对不能违反、下一步该读哪份文档。
> 完整文档地图见 [docs/README.md](docs/README.md)。本文件超过 200 行，或与 `docs/` 下的内容冲突时，先修文档再写代码。

## 1. 项目是什么

Lumi 与 Nox 是两位 AI 虚拟主播，共同主持一场直播。本仓库是 **open-core 快照**：公开双角色对话调度（点名路由、轮换、发言权仲裁）、实时语音链路、记忆与游戏接入；**不包含**角色人设、真实声线、Live2D 资产、观众数据与商业游戏本体。

- 运行时：Python，**3.12 为 CI 验证基线**
- 入口：`main.py`（零依赖演示）、`python -m examples.prototype_contracts`、`python -m games.<name>...`
- 依赖分层：`core/`、`settings/` **只用标准库**；`dialogue/`、`audio/`、`memory/`、`games/` 才需要 `requirements.txt`

## 2. 先跑一次（改代码前必须通过）

```bash
python main.py                          # 期望结尾：=== demo complete ===
python -m unittest discover -s tests    # 期望结尾：Ran 88 tests ... OK
python -m compileall -q .               # 无输出即通过
```

三条命令都不需要 `pip install`、`.env`、声卡或游戏。任一条失败 → 先修基线，不要在坏基线上叠加新改动。
也可以直接跑 `./init.ps1`（Windows）或 `./init.sh`（macOS / Linux）：同一组检查，外加文档体检。

## 3. 硬约束（违反任一条都必须回退）

| # | 约束 | 原因 |
|---|---|---|
| 1 | 不加入角色人设、真实 voice id、观众数据、Live2D 资产、商业游戏二进制 | open-core 的公开边界，见 [ARCHITECTURE](docs/ARCHITECTURE.md#whats-here-and-whats-not) |
| 2 | 密钥只从环境变量读取；`.env`、`*.db`、`logs/`、`recordings/` 永不提交 | 泄露不可逆；名单见 `.gitignore` |
| 3 | 测试必须离线：不联网、不开声卡、不启动游戏、不调用模型 | CI 无凭据，联网测试不可复现 |
| 4 | `core/`、`settings/` 不得 import `dialogue/`、`audio/`、`games/`、`memory/` | 保证零依赖演示与离线测试成立 |
| 5 | 只用全限定导入（`from core.event_bus import EventBus`），不在根目录新增模块 | 2026-11-12 已完成目录迁移，见 [PACKAGE_LAYOUT](docs/PACKAGE_LAYOUT.md) |
| 6 | 保持 Python 3.12 兼容，不使用仅 3.13+ 的语法或标准库 | CI 基线 |
| 7 | 一次改动只解决一个主题，不夹带无关重构或整文件格式化 | 评审成本；见 [CONTRIBUTING](CONTRIBUTING.md) |
| 8 | 声称"完成"必须附实际执行的命令与结果，未验证部分明确写出 | 防止提前宣告完成 |

## 4. 文档路由（按任务读，不要通读 `docs/`）

| 你要做的事 | 读这份 | 什么时候不用读 |
|---|---|---|
| 第一次把项目跑起来 | [GETTING_STARTED](docs/GETTING_STARTED.md) | 已能跑通 `main.py` |
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
3. 跑第 2 节的验证；基线失败就先修基线
4. 读 `feature_list.json`，选**一个** `not-started` 项
5. 按第 4 节读对应专题文档
6. 看最近改动：`git log --oneline -5`

- **一次只做一项（one feature at a time）**：同一时间只有一个 `in-progress` 项，做完再开下一项。
- **守住范围（stay in scope）**：只改完成当前项必需的代码；顺手发现的问题记进 `progress.md`，不要顺手改。
- **任务跨会话时**：把 `session-handoff.md` 补成快照，写明目标、已做、卡点、下一步。

## 6. 完成的定义（Definition of Done）

一项任务只有在**全部**满足时才算完成，缺一项就继续做或明确标注未完成：

- [ ] 目标行为已实现，且改动范围与 `feature_list.json` 里的描述一致
- [ ] 第 2 节验证实际跑过，新行为有对应用例（不是只跑旧测试）
- [ ] 命令与结果写进 `feature_list.json` 的 `evidence` 和 `progress.md`
- [ ] 相关文档与注释已同步；不一致就当场改文档，不留"以后补"
- [ ] 工作树干净，下一个会话能直接跑 `./init.sh`

"测试通过"不等于"真实链路可用"：语音、游戏、记忆落盘需要凭据与设备，必须在说明里写出实际验证到哪一步。

## 7. 结束会话（End of Session）

1. 跑第 2 节验证，记下命令与输出
2. 更新 `progress.md`：当前状态、证据、下一步、风险
3. 更新 `feature_list.json` 的 `status` 与 `evidence`
4. 工作树保持干净：不留 `__pycache__`、临时文件、未说明的未跟踪文件

## 8. 目录速查

| 路径 | 内容 |
|---|---|
| `core/` | 事件总线、状态机、发言调度器、语音仲裁器、路径与运行架构选择（零依赖） |
| `dialogue/` | 文本链路回合编排、快脑、点名归一化、联网搜索 |
| `audio/` | 实时语音会话、TTS、ASR、音频播放与音量 |
| `settings/` | 角色配置与本地声线注册表（零依赖，占位值） |
| `memory/` | 观众与自我记忆（SQLite，构建 / 检索 / 衰减 / 落盘） |
| `games/` | Buckshot、Terraria、Kingdom Rush、Wordle、Handle 的桥接与决策 |
| `interfaces/`、`examples/` | 游戏 / 绘画 / 演唱的最小协议与离线样本 |
| `tests/` | 88 个离线回归测试，覆盖范围见 [TESTING](docs/TESTING.md) |
| `docs/` | 专题文档，索引见 [docs/README](docs/README.md) |

## 9. 状态与验证文件

| 文件 | 作用 |
|---|---|
| `feature_list.json` | 范围锚点：哪些任务已交付、哪些待做、依赖关系 |
| `progress.md` | 跨会话日志：当前状态、证据、下一步、风险 |
| `session-handoff.md` | 长任务的中途快照模板；不跨会话时留空即可 |
| `init.sh` / `init.ps1` | 标准验证入口：编译 + 测试 + 文档体检 |
| `scripts/check_docs.py` | 文档体检：内部链接可达、单篇行数不超预算 |

## 10. 不要做的事

- 不要把规则继续堆进本文件：能写进 `docs/` 专题的，就不要写在这里（入口文件只做路由）
- 不要凭记忆改行为：先读对应模块与测试，再动手
- 不要为了让测试通过而放宽离线约束（mock 掉网络/硬件是允许的，真连出去不是）
- 不要提交 `/tmp` 路径、个人绝对路径或含凭据的日志
