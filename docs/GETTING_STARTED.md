# 快速开始：运行自己的示例角色

> **何时读**：第一次把仓库跑起来，或需要对照演示的预期输出与排错。
> **何时不用读**：已经能跑通 `main.py` —— 要改代码看 [DEVELOPMENT](DEVELOPMENT.md)，要写测试看 [TESTING](TESTING.md)。
> **复核**：入口命令、演示输出或 Python 基线版本变化时。

[返回首页](../README.md) · [English README](../README.en.md) · [如何贡献](../CONTRIBUTING.md) · [可以一起做什么](CONTRIBUTION_TASKS.md)

这份指南带你下载公开代码，先运行通用公开宿主的网页演示，再了解原有终端示例和代码入口。模型与真实声音的配置步骤见[公开宿主指南](PUBLIC_HOST.md)。

**首次可以不装依赖、不填密钥，先选择手动对话，在本机网页输入消息。** 手动对话的离线演示使用示例回复与模拟播放，运行真实的角色调度和取消流程，不调用模型、连接直播间、播放声音、连接 VTS 或启动游戏。另有[节目中控](PUBLIC_DIRECTOR.md)，可运行带真实五子棋的节目；它需要额外安装 aiohttp，不含私人角色资产或平台开播接线。

## 1. 下载代码

准备 Python 和 Git。**Python 3.12 是当前已验证基线**，公开 CI 也使用这个版本；其他版本可自行尝试，但不在这里承诺兼容。先用下面的版本命令确认实际调用的解释器。

Windows PowerShell：

```powershell
python --version
git clone https://github.com/MIO-456/Lumi_Nox.git
cd Lumi_Nox
```

macOS / Linux 终端：

```bash
python3 --version
git clone https://github.com/MIO-456/Lumi_Nox.git
cd Lumi_Nox
```

