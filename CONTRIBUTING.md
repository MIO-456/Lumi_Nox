# 参与 Lumi & Nox

欢迎一起改进这个双 AI 主播项目。我们优先欢迎代码、游戏接入和节目功能原型，也欢迎测试复现、教程与演示。**无需先申请加入团队，也不要求入群。** 中文或英文的 Issue、PR 都可以。

先看[可以一起做什么](docs/CONTRIBUTION_TASKS.md)，选择一个范围明确、能够独立验证的小任务。需要定位开发资料时，可从[文档地图](docs/README.md)进入。

## 从哪里开始

- **修复小问题、补测试或改文档**：可以直接提交 PR，说明问题、改法和验证结果。
- **新游戏、新节目或较大改动**：先开 [功能建议 Issue](https://github.com/MIO-456/Lumi_Nox/issues/new?template=feature_request.yml)，说明价值、第一版交付和需要的接口，和维护者确定范围后再实施。
- **遇到问题**：提交 [Bug 报告](https://github.com/MIO-456/Lumi_Nox/issues/new?template=bug_report.yml)，附版本、环境、复现步骤以及预期和实际结果。
- **想做教程或帮助测试**：可以分享公开模块的运行记录、教程或独立演示链接；先说清对应版本和适用环境。

开始前搜索一下[已有 Issues](https://github.com/MIO-456/Lumi_Nox/issues)。已有相同任务时，在原 Issue 留下打算做的部分，便于避免重复工作；不适合表单的话，也可以开空白 Issue。

任务范围、认领、讨论和验证证据放在对应 Issue / PR，多位贡献者可以并行开展不同工作。`feature_list.json` 是可选里程碑索引，`progress.md` 是公开发布与历史记录；不要求每个 PR 更新它们，也不要求只能从清单中选任务。长任务交接可按需复制 `session-handoff.md` 模板到自己的 Issue / PR。

## 本地运行和验证

Fork 仓库后 clone 自己的副本，在单独分支上修改。建议使用 Python 3.12，与当前 CI 一致；下面的演示和测试只使用标准库，不需要 API 密钥、音频设备、模型或安装 `requirements.txt`。

在仓库根目录执行：

```bash
python main.py
python -m unittest discover -s tests -v
python -m compileall -q .
```

`main.py` 会自动跑完一段终端演示，以 `demo complete` 结束。它用占位脑和占位语音驱动真实调度器与语音仲裁器，不会生成真实语音或连接直播。宿主可用 `python -m public_host` 启动，在页面选择离线演示后手动输入消息，同样无需依赖或密钥。

自动检查不访问真实外部服务、声卡、VTS 或游戏；HTTP 集成测试会创建本机 loopback 假服务。GitHub Actions 在 Ubuntu / Windows 的 Python 3.12 上分别运行 `bash ./init.sh` / `./init.ps1`，统一执行编译、文档与测试检查。测试数量和通过情况以对应提交的运行结果为准；分组与环境限制见[测试指南](docs/TESTING.md)。

具体上手步骤见[快速开始](docs/GETTING_STARTED.md)。完整语音或游戏模块可能需要额外依赖、自己的服务凭据与游戏环境，分别参阅 [README](README.md)、[游戏说明](games/README.md)和对应模块文档。

## 提交一个便于审阅的 PR

1. 一次解决一个问题，保留现有结构，避免混入无关重构或整文件格式调整。
2. 说明改动前后的行为；有关联 Issue 就附链接，小修复不必为了提交 PR 另开 Issue。
3. 按改动范围验证。代码改动补充必要回归并完成相关检查；仅改文档时运行 `python scripts/check_docs.py`，同时人工核对命令与描述。无关的基线失败或环境限制应如实说明，不要求顺手改完其他问题。
4. 写出实际运行的命令、环境和结果。无法验证的部分明确说明，不把模拟结果写成完整直播验收。

涉及外部模型、游戏或设备时，尽量提供不含凭据的小样例、脱敏日志和可独立运行的演示。维护者会根据实际范围评审；较大的功能可以先提交 Draft PR 讨论实现。

## 公开贡献与正式直播的边界

本仓库公开通用宿主、基础对话接线、配置页、可选 VTS 嘴型，以及部分语音、记忆、游戏桥接和架构资料。它不等于正式直播完整中控。私人角色人设与世界观、声线、Live2D 模型/动作资产、私有记忆及运营数据不随仓库公开；正式导演、视觉、绘画和演唱运行系统也不包含。

新功能可以先用占位角色、测试声线和公开样例完成独立原型。若任务需要尚未公开的直播接口，先与维护者确定最小接口、脱敏样例和验收方式，再推进依赖它的实现；不要求贡献者取得私有仓库或正式角色资产。

已有的[最小接口与离线样本](docs/PROTOTYPE_INTERFACES.md)可供游戏、绘画和演唱原型参考。新模块的解说可接到[公开宿主](docs/PUBLIC_HOST.md)的 `Session.submit_speech()`；但活动注册、启停、完成和回聊天尚未统一接入，不能把接口样本当成完整插件系统。

提交前移除 API 密钥、Cookie、个人路径、私人聊天或观众记录等敏感内容。第三方代码和素材请注明来源及许可，只提交可以公开分享的内容。

## 贡献如何保留与采用

- 公开库专用的文档、演示和社区文件在公开库维护，后续从主工程同步时应保留。
- 涉及共用代码的修复，由维护者评估是否回带主工程；后续同步前核对公开贡献，避免被覆盖。提交公开 PR 不等于自动改动正式直播。
- 被采用的贡献会保留作者署名，并在适合的更新说明中链接相关 PR、教程或作品。

是否合并、何时发布，以及是否进入直播、制作介绍视频或获得曝光，取决于评审、效果与节目安排，不作保证。技术贡献、测试和教程都欢迎，参与不与直播采用或曝光绑定。

## English quick guide

Code, game integrations and show prototypes are our main focus; reproducible bug reports, tests, tutorials and demos are welcome too. No team application or group membership is required. See the [contribution tasks](docs/CONTRIBUTION_TASKS.md) for starting points.

Small fixes can go straight to a PR. Discuss larger features in an Issue first, including the smallest useful deliverable and any missing interfaces. Issues and PRs track work; the milestone index and release log are optional, and different contributors may work in parallel. Use Python 3.12 and the commands above for checks without credentials or hardware; HTTP tests require local loopback networking. State what you actually tested and what remains unverified.

The public host provides basic chat, speech, local configuration and optional VTS mouth movement; the full production director and activity integrations are not included. Use generic characters and your own permitted assets; never submit secrets or private audience data. Maintainers assess shared fixes for the private runtime and preserve public-only docs, demos and community files during later syncs. Accepted contributions retain attribution and relevant links; adoption in the livestream or promotional coverage is not guaranteed.
