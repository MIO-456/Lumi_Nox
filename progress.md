# 进度与交接

> 跨会话的唯一状态记录。每次结束会话前更新这里，再更新 `feature_list.json`；两者不一致时以本文件的时间戳为准。
> 需要中途快照时，另建 `session-handoff.md`（模板已就位），不要在这里堆阶段性细节。

## Current State（当前状态）

**Last Updated（最后更新）**：2026-11-14 21:45（Asia/Shanghai）
**Current Objective（活动任务）**：无 —— `doc-01` 已完成，等待认领下一项
**Recommended Next Step（建议下一步）**：按优先级挑 `doc-02` / `contrib-01` / `ci-01` 之一
**基线**：88 项离线测试全绿；文档体检通过；`./init.ps1` 与 `bash ./init.sh` 均为 exit 0

## 已完成（What's Done）

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

## 本轮发现并修正的文档漂移

| 问题 | 处理 |
|---|---|
| `CONTRIBUTING.md` 写"当前测试集为 7 项"，实际 88 项 / 10 个文件 | 改为 88 项 |
| 技能安装在各仓库留下 6.9MB `npm-cache/` | 已删除 |
| `.agents/`、`skills-lock.json` 是本机工具产物 | 加入 `.gitignore` |

## 下一步（Next）

1. `doc-02` 文档季度审计：删除失效条目、合并重复主题（下次审计建议 2027-02）
2. `contrib-01` 交互式终端演示：见 `docs/CONTRIBUTION_TASKS.md`「可独立开始」一节
3. `ci-01` Windows CI 并行任务：只改 `.github/workflows/ci.yml`

## Blockers / Risks（风险与未验证项）

- **未验证**：真实语音链路、游戏桥、记忆落盘——公开库没有凭据、设备与游戏本体，离线测试不覆盖。
- **未验证**：CI 新加的 `check_docs.py` 步骤只在本地 PowerShell 5.1 与 WSL bash 上跑过，需要一次真实 PR 观察 Ubuntu 上的行为。
- **平台差异**：本机只有 Windows PowerShell 5.1（没有 `pwsh`），`init.ps1` 按 5.1 的退出码语义编写；统一到 `pwsh` 后可简化。

## 决策记录（Decisions）

- **入口文件不做百科全书**：`AGENTS.md` 只做路由，专题内容放 `docs/`。理由是「中间迷失」与优先级混淆——硬约束与建议混排会让两者都失效。
- **元信息用三行引用块**：`何时读 / 何时不用读 / 复核`，给出失效信号，避免文档只增不减。
- **状态文件放在仓库根**：`feature_list.json` / `progress.md` 是 agent 的启动路径，藏进子目录等于不存在。
- **进度日志与交接快照分开**：`progress.md` 追加式记录，`session-handoff.md` 是某一时刻快照，避免两者互相覆盖。

## 本次改动的文件（Files）

- 新增：`AGENTS.md`、`docs/README.md`、`docs/DEVELOPMENT.md`、`docs/TESTING.md`、`docs/CONVENTIONS.md`、`docs/GLOSSARY.md`、`scripts/check_docs.py`、`init.sh`、`init.ps1`、`feature_list.json`、`progress.md`、`session-handoff.md`
- 修改：`docs/ARCHITECTURE.md`、`docs/PACKAGE_LAYOUT.md`、`docs/GETTING_STARTED.md`、`docs/VOICE_MODULES.md`、`docs/PROTOTYPE_INTERFACES.md`、`docs/CONTRIBUTION_TASKS.md`、`docs/games/*.md`（均只加元信息头）、`CONTRIBUTING.md`（测试数）、`.gitignore`、`.github/workflows/ci.yml`

## 验证证据（Verification Evidence）

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

## 下次会话怎么接

1. 读 `AGENTS.md`
2. 跑 `./init.ps1`（或 `./init.sh`）
3. 从 `feature_list.json` 挑一个 `not-started` 项，在 Issue 或本条记录里声明后再动手
4. 结束时回到本文件顶部更新「当前状态」与「下一步」
