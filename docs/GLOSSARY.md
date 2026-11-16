# 术语表

> **何时读**：读到 `快脑`、`发言权`、`镜像`、`仲裁` 这类词不确定指什么，或要给新概念起名。
> **何时不用读**：只是在跑 `main.py` 或跑测试。
> **复核**：引入新的核心概念时；同一概念出现第二种叫法时（要么统一，要么在这里说明区别）。

公开库里同时存在"节目概念"和"代码概念"，两者不完全对应。本表给出**代码里的落点**，避免只记住比喻。

## 角色与节目

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **Lumi / Nox** | 两位共同主持直播的 AI 角色。公开库只提供占位角色，不含真实人设 | `settings/voice_config.py`、`main.py` |
| **通用公开宿主** | 使用本机网页把自定义角色、模型、语音与可选嘴型接起来；不等于正式直播完整中控 | `public_host/`、[PUBLIC_HOST](PUBLIC_HOST.md) |
| **示例A / 示例B** | 宿主默认角色名；使用者可改名称、设定和音色，启用一至两位角色 | `public_host/config.py` |
| **双角色共播** | 两个 AI 共享一个舞台、轮流说话，而不是各播各的 | `core/speaker_scheduler.py` |
| **接话** | 根据输入、点名与轮换选择发言角色；公开宿主每条输入只回应一次，不自动发起下一轮聊天 | `SpeakerScheduler.pick_speaker()`、`public_host/session.py` |
| **open-core** | 通用宿主、编排与部分语音/记忆/游戏代码公开；私人角色设定、声线、Live2D资产及观众数据不分发 | [ARCHITECTURE](ARCHITECTURE.md#open-core) |

## 发言与调度

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **发言权 / floor** | "现在谁能出声"的独占权。同一时刻只有一个角色持有 | `core/speech_output_arbiter.py` |
| **仲裁器 / arbiter** | 发放与回收发言权的组件；申请失败时可排队、丢弃或打断 | `SpeechOutputArbiter.request_start()` / `mark_done()` |
| **QUEUE / DROP / INTERRUPT** | 抢不到发言权时的三种策略：排队、丢弃、打断当前发言 | `POLICY_QUEUE` / `POLICY_DROP` / `POLICY_INTERRUPT` |
| **调度器 / scheduler** | 决定"下一句谁来说"，不管声音怎么播 | `core/speaker_scheduler.py` |
| **轮换 / rotation** | 没有点名时按 `active_speakers` 顺序交替 | `next_speaker`、`advance_from()` |
| **点名路由** | 文本里 @ 到某个角色就交给它回应；@ 到多个则退回轮换 | `detect_addressed_speaker()` |
| **输入队列** | 保存等待处理的消息；旧调度器接收观众事件，公开宿主另有网页请求队列，不自动连接直播间 | `enqueue_input()` / `pop_all_inputs()`、`Session.submit_text()` |
| **历史镜像 / mirroring** | 把搭档的话以带角色名的说明写进上下文，不混成自己的话；宿主只提交成功或演示完成的聊天回合 | `dialogue/conversation.py`、`public_host/session.py` |

调度与仲裁是两件事：调度决定**顺序**，仲裁保证**不重叠**。改其中一个不要顺手改另一个。

## 模型与语音

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **快脑 / fast brain** | 每个角色的轻量文本模型，负责工具型决策（例如游戏该走哪一步），追求低延迟 | `dialogue/fast_brain.py` |
| **文本链路** | 文本模型生成 → 独立 TTS 合成 → 播放（相对实时语音链路） | `dialogue/conversation.py`、`public_host/session.py` |
| **实时语音 / speech-to-speech** | 端到端语音会话（doubao SC2.0），直接进出声，不经文本再合成 | `audio/realtime_chat.py` |
| **TTS / ASR** | 语音合成 / 语音识别 | `audio/lumi_tts.py`、`audio/lumi_asr.py` |
| **PCM / 整句输出** | 合成结果是字节流；独立 Qwen 后端按整句收集、按文本顺序播放 | `audio/qwen3_tts.py`、`audio/spoken_output.py` |
| **兼容服务适配器** | 按约定调用聊天/TTS服务；当前宿主使用文本 Chat Completions 与完整 PCM WAV，不能代表所有厂商协议 | `public_host/providers.py` |
| **VTS 嘴型** | 根据播放音频的音量写入 VTube Studio 的嘴型输入；模型与私人动作资产不随代码提供 | `public_host/avatar.py`、[VTS_EXAMPLE](VTS_EXAMPLE.md) |
| **占位实现** | 为零依赖演示替换外部服务或设备的替身；模拟完成不能写成真实播放成功 | `DemoBrain`、`demo_speak()`、`DemoChat`、`DemoPlayer` |

## 状态与输入

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **IDLE / OPENING / CHATTING** | 空闲 / 开始 / 聊天；公开宿主里的会话状态不代表已经开始直播推流 | `core/state_machine.py`、`public_host/session.py` |
| **PLAYING_\\*** | 正在某个游戏环节：`PLAYING_WORDLE`、`PLAYING_HANDLE`、`PLAYING_BUCKSHOT`、`PLAYING_TERRARIA`、`PLAYING_KR` | 同上 |
| **DRAWING / TRANSITIONING / ENDING** | 绘画环节 / 环节切换 / 收尾 | 同上 |
| **弹幕 / SC / 礼物 / 上舰 / 进房 / 互动** | 观众输入的类型前缀；调度器会剥掉前缀再做点名判断 | `SpeakerScheduler._extract_routing_text()` |
| **Mio / 键盘 / 未知** | 操作者输入、键盘输入、来源未知——同样剥前缀 | 同上 |

枚举或前缀存在不等于已接入对应功能：公开宿主尚未提供游戏、绘画、唱歌的活动生命周期与切换，也不读取平台弹幕。

## 记忆

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **pinned 事实** | 被钉住、不会被衰减隐藏的长期事实 | `memory/decay.py` |
| **衰减 / decay** | 旧且未钉住的事实读取时被隐藏，但**不删除** | 同上 |
| **成员身份事实** | 观众是否上舰 / 等级这类身份信息，由确定性规则保护 | `memory/guard_facts.py` |
| **蒸馏 / distillation** | 会话结束后由模型抽取耐久事实写入 SQLite | `memory/extractor.py`、`memory/summarizer.py` |

记忆库本身不随仓库发布；测试用临时数据库，见 [TESTING](TESTING.md)。公开宿主只保留当前会话历史，未自动接入这些长期记忆模块。

## 游戏与原型

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **桥 / bridge** | 仓库代码与游戏进程之间的通信层（TCP、Lua、套接字等） | `games/*/` |
| **tModLoader** | Terraria 的 mod 加载器；仓库内附焦点安全的桥 mod | `games/terraria/mod/` |
| **行为树 / goal planner** | Terraria 用的分层目标规划与 A* 寻路 | `docs/games/terraria-behavior-tree.md` |
| **最小协议** | 给外部贡献者的离线握手形状（游戏局面、画板消息、歌曲资产） | `interfaces/`、`examples/` |

商业游戏本体、画板服务、演唱服务不在公开库。`games/` 有独立游戏桥和决策代码；`interfaces/` 与样本只提供数据约定，不构成完整活动宿主或自动接线。
