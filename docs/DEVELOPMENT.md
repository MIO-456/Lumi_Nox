# 开发环境与运行模式

> **何时读**：要选择运行入口、安装依赖、配置模型 / 语音 / 游戏，或排查演示与实际接入的差别。
> **何时不用读**：只需要体验无声演示，先看 [GETTING_STARTED](GETTING_STARTED.md)。
> **复核**：宿主配置、依赖文件、环境变量或模块启动方式变化时。

## 按目标选择运行路径

| 路径 | 需要什么 | 能做什么 | 不能做什么 |
|---|---|---|---|
| **无声演示与自动检查** | Python 标准库；HTTP 测试需允许本机回环连接 | `main.py`、宿主离线演示、协议样本与测试 | 不连接真实服务、声卡、VTS 或游戏 |
| **通用公开宿主** | `requirements-host.txt` + 自己的聊天/TTS服务、音色与输出设备 | 本机网页配置、1–2角色对话、播放和取消；可选 VTS 嘴型 | 尚未接入 ASR、直播间、游戏/绘画/唱歌活动管理 |
| **独立模块** | 按模块选择 `requirements.txt` 依赖及服务 / 设备 / 游戏 | 实时语音、TTS、ASR、快脑、记忆、游戏桥的独立接入 | 安装依赖不会自动连接所有模块；私人正式直播完整中控不包含 |

公开库是 open-core 快照：通用启动、配置与基础对话接线已经公开，Lumi/Nox 的私人设定、声线和模型资产仍不分发。使用者可自定义角色并自备获准使用的模型与声音；商业游戏本体也需自备。

## Python 版本

- **3.12 是 CI 基线**，提交的代码必须能在 3.12 上运行。
- PR 初稿作者在其他版本上的历史验证见 [progress.md](../progress.md)；它不替代当前提交的测试结果，也不构成其他 Python 版本的兼容承诺。
- 不要使用仅 3.13+ 才有的语法或标准库特性。

## 安装依赖（可选）

通用宿主的真实语音 / 可选 VTS：

```bash
python -m pip install -r requirements-host.txt
python -m public_host
```

浏览器打开终端显示的本机地址，按 [PUBLIC_HOST](PUBLIC_HOST.md) 保存自己的配置。离线演示不需要安装依赖；真实服务适配器当前接收兼容聊天接口和完整 16 位 PCM WAV，不能把所有 TTS 服务视为直接兼容。VTS 另见 [VTS_EXAMPLE](VTS_EXAMPLE.md)。

较早的独立模块按需要安装 `python -m pip install -r requirements.txt`，注意：

- `PyAudioWPatch` 是 **Windows** 的 WASAPI 音频 I/O；其他平台的音频输出需要额外移植，不是改个 import 就能跑。
- `playwright` 只服务 Wordle bot，还需要 `playwright install` 下载浏览器。
- 依赖装完不代表链路可用：还需要对应服务的凭据、兼容的音色 / 模型，以及真实设备。

## 两种本机配置

公开宿主通过网页保存到 `.local-host/config.json`，VTS 授权保存在 `.local-host/vts/`；可用 `--data-dir` 显式指定其他目录。宿主不自动读取 `.env`、私人直播配置或旧 VTS 授权。密钥在本机文件中明文保存，保存后不回显；不要把配置目录附到 Issue、PR 或日志中。

较早的独立模块从环境变量读取凭据，模板见 [.env.example](../.env.example)。`.env` 与 `.local-host/` 均已忽略，实际凭据永不提交。

