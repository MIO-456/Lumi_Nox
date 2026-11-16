# Lumi_Nox

**中文 | [English](README.en.md)** · [快速开始](docs/GETTING_STARTED.md) · [如何贡献](CONTRIBUTING.md) · [可以一起做什么](docs/CONTRIBUTION_TASKS.md)

> 两位 AI 主播，共同主持一场直播。这里分享让她们轮流接话、回应观众和参与游戏的部分工程实现。

[![CI](https://github.com/MIO-456/Lumi_Nox/actions/workflows/ci.yml/badge.svg)](https://github.com/MIO-456/Lumi_Nox/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/MIO-456/Lumi_Nox)](https://github.com/MIO-456/Lumi_Nox/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![Lumi 与 Nox 同台直播](docs/assets/hero.png)

**▶ 观看实际直播与视频：[Lumi和Nox-AI搭档](https://space.bilibili.com/544387533)**

## 这是什么项目

Lumi 和 Nox 是共同主持直播的两位 AI 虚拟主播。项目围绕双角色对话、观众互动、语音、记忆和游戏展开：观众点名时由谁回应，两人怎样接话，游戏决策怎样与说话配合，都是这里关心的工程问题。

这个仓库是项目的 **open-core（核心源码公开）**：收录从实际直播系统整理的模块和工程资料，按阶段发布。通用公开宿主提供本机配置与节目中控：使用自己的模型、声音和角色，运行开场→聊天→五子棋→聊天→收尾，也可选择手动对话与可选的 VTube Studio 嘴型。首页画面展示实际节目；公开宿主不包含 Lumi／Nox 的私人角色资产、平台账号或完整生产环境。

## 先跑一次：无需密钥的网页演示

准备 Python 和 Git。**Python 3.12 是已验证的基线，也是当前 CI 使用的版本。**

```bash
git clone https://github.com/MIO-456/Lumi_Nox.git
cd Lumi_Nox
python -m public_host
```

浏览器打开 **http://127.0.0.1:8765**，将“运行模式”改为“离线演示”，将“运行方式”从默认的“按节目单运行”改为“手动对话”，保存本机配置，再点“开始会话”并输入消息。它使用真实的调度与取消流程，返回示例文本，**不调用模型、不播放声音、不连接 VTS**；事件会标记为模拟结果。首次默认选中“真实服务”，需主动切换为演示。

这条路径只用 Python 标准库，**不需要安装依赖，也不需要 API Key、模型、声卡或游戏**。macOS / Linux 按需将 `python` 换成 `python3`。结束后停止会话，在终端按 Ctrl+C 退出服务。下载与检查步骤见[快速开始](docs/GETTING_STARTED.md)。

需要真实语音时，安装 `python -m pip install -r requirements-host.txt`，在页面填写自己的聊天与 TTS 基础地址、模型、密钥和角色音色。当前适配器支持 `/chat/completions` 文本响应与 `/audio/speech` 的完整 16 位 PCM WAV；各服务协议和音色权限仍需核对。详见[公开宿主使用指南](docs/PUBLIC_HOST.md)；可选皮套从 [VTS 与 Hiyori 示例](docs/VTS_EXAMPLE.md)开始。真实云服务、声卡和 Hiyori 效果需在使用者环境中实测。

想跑完整节目流程，可先安装 `python -m pip install aiohttp`，启用两个名字不同的角色，选择“按节目单运行”并保存。离线演示中的五子棋仍会真实落子、计分和结算，落子由本地规则算法决定，不调用语言模型。具体操作、端口和验证边界见[公开中控与五子棋](docs/PUBLIC_DIRECTOR.md)。

### 原有终端演示仍可使用

运行 `python main.py`，几秒后会看到 `=== demo complete ===`，无需安装依赖或填写密钥。

- **真实运行的部分**：事件总线、状态机、发言调度器和语音输出仲裁器；可以观察点名路由、轮换和占用发言权时的排队。
- **演示占位的部分**：`DemoBrain` 返回固定台词；`demo_speak()` 只打印文字，**终端无声**。演示会自动跑完，不接收你手动输入的弹幕，也不连接直播间。

## 项目特点与当前公开模块

| 关注的问题 | 公开实现 | 从哪里看 |
|---|---|---|
| 怎样把自己的角色实际接起来 | 本机配置页、兼容模型与语音接口、播放收尾、取消与可选 VTS 嘴型 | [public_host/](public_host/)、[使用指南](docs/PUBLIC_HOST.md) |
| 怎样运行一场有游戏的节目 | 节目单、计时、准备/激活/停止与整场结算；首个活动为网页五子棋 | [core/program/](core/program/)、[公开中控指南](docs/PUBLIC_DIRECTOR.md) |
| 两位角色怎样轮流接话 | 点名路由、观众消息队列、轮换与跨角色历史镜像 | [core/speaker_scheduler.py](core/speaker_scheduler.py)、[dialogue/conversation.py](dialogue/conversation.py) |
| 怎样避免同时占用语音输出 | 发声前申请、完成后释放，支持排队、丢弃和打断策略 | [core/speech_output_arbiter.py](core/speech_output_arbiter.py) |
| 模块怎样共享节目状态 | 进程内事件总线与全局状态机 | [core/event_bus.py](core/event_bus.py)、[core/state_machine.py](core/state_machine.py) |
| 怎样接入模型与语音 | 文本快脑、双角色实时语音会话、流式 TTS 与 ASR | [dialogue/fast_brain.py](dialogue/fast_brain.py)、[audio/realtime_chat.py](audio/realtime_chat.py)、[audio/lumi_tts.py](audio/lumi_tts.py)、[audio/lumi_asr.py](audio/lumi_asr.py) |
| 怎样独立接入 Qwen 合成与播放 | 整句 PCM、播放结果、取消与分角色音量 | [语音模块指南](docs/VOICE_MODULES.md)、[audio/qwen3_tts.py](audio/qwen3_tts.py) |
| 怎样延续观众与角色的记忆 | SQLite 存储、事实提取、确定性事实与时间衰减 | [memory/](memory/) |
| 怎样让 AI 参与游戏 | 恶魔轮盘、泰拉瑞亚、王国保卫战、Wordle、汉兜的桥接与决策代码 | [游戏入口与说明](games/README.md) |

游戏模块包含泰拉瑞亚的 A* 寻路、目标规划与 tModLoader 模组，王国保卫战的 Lua 桥接与逐波策略，以及在独立进程中执行搜索的词语游戏求解器。各模块的使用条件不同，具体见对应指南。

公开宿主已连接基础对话、节目流程与五子棋；实时语音、ASR、记忆及其他游戏模块仍需按各自指南准备环境并接线。B 站弹幕与 OBS 自动控制尚未接入，推流由使用者操作。

## 可以一起做什么

我们优先欢迎能让节目更有意思的技术贡献：**接入一款新游戏、尝试绘画的新表现形式、探索角色点唱**。这些是待讨论的方向；点唱可以从已有角色曲库入手，也可以探索按请求制作新的演唱版本，具体路线尚待讨论。先展示独立原型、效果和可行性，再确定接入范围。[最小接口、样本与离线演示](docs/PROTOTYPE_INTERFACES.md)已提供；正式直播接线和实机验收由维护者负责。

也欢迎从小处开始：给终端演示增加可选的手动输入，复现一个问题，在自己的系统上跑一次上手指南，或为公开模块写一篇教程。不需要先加入团队。

- [可以一起做什么](docs/CONTRIBUTION_TASKS.md)：查看方向、可独立开始的任务和交付要求。
- [如何贡献](CONTRIBUTING.md)：了解提出建议、提交问题与 PR 的流程。
- [Issues](https://github.com/MIO-456/Lumi_Nox/issues)：查找已有讨论，或带着复现步骤、原型和具体想法发起交流。

## 阅读路线与技术资料

先试[公开宿主](docs/PUBLIC_HOST.md)，或运行 [main.py](main.py) 阅读最小调度示例，再按[快速开始中的读代码路线](docs/GETTING_STARTED.md#5-下一步读哪些代码)深入。子系统资料包括：

- [通用公开宿主：配置、运行与验证](docs/PUBLIC_HOST.md)
- [公开中控与真实五子棋](docs/PUBLIC_DIRECTOR.md)
- [VTS 与 Hiyori 示例适配](docs/VTS_EXAMPLE.md)
- [整体架构与数据流](docs/ARCHITECTURE.md)
- [本次源码更新与验证范围](docs/releases/2026-11-12-source-refresh.md)
- [独立语音模块与配置](docs/VOICE_MODULES.md)
- [游戏目录与入口](games/README.md)
- [泰拉瑞亚模组安装与后台控制说明](games/terraria/mod/README.md)
- [泰拉瑞亚行为树设计](docs/games/terraria-behavior-tree.md)
- [王国保卫战逆向工程记录](docs/games/kingdom-rush-reverse-engineering.md)
- [发布记录](https://github.com/MIO-456/Lumi_Nox/releases)

```text
main.py                   # 标准库终端演示
public_host/              # 通用中控、节目接线、服务适配、播放、VTS 与本机网页
core/                     # 事件、状态、节目调度、角色调度和语音仲裁
minigames/                # 五子棋规则、自动落子、整场结算与网页底座
dialogue/                 # 对话编排、模型调用、观众名称和联网搜索
audio/                    # ASR、TTS、实时语音与播放处理
settings/                 # 示例角色配置与音色适配
interfaces/               # 游戏规则、画板消息和歌曲资产契约
examples/                 # 无需密钥的原型数据与临时音轨样本
memory/                   # 记忆存储、提取与衰减
games/                    # 游戏桥接与决策
tests/                    # 公开核心与宿主检查（外部服务由测试替身替代）
docs/                     # 上手、架构与模块指南
```

从旧版本更新的调用者，见[目录迁移与导入对照](docs/PACKAGE_LAYOUT.md)。

## 检查运行结果

在仓库根目录运行；macOS / Linux 按需使用 `python3`：

```bash
python main.py
python -m unittest discover -s tests -v
python -m compileall -q .
```

演示应正常结束，测试应显示 `OK`，编译检查成功时通常没有输出。GitHub Actions 配置为在每次 push 和 PR 中使用 Ubuntu、Windows / Python 3.12 执行编译与 unittest；其中 `test_zero_setup_demo` 会调用 `main.py`，宿主 HTTP 测试需要允许本机回环连接。这些检查验证公开核心与宿主的模拟链路，不代表真实语音、皮套、游戏或整场直播已经验收。

## 公开范围与许可

代码沿用 [MIT 许可证](LICENSE)。通用启动器、配置页、节目中控、共用节目调度、五子棋、对话调度和基础 VTS 嘴型适配已公开。`core/program/` 同时由正式导演候选调用；正式环境的实机验证和推广尚未完成，不能据此声称已实播。角色的真实人设、世界观、角色 IP、声纹与私有音色 ID，以及观众数据库、运行日志和运营资料不在公开范围。Live2D 模型资产、私人动作与表情库、视觉与绘画运行系统、私人导演的设备/平台接线仍未包含；商业游戏本体需自行准备。Hiyori 示例仅提供官方获取与适配说明，模型不随仓库分发，使用须遵守其独立许可。

公开宿主使用独立的 `.local-host/` 保存本机配置与 VTS 授权，不自动读取 `.env` 或私人直播配置。较早的模块仍使用 `settings/voice_config.py` 的占位配置和 `settings/voice_registry.py` 的音色变量，见 [.env.example](.env.example)。请使用自己的模型、角色与获准使用的声音素材，真实密钥仅保存在本地。

新增公开模块会先核对隐私、许可与依赖边界；贡献的评审与采用流程见[贡献指南](CONTRIBUTING.md)。
