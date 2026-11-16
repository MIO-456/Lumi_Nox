# 公开发布进度与历史记录

> 本文件记录公开发布、重要维护变化及对应证据，不要求每位贡献者在每次会话结束时更新。
> 任务范围、认领与讨论放在对应 Issue / PR；`feature_list.json` 是可选索引，`session-handoff.md` 是按需交接模板。发生矛盾时核对已确认需求、实际提交和验证结果，不用时间戳代替判断。

## 当前状态（2026-11-16）

- 公开中控提供可编辑的节目单、开始、下一段、进入收尾、立即停止、当前阶段与错误记录。开场、自动聊天、真实网页五子棋、回聊天和结束已接入；用法见 [PUBLIC_DIRECTOR](docs/PUBLIC_DIRECTOR.md)。原手动对话与服务/角色配置保留。
- `core/program/` 与正式导演候选共用实际计时、推进、转场阶段和回执检查；正式候选尚需独立实机与推广验证，不能把本次公开验证等同于正式版本已上线。
- 本地浏览器已跑完整示例节目：实际棋盘落子、两方各 1 分、整场结算后回聊天与结束，最终游戏和网页底座关闭。模型和声音使用明确的无声 demo，不是实际云模型、音频设备或 VTS 验收。
- CI 安装 aiohttp 并在 Ubuntu/Windows 运行统一检查，使本地真实五子棋接线测试也执行。其他商业游戏、绘画/演唱、麦克风 ASR、平台弹幕与 OBS 自动控制尚未接入这个公开中控。实际推流由使用者控制。
- 本轮最终本地检查：Windows / Python 3.12 的 `init.ps1` 完成编译、27 份文档检查和 223 项测试（222 通过、1 项因本机缺 Bash 跳过）。真实五子棋的自动结束、跳过、提前收尾、立即停止均执行，验证同总线、唯一状态机及准确活动 ID；另有 WebSocket 断线清理回归。JS 语法和公开依赖/隐私/目录边界审计通过；远端双平台结果以本次提交的 Actions 为准。

## 2026-11-16：PR #1 文档与验证入口维护

