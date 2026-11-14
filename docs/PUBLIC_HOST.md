# 通用公开宿主

公开宿主把配置、模型回复、语音播放、角色轮换和可选 VTube Studio 嘴型接在一起。你可以用自己的模型服务和角色运行它，也可以先用无声的离线演示了解流程。

这是供使用者和贡献者运行模块的最小宿主。当前入口为网页文字输入，不含麦克风 ASR、平台弹幕、自动推流、正式节目单、视觉或游戏自动编排。已有游戏和原型接口仍可单独使用；它们尚未自动接到这个控制页。

## 1. 先启动离线演示

需要 Python 3.10 或以上；CI 使用 Python 3.12。在仓库根目录执行：

```sh
python -m public_host
```

打开终端显示的本机地址（默认 `http://127.0.0.1:8765`），选择 **离线演示**，保存后开始会话，再发送一句话。此模式不需要安装第三方依赖，也不调用模型、不播放声音、不连接 VTS；页面明确标记“演示完成”。配置页中的 VTS 查询是独立、手动发起的操作。

角色页默认只启用“示例A”。启用“示例B”并保存，可测试轮换：每条观众输入只由一位角色回答，成功结束后下一条交给下一位；也可手动指定角色。它不会在没有输入时自动连续聊天。

原有 `python main.py` 仍是固定事件的终端演示，与这个网页入口各自独立。

## 2. 接入自己的模型和声音

建议使用虚拟环境，再安装宿主的可选依赖：

```sh
python -m pip install -r requirements-host.txt
python -m public_host
```

`sounddevice` 用于实际播放，`websockets` 用于可选 VTS。Windows 的 sounddevice 安装包通常带 PortAudio；Linux 若缺少 PortAudio，需要按发行版安装系统音频库。

在停止状态下切换为 **真实服务**，填写并保存：

| 配置 | 填什么 |
|---|---|
| 聊天基础地址 | 服务商提供的兼容 API 根地址，通常以 `/v1` 结尾，不要填到 `/chat/completions` |
| 聊天模型名、Key | 你购买或本地部署的模型标识与凭证；无鉴权本地服务可不填 Key |
| 回复长度字段 | 按服务要求选择 `max_tokens`、`max_completion_tokens` 或不发送 |
| 语音基础地址、模型名、Key | 可与聊天使用不同服务，分别保存 |
| 角色音色 | 该语音服务支持的音色名称或 ID |
| 输出设备 | 系统默认、实际扬声器或你自己配置的虚拟声卡 |

先点“测试聊天连接”和“试听声音”。它们会各自发起一次真实请求，试听会实际出声。通过后开始会话：输入 → 模型文本 → 合成完整音频 → 播放结束 → 下一位角色。

### 当前适配范围

- 聊天：`POST {base_url}/chat/completions`，`stream: false`，读取 `choices[0].message.content` 的文本。
- 语音：`POST {base_url}/audio/speech`，发送 `model`、`input`、`voice`、`response_format: "wav"`，接收完整 WAV 文件。当前支持 16-bit PCM、单/双声道、8–96 kHz，单条不超过 120 秒。
- 两种服务可分别设置地址、模型、Key 和超时。同协议换服务通常只改配置。
- “兼容聊天接口”不代表也兼容语音。返回 SSE、URL、Base64、MP3 或不同 JSON 结构的服务，需要另写适配器；现有 Qwen/CosyVoice 模块没有自动接进这个宿主。
- 这里不内置任何运营方正在使用的模型、声线或服务地址。调用者自己承担所选服务的使用费用。

字段依据：[Chat Completions](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)、[Speech](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)。第三方实现仍须按其文档和连接检查确认，不能仅凭“OpenAI-compatible”名称判断全部能力。

## 3. 停止、打断和历史

- **打断当前**：取消当前回复/合成/播放，保留后面的队列。
- **停止会话**：同时清空等待队列；收尾结束前禁止新会话与配置修改。
- **直接朗读**：必须指定角色，跳过模型，仅合成并播放输入文字；不写入聊天历史，也不推进自动轮换。
- **试听**：仅在停止状态使用，完成、失败或停止后自动释放会话；不连接皮套。