也可以在[仓库首页](https://github.com/MIO-456/Lumi_Nox)选择 **Code → Download ZIP**，解压后在含有 `main.py` 的目录打开终端。准备提交 PR 时，建议使用 Git，具体流程见[如何贡献](../CONTRIBUTING.md)。

如果系统找不到 Python 命令，先安装 Python 3.12，再重新打开终端。Windows 已安装 Python Launcher 时，也可以用 `py -3.12` 代替本指南的 `python`。如果找不到 `git`，安装 Git 后重开终端，或先使用 ZIP 下载。

## 2. 运行网页演示

在刚下载的仓库根目录运行：

Windows PowerShell：

```powershell
python -m public_host
```

macOS / Linux：

```bash
python3 -m public_host
```

浏览器打开 **http://127.0.0.1:8765**，按下面的顺序操作：

1. 将“运行模式”从默认的“真实服务”切换为“离线演示”。
2. 将“运行方式”从默认的“按节目单运行”切换为“手动对话”，点击“保存本机配置”，再点击“开始会话”。
3. 输入一句话并发送，查看角色回复和运行事件。回复是示例文本，事件标为模拟结果；无声是预期行为。
4. 可在“角色与皮套”中启用第二位示例角色；修改配置前先停止会话。再次保存、开始后，可指定角色或观察逐条消息轮换。
5. 点击“停止会话”；不用控制页时，在终端按 Ctrl+C 退出。

**到这里无需 `pip install`，也无需复制 `.env`、填写密钥或下载模型。** 服务只监听本机。默认配置保存在被 Git 忽略的 `.local-host/`，不自动读取其他模块的环境变量或私人直播配置。

需要真实对话与声音时，先安装 `python -m pip install -r requirements-host.txt`，按[公开宿主指南](PUBLIC_HOST.md)配置自己的模型、服务、音色和输出设备。当前适配器分别使用 `/chat/completions` 文本回复与 `/audio/speech` 的完整 16 位 PCM WAV，并非所有标称兼容的 TTS 都能直接使用。VTS 可先不启用；需要时再按 [VTS/Hiyori 示例](VTS_EXAMPLE.md)安装模型、授权和检查嘴型。

`requirements.txt` 则供其他语音、模型和游戏模块使用，其中包含 Windows 音频组件；不必为了网页演示安装它。

### 可选：跑一场带真实五子棋的节目

安装 `python -m pip install aiohttp`，在角色页启用两个不同名字的角色，选择“按节目单运行”并保存。点击“开始节目”，运行开场→聊天→五子棋→聊天→收尾。五子棋由本地算法按真实规则落子、计分；离线模式的聊天与结果发言仍是无声模拟，不能当作真实出声验收。节目操作、时长与停止规则见[公开中控指南](PUBLIC_DIRECTOR.md)。

### 可选：原有终端演示

在根目录运行 `python main.py`（macOS / Linux 按需改用 `python3`）。它自动演示两位角色接话，`DemoBrain` 返回固定台词，`demo_speak()` 只打印文字，不接收手动输入。这个入口同样只需标准库。

## 3. 对照终端演示的预期输出

程序会在几秒内自动演完六轮对话，再演示发言权被占用时的排队。下面是输出节选；省略号表示中间还有其他台词，不是报错：

```text
loaded character: Lumi  (subtitle color #FFC0CB)
loaded character: Nox  (subtitle color #FFA500)
     [state] IDLE -> OPENING
     [state] OPENING -> CHATTING
...
  >> viewer @-mentioned Nox -> routed to Nox
  Nox: mona, bold of you to assume I'd make this easy.
  >> next turn -> Lumi
...
  >> viewer @-mentioned Lumi -> routed to Lumi
...
=== one voice at a time (arbitration) ===
...
     [arbiter] Nox queued - floor is busy
  Nox requests the floor while Lumi holds it -> blocked (queued)
  Lumi finishes; the floor is free again.
     [state] CHATTING -> ENDING
     [state] ENDING -> IDLE

=== demo complete ===
```

这里可以核对三件事：

1. 预设观众消息点名 Nox、Lumi 时，调度器把消息交给对应角色。
2. 没有点名的轮次按顺序轮换；台词是固定示例，每次运行相似是正常的。
3. Lumi 占用发言权时，Nox 的请求显示 `blocked (queued)`。这段只展示排队与释放，不继续播放排队的那句。

输出里的 `speech_...` 标识会随运行变化。**没有声音是预期行为**；看到 `=== demo complete ===` 后程序退出也是正常结束。

## 4. 检查环境与代码

仍在仓库根目录运行：

Windows PowerShell：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
```

macOS / Linux：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
```

测试通过时结尾显示 `OK`；编译检查成功时通常没有输出。`compileall` 会生成本地 `__pycache__` 缓存，仓库已忽略这些文件。

[现有 CI](../.github/workflows/ci.yml) 配置为在 Ubuntu、Windows / Python 3.12 中执行上述编译与测试；测试里的 `test_zero_setup_demo` 还会运行 `main.py`。宿主测试使用替代的模型、声卡和 VTS，HTTP 测试需要允许本机回环连接。这些检查不能证明真实云服务、音色效果、声卡、Hiyori 或整套直播在你的平台都能工作；真实连接需按宿主指南逐项验收。

遇到问题时，先检查这些常见情况：

| 现象 | 先检查什么 |
|---|---|
| 找不到 `main.py` | 终端是否处于解压或克隆后的仓库根目录；Windows 用 `Get-Location` / `Get-ChildItem`，macOS / Linux 用 `pwd` / `ls` 查看。 |
| Python 版本不对，或代码出现语法错误 | 再运行版本命令，确认使用的是 Python 3.12；Windows 可尝试 `py -3.12 main.py`。 |
| 缺少 `event_bus` 等本地模块 | 是否完整下载仓库，而不是只保存了 `main.py`。 |
| 网页打不开 | 确认终端服务仍在运行，并打开终端打印的本机地址。端口被占用时可运行 `python -m public_host --port 8766`。 |
| 网页回复是示例文本、没有声音 | 检查是否选择了“离线演示”；此模式不调用真实服务、不打开声卡。 |
| 开始节目提示缺少 aiohttp 或需要两个角色 | 五子棋需要安装 aiohttp，并启用两位名字不同的角色；若只想零依赖聊天，切换为“手动对话”。 |
| `main.py` 没声音、不能输入问题、回复总是相似 | 终端入口是固定场景的无声演示；需要手动输入时运行 `python -m public_host`。 |
| 测试出现 `FAILED` / `ERROR`，或程序未正常结束 | 记录系统、Python 版本、仓库版本、执行命令与完整报错，按[贡献指南](../CONTRIBUTING.md)提交可复现的问题。 |

## 5. 下一步读哪些代码

按一条消息从进入到输出的顺序阅读：

公开宿主从 [public_host/server.py](../public_host/server.py) 接收配置与输入，由 [public_host/session.py](../public_host/session.py) 串联调度、模型、播放与取消；服务协议在 [providers.py](../public_host/providers.py)，声音与可选嘴型在 [playback.py](../public_host/playback.py) 和 [avatar.py](../public_host/avatar.py)。节目入口为 [public_host/program.py](../public_host/program.py)，调用 [core/program/](../core/program/) 的共用推进逻辑和[五子棋适配器](../public_host/activities/gomoku.py)。它没有自动接入直播间、麦克风、长期记忆、OBS 控制或其他游戏。

想先读更小的例子，可以沿着原有终端演示阅读：

1. [main.py](../main.py)：找到 `main()`，看事件总线、状态机、调度器和仲裁器怎样组装；同时认清 `DemoBrain`、`demo_speak()` 两处占位实现。
2. [core/speaker_scheduler.py](../core/speaker_scheduler.py)：跟随 `enqueue_input()`、`pick_speaker()`、`advance_from()`，看观众消息入队、点名选择与轮换。
3. [core/speech_output_arbiter.py](../core/speech_output_arbiter.py)：跟随 `request_start()` 和 `mark_done()`，看发言权如何申请、排队与释放。
4. [core/event_bus.py](../core/event_bus.py) 与 [core/state_machine.py](../core/state_machine.py)：看模块如何交换事件、节目状态如何转换。
5. [整体架构](ARCHITECTURE.md)：再去理解文本链路的 [dialogue/conversation.py](../dialogue/conversation.py)、实时语音的 [audio/realtime_chat.py](../audio/realtime_chat.py)，或选择一个[游戏模块](../games/README.md)深入。

演示只在本地保存搭档上一句话，不等于完整历史镜像链路，也不调用长期记忆。更完整的对话编排、镜像和记忆实现在各自模块中。

想接自己的模型或语音，先用[公开宿主](PUBLIC_HOST.md)的配置页完成基础链路；同协议服务通常只需改地址、模型和凭据，不同协议需要增加适配器。独立语音、ASR 或其他游戏模块仍需参考对应代码、[旧模块依赖](../requirements.txt)、[环境变量示例](../.env.example)与设置说明接线。通用中控和共用调度已公开；正式导演候选也调用该共用层，但尚未完成正式环境实机推广。私人平台/设备接线、真实角色人设与资产仍不包含。

想开始贡献，可以先看[任务与方向](CONTRIBUTION_TASKS.md)。终端交互、复现测试和教程可从公开代码独立开展；新游戏、绘画表现或角色点唱等节目方向，先讨论原型与接入条件，再按[贡献流程](../CONTRIBUTING.md)推进。

原型作者还可以运行 `python -m examples.prototype_contracts`，阅读[最小接口与离线样本](PROTOTYPE_INTERFACES.md)。这条路径同样只需标准库；它展示数据交接，不启动游戏、渲染器或演唱服务。独立 Qwen 后端的调用与配置见[语音模块指南](VOICE_MODULES.md)。