- 公开宿主已在 `68c5a36` 发布：本机配置、自定义单/双角色、聊天/TTS/播放/取消与可选 VTS 嘴型；使用方式见 [PUBLIC_HOST](docs/PUBLIC_HOST.md)。真实服务、声卡和 Hiyori 效果仍需按指南实测，游戏/绘画/演唱活动管理尚未统一接入。
- Ubuntu 与 Windows 的 CI 矩阵已存在，不再列为待新建任务。本轮将两端接到统一验证脚本；是否通过以对应提交的 Actions 为准。
- [PR #1](https://github.com/MIO-456/Lumi_Nox/pull/1) 提供文档分层、术语表、验证入口和交接模板。本轮保留这些贡献，更新其旧宿主说明与协作规则：允许本机假服务测试、自定义角色及独立本机配置；多位贡献者可按 Issue / PR 并行工作。
- 本轮维护者已在 Windows / Python 3.12.10 实际运行 `init.ps1`：编译、26 份文档检查与 167 项测试完成（166 通过、1 项因本机无 Git Bash 跳过）；`python main.py` 到达 `demo complete`，公开边界审计通过。新增回归覆盖解释器失效、空格路径、编码恢复、失败后停止及文档检查误报。Bash 与双平台最终结果以 [PR #1](https://github.com/MIO-456/Lumi_Nox/pull/1) 的最新 CI 为准；此记录不预先宣称合并或远端通过。

## 接下来如何参与

从[贡献方向](docs/CONTRIBUTION_TASKS.md)和已有 Issues / PR 选择或提出具体工作。较大活动接线先确定范围；小修复可直接提 PR。无需先完成终端交互或定期文档审计，也无需从 `feature_list.json` 自动认领下一项。

维护者在发布或重要状态变化时更新这里；单次修复的命令、结果和限制留在对应 PR 即可。跨人或跨会话的长任务可按需使用 [交接模板](session-handoff.md)。

## 历史记录：PR #1 初稿（723f8bc，2026-11-14）

以下保留作者当时的工作与验证记录。旧测试数量、平台环境、任务排序及文档策略仅描述该版本，当前流程以上文和 [AGENTS](AGENTS.md) 为准。

### 当时状态

**Last Updated（最后更新）**：2026-11-14 21:45（Asia/Shanghai）
**Current Objective（活动任务）**：无 —— `doc-01` 已完成，等待认领下一项
**Recommended Next Step（建议下一步）**：按优先级挑 `doc-02` / `contrib-01` / `ci-01` 之一
**基线**：88 项离线测试全绿；文档体检通过；`./init.ps1` 与 `bash ./init.sh` 均为 exit 0

### 当时已完成（What's Done）

- [x] `verify-01` 离线基线可重复：演示、编译、88 项测试在 Windows / Python 3.14.7 上跑通
- [x] `doc-01` 分层文档体系落地：
  - `AGENTS.md` 入口路由：概览、8 条硬约束、文档路由表、启动工作流、完成的定义、结束流程
  - `docs/README.md` 文档地图 + 元信息规范 + 写作规则 + 审计约定
  - 新增专题：`DEVELOPMENT.md`、`TESTING.md`、`CONVENTIONS.md`、`GLOSSARY.md`
  - 已有 8 篇专题补「何时读 / 何时不用读 / 复核」元信息头
  - `scripts/check_docs.py`：链接可达、行数预算、索引登记三项硬检查
  - `init.sh` / `init.ps1`：编译 + 文档 + 测试的统一验证入口
  - `session-handoff.md`：跨会话快照模板
  - CI 增加 `python scripts/check_docs.py` 步骤

### 初稿发现并修正的文档漂移

| 问题 | 处理 |
|---|---|
| `CONTRIBUTING.md` 写"当前测试集为 7 项"，实际 88 项 / 10 个文件 | 改为 88 项 |
| 技能安装在各仓库留下 6.9MB `npm-cache/` | 已删除 |
| `.agents/`、`skills-lock.json` 是本机工具产物 | 加入 `.gitignore` |

### 当时建议的后续方向（已由当前状态更新）

1. `doc-02` 文档季度审计：删除失效条目、合并重复主题（下次审计建议 2027-02）
2. `contrib-01` 交互式终端演示：见 `docs/CONTRIBUTION_TASKS.md`「可独立开始」一节
3. `ci-01` Windows CI 并行任务：只改 `.github/workflows/ci.yml`

### 初稿风险与未验证项

- **未验证**：真实语音链路、游戏桥、记忆落盘——公开库没有凭据、设备与游戏本体，离线测试不覆盖。
- **未验证**：CI 新加的 `check_docs.py` 步骤只在本地 PowerShell 5.1 与 WSL bash 上跑过，需要一次真实 PR 观察 Ubuntu 上的行为。
- **平台差异**：本机只有 Windows PowerShell 5.1（没有 `pwsh`），`init.ps1` 按 5.1 的退出码语义编写；统一到 `pwsh` 后可简化。

### 初稿决策记录

- **入口文件不做百科全书**：`AGENTS.md` 只做路由，专题内容放 `docs/`。理由是「中间迷失」与优先级混淆——硬约束与建议混排会让两者都失效。
- **元信息用三行引用块**：`何时读 / 何时不用读 / 复核`，给出失效信号，避免文档只增不减。
- **状态文件放在仓库根**：`feature_list.json` / `progress.md` 是 agent 的启动路径，藏进子目录等于不存在。
- **进度日志与交接快照分开**：`progress.md` 追加式记录，`session-handoff.md` 是某一时刻快照，避免两者互相覆盖。

### 初稿改动的文件

- 新增：`AGENTS.md`、`docs/README.md`、`docs/DEVELOPMENT.md`、`docs/TESTING.md`、`docs/CONVENTIONS.md`、`docs/GLOSSARY.md`、`scripts/check_docs.py`、`init.sh`、`init.ps1`、`feature_list.json`、`progress.md`、`session-handoff.md`
- 修改：`docs/ARCHITECTURE.md`、`docs/PACKAGE_LAYOUT.md`、`docs/GETTING_STARTED.md`、`docs/VOICE_MODULES.md`、`docs/PROTOTYPE_INTERFACES.md`、`docs/CONTRIBUTION_TASKS.md`、`docs/games/*.md`（均只加元信息头）、`CONTRIBUTING.md`（测试数）、`.gitignore`、`.github/workflows/ci.yml`

### 初稿验证证据

```text
python main.py                          -> === demo complete ===
python -m compileall -q .               -> exit 0
python -m unittest discover -s tests    -> Ran 88 tests ... OK
python scripts/check_docs.py            -> docs ok: 24 files, links resolve, budgets hold.
./init.ps1                              -> == all checks passed ==  (exit 0)
bash ./init.sh                          -> == all checks passed ==  (exit 0)
node .agents/skills/harness-creator/scripts/validate-harness.mjs --target .
                                        -> Overall: 100/100（instructions/state/verification/scope/lifecycle 均满分）
```

初稿把任务索引和本文件当作每次会话的入口；本轮调整为 Issues / PR 跟踪任务、发布时更新进度，交接模板按需使用。原作者提供的验证入口和专题文档继续保留。