| 分组 | 变量 | 用途 |
|---|---|---|
| 快脑 / 文本模型 | `ARK_API_KEY_FAST`、`DASHSCOPE_API_KEY` | 快脑与 DashScope 系服务 |
| 实时语音 | `VOLC_DIALOG_APP_ID`、`VOLC_DIALOG_ACCESS_KEY` | doubao SC2.0 双人语音会话 |
| 联网搜索 | `VOLC_WEBSEARCH_API_KEY`、`VOLC_WEBSEARCH_BOT_ID` | `dialogue/web_search.py` |
| 本地声线 | `CHARACTER_A_VOICE_ID`、`CHARACTER_A_VOICE_MODEL`、`CHARACTER_B_*`、`TTS_MODEL`、`TTS_VOICE_ID` | `settings/voice_registry.py` 解析克隆音色，缺失时安全回退 |
| 独立 Qwen 音频 | `QWEN_AUDIO_TTS_WEBSOCKET_URL` | `audio/qwen_audio_tts.py` |

`settings/voice_registry.py` 的旧模块保留已有音色回退行为。公开宿主则要求启用角色填写服务支持的音色；真实模式缺少必需配置时应提示错误，不能悄悄切为演示或伪报播放成功。

## 运行模式

| 目标 | 命令 | 前置条件 |
|---|---|---|
| 无声演示（真实调度 + 占位脑与语音） | `python main.py` | 无 |
| 网页离线演示 | `python -m public_host` | 在页面主动选择离线演示、保存后开始 |
| 真实对话 / 语音 / 可选 VTS | `python -m public_host` | 宿主依赖、个人服务配置；VTS 需独立授权与模型 |
| 离线协议样本 | `python -m examples.prototype_contracts` | 无 |
| 自动检查 | `python -m unittest discover -s tests` | 允许本机 loopback 假服务；无需第三方依赖和真实凭据 |
| 编译检查 | `python -m compileall -q .` | 无 |
| Buckshot 决策 / 桥 | `python -m games.buckshot.bot` | 游戏端桥 |
| Terraria 走路测试 | `python -m games.terraria.bot --demo walk` | Terraria、tModLoader、模组构建与桥；此命令会启动并连接真实游戏 |
| Kingdom Rush 决策预览 | `python -m games.kingdom_rush.ai --dry-run` | 正在运行的游戏与兼容桥；只是不执行决策动作，仍会连接游戏 |
| Wordle 无模型模式 | `python -m games.word_games.wordle.bot --no-llm` | 无 |
| Handle 桥 | `python -m games.word_games.handle.bridge` | 游戏端桥 |

游戏细节见 [games/README](../games/README.md)；独立语音模块见 [VOICE_MODULES](VOICE_MODULES.md)。

## 资源定位

`core.paths.PROJECT_ROOT` 指向仓库根目录。音量配置 `role_volumes.json`、快脑读取的相对提示词路径、字幕与素材都按仓库根解析——**不要**假设当前工作目录，也不要把资源搬进 `core/`。这些运行资源由使用者自备，仓库里没有。

## 常见故障

| 现象 | 先检查 |
|---|---|
| `ModuleNotFoundError: No module named 'openai'` | 是否进入了需要此 SDK 的独立模块；演示和公开宿主的兼容 HTTP 适配器不要求它 |
| `ModuleNotFoundError: core...` | 是否在仓库根目录执行，或用 `python -m` 而不是直接跑子目录文件 |
| 有输出但没声音 | `main.py` 和宿主离线演示本来无声；真实模式需检查试听结果、设备与状态，不能直接当成正常 |
| 实时语音连不上 | 凭据是否过期、网络是否可达；公开库不提供代用服务 |
| 音频设备报错 | 宿主使用 sounddevice / PortAudio；旧模块可能使用 PyAudioWPatch。按实际入口检查依赖、设备和占用 |
| 自动测试访问真实服务 / 声卡 | 应改用替身；测试自建的本机 HTTP 服务属于允许范围，见 [TESTING](TESTING.md) |

## 不要做的事

- 不要把真实凭据写进代码、测试或文档示例。
- 不要把"演示能跑"当成"语音链路验收"。
- 不要在 `core/`、`settings/` 里引入第三方依赖——零依赖演示与离线测试都依赖这条边界。
