# 术语表

> **何时读**：读到 `快脑`、`发言权`、`镜像`、`仲裁` 这类词不确定指什么，或要给新概念起名。
> **何时不用读**：只是在跑 `main.py` 或跑测试。
> **复核**：引入新的核心概念时；同一概念出现第二种叫法时（要么统一，要么在这里说明区别）。

公开库里同时存在"节目概念"和"代码概念"，两者不完全对应。本表给出**代码里的落点**，避免只记住比喻。

## 角色与节目

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **Lumi / Nox** | 两位共同主持直播的 AI 角色。公开库只提供占位角色，不含真实人设 | `settings/voice_config.py`、`main.py` |
| **双角色共播** | 两个 AI 共享一个舞台、轮流说话，而不是各播各的 | `core/speaker_scheduler.py` |
| **接话** | 一个角色说完，另一个接着说的过程；默认靠轮换，被点名时由点名决定 | `SpeakerScheduler.pick_speaker()` |
| **open-core** | 编排、语音、记忆、游戏桥开源；人设、真实声线、Live2D、观众数据不公开 | [ARCHITECTURE](ARCHITECTURE.md#open-core) |

## 发言与调度

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **发言权 / floor** | "现在谁能出声"的独占权。同一时刻只有一个角色持有 | `core/speech_output_arbiter.py` |
| **仲裁器 / arbiter** | 发放与回收发言权的组件；申请失败时可排队、丢弃或打断 | `SpeechOutputArbiter.request_start()` / `mark_done()` |
| **QUEUE / DROP / INTERRUPT** | 抢不到发言权时的三种策略：排队、丢弃、打断当前发言 | `POLICY_QUEUE` / `POLICY_DROP` / `POLICY_INTERRUPT` |
| **调度器 / scheduler** | 决定"下一句谁来说"，不管声音怎么播 | `core/speaker_scheduler.py` |
| **轮换 / rotation** | 没有点名时按 `active_speakers` 顺序交替 | `next_speaker`、`advance_from()` |
| **点名路由** | 文本里 @ 到某个角色就交给它回应；@ 到多个则退回轮换 | `detect_addressed_speaker()` |
| **输入队列** | 弹幕 / SC 不打断当前发言，先入队，下一轮取 | `enqueue_input()` / `pop_all_inputs()` |
| **历史镜像 / mirroring** | 把对方刚说的话以"`[Lumi said] ...`"形式写进自己的上下文，且不混成自己的话 | `dialogue/conversation.py` |

调度与仲裁是两件事：调度决定**顺序**，仲裁保证**不重叠**。改其中一个不要顺手改另一个。

## 模型与语音

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **快脑 / fast brain** | 每个角色的轻量文本模型，负责工具型决策（例如游戏该走哪一步），追求低延迟 | `dialogue/fast_brain.py` |
| **文本链路** | 文本模型生成 → 独立 TTS 合成 → 播放（相对实时语音链路） | `dialogue/conversation.py` |
| **实时语音 / speech-to-speech** | 端到端语音会话（doubao SC2.0），直接进出声，不经文本再合成 | `audio/realtime_chat.py` |
| **TTS / ASR** | 语音合成 / 语音识别 | `audio/lumi_tts.py`、`audio/lumi_asr.py` |
| **PCM / 整句输出** | 合成结果是字节流；独立 Qwen 后端按整句收集、按文本顺序播放 | `audio/qwen3_tts.py`、`audio/spoken_output.py` |
| **占位实现** | 为了让演示零依赖而替换真实现的替身，名字带 `Demo` 前缀 | `DemoBrain`、`demo_speak()` |

## 状态与输入

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **IDLE / OPENING / CHATTING** | 未开播 / 开场 / 聊天中 | `core/state_machine.py` |
| **PLAYING_\\*** | 正在某个游戏环节：`PLAYING_WORDLE`、`PLAYING_HANDLE`、`PLAYING_BUCKSHOT`、`PLAYING_TERRARIA`、`PLAYING_KR` | 同上 |
| **DRAWING / TRANSITIONING / ENDING** | 绘画环节 / 环节切换 / 收尾 | 同上 |
| **弹幕 / SC / 礼物 / 上舰 / 进房 / 互动** | 观众输入的类型前缀；调度器会剥掉前缀再做点名判断 | `SpeakerScheduler._extract_routing_text()` |
| **Mio / 键盘 / 未知** | 操作者输入、键盘输入、来源未知——同样剥前缀 | 同上 |

## 记忆

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **pinned 事实** | 被钉住、不会被衰减隐藏的长期事实 | `memory/decay.py` |
| **衰减 / decay** | 旧且未钉住的事实读取时被隐藏，但**不删除** | 同上 |
| **成员身份事实** | 观众是否上舰 / 等级这类身份信息，由确定性规则保护 | `memory/guard_facts.py` |
| **蒸馏 / distillation** | 会话结束后由模型抽取耐久事实写入 SQLite | `memory/extractor.py`、`memory/summarizer.py` |

记忆库本身不随仓库发布；测试用临时数据库，见 [TESTING](TESTING.md)。

## 游戏与原型

| 术语 | 含义 | 代码落点 |
|---|---|---|
| **桥 / bridge** | 仓库代码与游戏进程之间的通信层（TCP、Lua、套接字等） | `games/*/` |
| **tModLoader** | Terraria 的 mod 加载器；仓库内附焦点安全的桥 mod | `games/terraria/mod/` |
| **行为树 / goal planner** | Terraria 用的分层目标规划与 A* 寻路 | `docs/games/terraria-behavior-tree.md` |
| **最小协议** | 给外部贡献者的离线握手形状（游戏局面、画板消息、歌曲资产） | `interfaces/`、`examples/` |

商业游戏本体、画板服务、演唱服务都不在公开库——只有协议与离线样本。
