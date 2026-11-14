# 开发环境与运行模式

> **何时读**：要安装依赖、配置 `.env`、接真实模型 / 语音 / 游戏，或排查"为什么零依赖能跑、真实模块不能跑"。
> **何时不用读**：只需要跑 `python main.py` 和测试——那是零依赖路径，见 [GETTING_STARTED](GETTING_STARTED.md)。
> **复核**：`requirements.txt`、`.env.example` 或各模块启动方式变化时。

## 两条路径，不要混用

| 路径 | 需要什么 | 能做什么 | 不能做什么 |
|---|---|---|---|
| **零依赖**（默认） | 只要 Python | `main.py` 演示、88 项离线测试、示例协议 | 出声、连模型、连游戏 |
| **完整**（可选） | `requirements.txt` + 自己的凭据 / 设备 / 游戏 | 实时语音、TTS、ASR、快脑、记忆、游戏桥 | 完整直播——公开库没有启动器与导演中控 |

公开库是 open-core 快照：没有的人设、声线、资产与游戏本体，**不要试图在仓库里补齐**，用占位值或自备资源。

## Python 版本

- **3.12 是 CI 基线**，提交的代码必须能在 3.12 上运行。
- 实测 2026-11：Python 3.14.7 上 88 项测试同样通过，但这不是承诺，只是当前观察。
- 不要使用仅 3.13+ 才有的语法或标准库特性。

## 安装依赖（可选）

```bash
python -m pip install -r requirements.txt
```

几个注意点：

- `PyAudioWPatch` 是 **Windows** 的 WASAPI 音频 I/O；其他平台的音频输出需要额外移植，不是改个 import 就能跑。
- `playwright` 只服务 Wordle bot，还需要 `playwright install` 下载浏览器。
- 依赖装完不代表链路可用：还需要对应服务的凭据、兼容的音色 / 模型，以及真实设备。

## 环境变量

全部凭据从环境变量读取，模板见 [.env.example](../.env.example)；`.env` 已在 `.gitignore` 中，**永不提交**。

| 分组 | 变量 | 用途 |
|---|---|---|
| 快脑 / 文本模型 | `ARK_API_KEY_FAST`、`DASHSCOPE_API_KEY` | 快脑与 DashScope 系服务 |
| 实时语音 | `VOLC_DIALOG_APP_ID`、`VOLC_DIALOG_ACCESS_KEY` | doubao SC2.0 双人语音会话 |
| 联网搜索 | `VOLC_WEBSEARCH_API_KEY`、`VOLC_WEBSEARCH_BOT_ID` | `dialogue/web_search.py` |
| 本地声线 | `CHARACTER_A_VOICE_ID`、`CHARACTER_A_VOICE_MODEL`、`CHARACTER_B_*`、`TTS_MODEL`、`TTS_VOICE_ID` | `settings/voice_registry.py` 解析克隆音色，缺失时安全回退 |
| 独立 Qwen 音频 | `QWEN_AUDIO_TTS_WEBSOCKET_URL` | `audio/qwen_audio_tts.py` |

`settings/voice_registry.py` 只在用户提供声线时使用，读不到就回退到占位配置——这是有意的降级设计，不要改成抛异常。

## 运行模式

| 目标 | 命令 | 前置条件 |
|---|---|---|
| 无声演示（真实调度 + 占位脑与语音） | `python main.py` | 无 |
| 离线协议样本 | `python -m examples.prototype_contracts` | 无 |
| 全部离线检查 | `python -m unittest discover -s tests` | 无 |
| 编译检查 | `python -m compileall -q .` | 无 |
| Buckshot 决策 / 桥 | `python -m games.buckshot.bot` | 游戏端桥 |
| Terraria 走路演示 | `python -m games.terraria.bot --demo walk` | 无（演示模式） |
| Kingdom Rush 干跑 | `python -m games.kingdom_rush.ai --dry-run` | 无（dry-run） |
| Wordle 无模型模式 | `python -m games.word_games.wordle.bot --no-llm` | 无 |
| Handle 桥 | `python -m games.word_games.handle.bridge` | 游戏端桥 |

游戏细节见 [games/README](../games/README.md)；独立语音模块见 [VOICE_MODULES](VOICE_MODULES.md)。

## 资源定位

`core.paths.PROJECT_ROOT` 指向仓库根目录。音量配置 `role_volumes.json`、快脑读取的相对提示词路径、字幕与素材都按仓库根解析——**不要**假设当前工作目录，也不要把资源搬进 `core/`。这些运行资源由使用者自备，仓库里没有。

## 常见故障

| 现象 | 先检查 |
|---|---|
| `ModuleNotFoundError: No module named 'openai'` | 走的是完整路径但没装依赖；零依赖路径不应触发 |
| `ModuleNotFoundError: core...` | 是否在仓库根目录执行，或用 `python -m` 而不是直接跑子目录文件 |
| 有输出但没声音 | 预期行为：公开演示用 `demo_speak()` 打印而不发声 |
| 实时语音连不上 | 凭据是否过期、网络是否可达；公开库不提供代用服务 |
| 音频设备报错 | `PyAudioWPatch` 是否安装、是否 Windows、设备是否被占用 |
| 测试连到了网络 / 声卡 | 违反离线约束，见 [TESTING](TESTING.md) |

## 不要做的事

- 不要把真实凭据写进代码、测试或文档示例。
- 不要把"演示能跑"当成"语音链路验收"。
- 不要在 `core/`、`settings/` 里引入第三方依赖——零依赖演示与离线测试都依赖这条边界。
