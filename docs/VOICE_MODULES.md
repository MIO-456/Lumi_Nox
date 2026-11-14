# 独立语音模块

> **何时读**：要单独调用 Qwen / CosyVoice 合成、接音频设备，或排查播放结果与取消语义。
> **何时不用读**：只跑零依赖演示——那时语音由 `demo_speak()` 替代，不会出声。
> **复核**：语音后端、依赖或相关 `.env` 变量变化时。

这批公开了 Qwen 合成与播放后端，供独立接线使用。`main.py` 仍是无声的标准库演示；现有 `audio/lumi_tts.py` 和 `core/run_architecture.py` 没有自动切换到 Qwen。这里不包含角色音色或完整直播启动器。

## 模块与条件

| 模块 | 用途与使用条件 |
|---|---|
| `audio/cosyvoice_tts.py` | 原有流式 CosyVoice，增加首帧超时与有上限的重试；需要自己的服务凭据、兼容音色和音频设备。 |
| `audio/qwen3_tts.py` | Qwen HTTP SSE 适配、整句 PCM 收集、按文本顺序播放、取消和结果回传；可单独合成而不开声卡。 |
| `audio/qwen_audio_tts.py` | 把兼容 `tts_v2` 的 Qwen-Audio 服务转换到相同 PCM 接口；必须设置自己的 `QWEN_AUDIO_TTS_WEBSOCKET_URL`，并确认模型、音色及权限匹配。SDK 默认端点并非经过验证的替代品。 |
| `audio/tts_emitter.py` | 用 `open/feed/finish/abort` 统一合成器生命周期；旧 CosyVoice 调用保持可用，新回调需后端支持。 |
| `audio/role_volume.py` | 分角色音量、PCM 增益与削波；本地设置 `role_volumes.json` 已忽略，不随源码发布。示例角色键沿用 `Lumi` / `Nox`，不代表提供真实声线。 |
| `audio/spoken_output.py`、`audio/speech_pauses.py` | 清理格式和控制标签；支持的模型可使用经过校验的停顿。只处理明确格式，不保证识别所有自然语言旁白。 |

依赖见 [requirements.txt](../requirements.txt)。音频设备路径使用 Windows 的 PyAudioWPatch；标准库演示和离线测试不要求安装这些库。移植其他平台音频输出是额外工作。

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

离线测试替换网络、SDK 和声卡，检查重试、半句丢弃、取消、播放失败、停顿控制和旧 emitter 调用兼容性。本次没有调用真实模型、打开声卡、实听或验证整场直播；真实音色效果、设备路由及服务端兼容性需要使用者在自己的环境验收。
