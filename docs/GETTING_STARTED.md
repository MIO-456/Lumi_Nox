# 快速开始：先看两位角色怎样接话

[返回首页](../README.md) · [English README](../README.en.md) · [如何贡献](../CONTRIBUTING.md) · [可以一起做什么](CONTRIBUTION_TASKS.md)

这份指南带你下载公开代码，运行 `main.py`，检查结果，再找到适合继续阅读的模块。

**你会得到一个自动播放的无声终端演示。** 发言调度与仲裁使用仓库里的真实模块；回复由 `DemoBrain` 返回固定占位台词，`demo_speak()` 只打印文字。它不会调用模型、连接直播间、播放声音或启动游戏，也不等待你输入弹幕。

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

## 2. 运行演示

在刚下载的仓库根目录运行：

Windows PowerShell：

```powershell
python main.py
```

macOS / Linux：

```bash
python3 main.py
```

**到这里无需 `pip install`，也无需复制 `.env`、填写密钥或下载模型。** 演示及下方检查只使用 Python 标准库。`requirements.txt` 是供更深入的语音、模型、游戏接入参考的依赖列表，其中包含 Windows 音频组件，不是这个演示的安装步骤。

## 3. 对照预期输出

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

[现有 CI](../.github/workflows/ci.yml) 在 Ubuntu / Python 3.12 中执行上述编译与测试；测试里的 `test_zero_setup_demo` 还会运行 `main.py`。它们检查公开核心和无外部服务的演示，不能据此推断真实语音、游戏设备或整套直播在你的平台都能工作。

遇到问题时，先检查这些常见情况：

| 现象 | 先检查什么 |
|---|---|
| 找不到 `main.py` | 终端是否处于解压或克隆后的仓库根目录；Windows 用 `Get-Location` / `Get-ChildItem`，macOS / Linux 用 `pwd` / `ls` 查看。 |
| Python 版本不对，或代码出现语法错误 | 再运行版本命令，确认使用的是 Python 3.12；Windows 可尝试 `py -3.12 main.py`。 |
| 缺少 `event_bus` 等本地模块 | 是否完整下载仓库，而不是只保存了 `main.py`。 |
| 没声音、不能输入问题、回复总是相似 | 这是固定场景的无声演示，符合预期；手动输入是[可参与的扩展任务](CONTRIBUTION_TASKS.md)，当前尚未提供。 |
| 测试出现 `FAILED` / `ERROR`，或程序未正常结束 | 记录系统、Python 版本、仓库版本、执行命令与完整报错，按[贡献指南](../CONTRIBUTING.md)提交可复现的问题。 |

## 5. 下一步读哪些代码

按一条消息从进入到输出的顺序阅读：

1. [main.py](../main.py)：找到 `main()`，看事件总线、状态机、调度器和仲裁器怎样组装；同时认清 `DemoBrain`、`demo_speak()` 两处占位实现。
2. [speaker_scheduler.py](../speaker_scheduler.py)：跟随 `enqueue_input()`、`pick_speaker()`、`advance_from()`，看观众消息入队、点名选择与轮换。
3. [speech_output_arbiter.py](../speech_output_arbiter.py)：跟随 `request_start()` 和 `mark_done()`，看发言权如何申请、排队与释放。
4. [event_bus.py](../event_bus.py) 与 [state_machine.py](../state_machine.py)：看模块如何交换事件、节目状态如何转换。
5. [整体架构](ARCHITECTURE.md)：再去理解文本链路的 [conversation.py](../conversation.py)、实时语音的 [realtime_chat.py](../realtime_chat.py)，或选择一个[游戏模块](../games/README.md)深入。

演示只在本地保存搭档上一句话，不等于完整历史镜像链路，也不调用长期记忆。更完整的对话编排、镜像和记忆实现在各自模块中。

想接自己的模型或语音时，再阅读对应代码、[依赖列表](../requirements.txt)、[环境变量示例](../.env.example)与游戏设置说明。公开仓库没有当前直播的完整启动器、导演中控、真实角色人设或角色资产；安装依赖、填好密钥仍需针对具体模块进行集成。

想开始贡献，可以先看[任务与方向](CONTRIBUTION_TASKS.md)。终端交互、复现测试和教程可从公开代码独立开展；新游戏、绘画表现或角色点唱等节目方向，先讨论原型与接入条件，再按[贡献流程](../CONTRIBUTING.md)推进。

原型作者还可以运行 `python -m examples.prototype_contracts`，阅读[最小接口与离线样本](PROTOTYPE_INTERFACES.md)。这条路径同样只需标准库；它展示数据交接，不启动游戏、渲染器或演唱服务。独立 Qwen 后端的调用与配置见[语音模块指南](VOICE_MODULES.md)。