只有音频设备确认播放结束，才记录“已播完”并提交该角色历史。取消和失败不推进轮换，也不会把未播完的回答记成成功。历史只保存在当前会话内，每个角色最多保留最近 20 个完成回合，重开清空；不自动导入私人记忆。

外部 HTTP 请求如果正在等待响应，停止后可能仍需等待该请求返回或超时；页面会保留“正在停止”，迟到结果不会播放或提交历史。当前不提供流式逐句播放。

## 4. 添加 Hiyori / 自有皮套

按 [VTS 示例](VTS_EXAMPLE.md) 自行安装 VTube Studio 和合法模型，先完成单角色授权、模型 ID 绑定、嘴型映射，再扩展到不同端口的两个实例。

皮套是可选项，不配置仍可文字对话与真实出声。宿主不会下载或自动切换模型，不含 Lumi/Nox 的模型、表情动作资产和人设。模型许可与代码许可分别适用。

## 5. 配置文件与本地数据

默认保存到仓库根的 `.local-host/config.json`，已由 `.gitignore` 排除；VTS 授权 token 位于同目录下的 `vts/`。Key 以明文保存在本机文件，网页保存后不回显。备份或分享时不要附带这个目录。

空白 Key 输入框在地址未改变时保留已保存值；选“清除已保存的 Key”才删除。更改服务地址时空白 Key 会清空旧值，需要填写新服务的 Key。配置示例在 [examples/public-host.example.json](../examples/public-host.example.json)，不含可用账号或服务。

可用 `--data-dir` 指定独立目录，用 `--port` 更换端口。例如：

```sh
python -m public_host --data-dir .local-host --port 8766
```

宿主只绑定本机地址，打开页面不会调用外部模型。它不会读取父目录 `.env`、正式直播配置、私人角色文件或旧 VTS token。Ctrl+C 退出服务。

## 6. 贡献者如何接入

先阅读 [贡献说明](../CONTRIBUTING.md) 和 [贡献任务](CONTRIBUTION_TASKS.md)。新模块产生一段解说时，可调用 `Session.submit_speech(text, speaker)`，不要自己同时打开第二个播放器。下面是无声、零 Key 的可运行示例：

```python
import time
from public_host.config import default_config, runtime_config
from public_host.providers import make_services
from public_host.playback import make_player
from public_host.session import Session

config = default_config()
config["mode"] = "demo"
config = runtime_config(config)
chat, synth = make_services(config)
session = Session(config, chat=chat, synth=synth, player=make_player(config))
session.start()
try:
    request_id = session.submit_speech("新模块已完成一次动作。", "示例A")
    while session.snapshot()["running"]:
        events = session.snapshot()["events"]
        result = next((e for e in events if e["data"].get("request_id") == request_id
                       and e["event_type"] in ("turn_completed", "turn_simulated",
                                               "turn_failed", "turn_cancelled")), None)
        if result:
            print(result["event_type"], result["data"]["outcome"])
            break
        time.sleep(0.05)
finally:
    session.stop()
    while session.snapshot()["running"]:
        time.sleep(0.05)
```

接收 `request_id` 只代表入队；用带相同 ID 的终态事件判断完成或失败。接真实服务时使用经过校验的个人配置，并注入实际服务/播放器；启用 VTS 还需要传入对应的 `avatar_factory`，网页入口已完成这些接线。

新增不兼容协议时，实现 `complete(messages, cancel)` 或 `synthesize(text, voice, cancel)` 并接入服务工厂和配置校验；沿用取消检查、有限大小、错误脱敏与 `AudioClip` 契约，不改每个调用模块。用本地假服务补协议测试，再说明自己的真实服务验证结果。

## 7. 验证范围

自动测试覆盖配置、HTTP 协议、真实 loopback 服务、队列、取消、播放结束回调、VTS 假连接及本机页面接口；不需要 Keys 或硬件。运行：

```sh
python -m unittest discover -s tests -v
```

这些测试不代表特定云模型、真实扬声器或 Hiyori 已实机验证。首次使用按“离线演示 → 聊天连接 → 声音试听 → 单角色 VTS → 双角色”的顺序检查。发布时请分别记录自动测试与实机结果。
