# 语音接入与独立模块

希望先让自己的角色说话，可以使用[通用公开宿主](PUBLIC_HOST.md)。它提供启动入口、本机配置页和基础对话接线；下面的独立 Qwen/CosyVoice 模块则保留供更深入的接入使用。两条路径不共用同一套配置，也不会自动互相切换。

## 通用宿主：先配置一条完整语音链路

在仓库根目录运行：

```bash
python -m pip install -r requirements-host.txt
python -m public_host
```

打开 `http://127.0.0.1:8765`，填写自己的聊天与语音基础地址、模型、所需密钥，以及每位角色可用的音色名或 ID。先保存配置、测试聊天连接与试听声音，再开始会话；检查和试听会真实访问你配置的服务。仅体验无声离线演示时不需要安装上述依赖。

宿主的 `public_host/providers.py` 当前使用 `/audio/speech`，发送 `model`、`input`、`voice`、`response_format: "wav"`，接收完整、未压缩的 16 位 PCM WAV。支持单声道或双声道、8–96 kHz，单次语音最长 120 秒。`public_host/playback.py` 使用 sounddevice 输出，等待音频播放结束后才记为完成；停止或打断后，迟到的合成结果不会继续播放。

**接口名称相同不代表任意 TTS 都能直接使用。** 返回 MP3、原始 PCM、SSE 或厂商专有 WebSocket 数据的服务需要另写适配器；下面的 Qwen SSE 模块尚未自动接入这个宿主。相同协议内更换服务通常只需更新配置，不同协议应适配 `synthesize()` 返回的音频格式，避免把服务细节写进调度器。

本机设置与凭据保存在 `.local-host/`，不自动读取旧模块的 `.env`。可选 [VTS 嘴型](VTS_EXAMPLE.md)取自实际播放音频的音量；声卡设备、真实服务、音色和模型效果均需自行实测。公开宿主不包含私人声线、角色模型或正式直播完整中控。

## 独立 Qwen 与既有语音模块

这些后端供调用者自行接线。`main.py` 仍是无声的标准库终端演示；现有 `audio/lumi_tts.py` 和 `core/run_architecture.py` 没有自动切换到 Qwen。

## 模块与条件

| 模块 | 用途与使用条件 |
|---|---|
| `audio/cosyvoice_tts.py` | 原有流式 CosyVoice，增加首帧超时与有上限的重试；需要自己的服务凭据、兼容音色和音频设备。 |
| `audio/qwen3_tts.py` | Qwen HTTP SSE 适配、整句 PCM 收集、按文本顺序播放、取消和结果回传；可单独合成而不开声卡。 |
| `audio/qwen_audio_tts.py` | 把兼容 `tts_v2` 的 Qwen-Audio 服务转换到相同 PCM 接口；必须设置自己的 `QWEN_AUDIO_TTS_WEBSOCKET_URL`，并确认模型、音色及权限匹配。SDK 默认端点并非经过验证的替代品。 |
| `audio/tts_emitter.py` | 用 `open/feed/finish/abort` 统一合成器生命周期；旧 CosyVoice 调用保持可用，新回调需后端支持。 |
| `audio/role_volume.py` | 分角色音量、PCM 增益与削波；本地设置 `role_volumes.json` 已忽略，不随源码发布。示例角色键沿用 `Lumi` / `Nox`，不代表提供真实声线。 |
| `audio/spoken_output.py`、`audio/speech_pauses.py` | 清理格式和控制标签；支持的模型可使用经过校验的停顿。只处理明确格式，不保证识别所有自然语言旁白。 |

这些独立模块的依赖见 [requirements.txt](../requirements.txt)。它们的音频设备路径使用 Windows 的 PyAudioWPatch；标准库演示和离线测试不要求安装这些库。移植这些模块的音频输出到其他平台是额外工作；公开宿主使用单独的 [requirements-host.txt](../requirements-host.txt) 与 sounddevice 播放路径。

## 最小调用：合成为 WAV，不播放

安装语音依赖，在本地 `.env` 设置 `DASHSCOPE_API_KEY`。下面示例另外读取 `TTS_MODEL` 与 `TTS_VOICE_ID`；由使用者填写自己账号实际可用且相互兼容的值，没有内置角色音色。若模型名以 `qwen-audio-` 开头，还需要上述 WebSocket 地址。

在仓库根目录保存并运行自己的脚本：

```python
import os
import wave
from audio import qwen3_tts

qwen3_tts.init(pa_instance=None)  # 只加载本地配置；此调用不开声卡
result = qwen3_tts.synthesize_sentence_pcm(
    text="这是一段测试语音。",
    model=os.environ["TTS_MODEL"],
    voice_id=os.environ["TTS_VOICE_ID"],
)
with wave.open("sample.wav", "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(result.sample_rate)
    output.writeframes(result.pcm)
```

运行这个示例会真实调用使用者配置的服务。结果为 24 kHz、单声道、16 位 PCM 包装的 WAV；空结果、无效 PCM 或合成错误抛异常。失败请求最多尝试两次，失败尝试的半句音频不进入成功结果。

## 接入播放时

调用 `qwen3_tts.init(pa_instance=你的PyAudio实例)`，再创建 `IndependentSynth(speaker="Lumi")`，使用 `open(voice_id, model, cable_index)` → `feed(text)` → `finish()`。`cable_index` 是本机输出设备编号；不要照搬其他电脑编号。需要统一入口时，可把合成器交给 `IndependentTTSEmitter`。

`finish()` 后检查 `outcome`：`complete`、`partial`、`no_audio` 或 `aborted`，并查看 `failed_sentences`。发送成功不等于播放成功；主输出写入失败不能报告 `complete`。`request_abort()` 使旧输出失效，`abort()` 还执行资源清理。底层 SDK 正在等待的网络请求不一定能立即打断；过期结果应丢弃。

`on_audio_start` 和 `before_audio` 是供调用者接线的回调；接字幕或语音仲裁时，需要用输出编号关联完成与取消。这里没有替调用者连接导演、OBS 或直播间。

## 本次验证范围

独立模块的离线测试替换网络、SDK 和声卡，检查重试、半句丢弃、取消、播放失败、停顿控制和旧 emitter 调用兼容性。宿主测试另外检查兼容接口响应、播放完成与取消、会话收尾以及 VTS 替身交互。这些测试没有调用真实模型、打开声卡或验证 Hiyori 和整场直播；真实音色效果、设备路由及服务端兼容性需要使用者在自己的环境验收。
