# 目录与旧版导入迁移

> **何时读**：要新增文件、判断某模块该放哪个子包，或要修旧式根目录导入。
> **何时不用读**：只在已有模块内改逻辑，不动文件位置。
> **复核**：下一次目录调整时；这篇记录的是 2026-11-12 那次迁移，再搬动模块就需要重写而不是追加。

2026-11-12，原来平铺在根目录的22个模块按用途分组。根目录保留`main.py`，下载后仍可运行`python main.py`，无需安装本项目或配置`PYTHONPATH`。游戏、记忆、原型接口及其示例目录保持原位。

通用入口 `public_host/` 提供配置页、节目中控、模型与语音适配和可选 VTS 嘴型，使用 `python -m public_host` 启动。节目推进与转场判断位于 `core/program/`，首个真实活动五子棋位于 `minigames/`；旧模块的目录和导入方式保持不变。私人平台/设备接线仍未包含，见[宿主指南](PUBLIC_HOST.md)和[中控指南](PUBLIC_DIRECTOR.md)。

| 新目录 | 原来的文件名（文件名本身保留） |
|---|---|
| `core/` | `event_bus.py`、`state_machine.py`、`speaker_scheduler.py`、`speech_output_arbiter.py`、`run_architecture.py` |
| `dialogue/` | `conversation.py`、`fast_brain.py`、`viewer_name.py`、`web_search.py` |
| `audio/` | `cosyvoice_tts.py`、`lumi_asr.py`、`lumi_tts.py`、`qwen3_tts.py`、`qwen_audio_tts.py`、`realtime_chat.py`、`realtime_chat_protocol.py`、`role_volume.py`、`speech_pauses.py`、`spoken_output.py`、`tts_emitter.py` |
| `settings/` | `voice_config.py`、`voice_registry.py` |

## 调用方式

旧脚本中的导入需要修改，例如：

```python
# 旧版：from event_bus import EventBus
from core.event_bus import EventBus

# 旧版：import qwen3_tts
from audio import qwen3_tts

# 旧版：from voice_config import get_speaker_config
from settings.voice_config import get_speaker_config
```

函数和类的名称、参数保持原样。按文件路径动态加载、mock或保存了模块名的调用者，也需要改为相应的全限定名，例如`audio.qwen3_tts`。根目录不再提供旧名包装模块；不要混用旧目录中的文件与新目录中的代码。

上述目录迁移本身只调整导入；新增的通用宿主提供了另一条实际运行入口。当前可按目标选择：

| 入口 | 用途 |
|---|---|
| `python -m public_host` | 本机节目中控与手动对话。手动对话的离线演示仅用标准库；默认节目中的五子棋需 aiohttp，真实语音和 VTS 使用 `requirements-host.txt` 及用户自己的服务和模型。 |
| `python main.py` | 自动运行的无声终端调度演示，无需密钥。 |
| `python -m games...` | 对应游戏的独立入口；按各游戏指南准备本体、依赖和桥接。 |
| `python -m examples.prototype_contracts` | 原型接口与离线样本，不启动真实游戏或演唱服务。 |

需要独立接线时，在仓库根目录的调用脚本中使用上面的包导入方式。公共宿主已连接五子棋；其他游戏、记忆或实时语音仍需各自适配。

## 通用宿主内部目录

| 位置 | 负责什么 |
|---|---|
| `public_host/__main__.py`、`server.py`、`web/` | 命令行启动、本机 HTTP 接口和网页；默认地址 `http://127.0.0.1:8765`。 |
| `public_host/config.py` | 校验和保存使用者自己的配置。 |
| `public_host/session.py` | 将消息、角色调度、模型、合成、播放和取消连接起来。 |
| `core/program/`、`public_host/program.py` | 共用节目推进/转场判断，以及公开宿主的语音与活动 I/O 接线。正式导演候选也调用共用层，正式实机推广尚未完成。 |
| `public_host/activities/gomoku.py`、`minigames/` | 五子棋准备、激活、确认停止；已有规则算法、计分和网页底座。aiohttp 在准备活动时加载。 |
| `public_host/providers.py`、`playback.py` | 兼容聊天/TTS 接口和实际音频播放；离线演示使用替身。 |
| `public_host/avatar.py` | 可选的 VTS 授权、模型核对和音量嘴型；不附带模型文件。 |

## 配置和素材的位置

`core.paths.PROJECT_ROOT`标识仓库根目录。移动源码后，音量配置仍在根目录`role_volumes.json`，快脑读取的相对提示词路径仍从仓库根开始，字幕与相关素材仍从原有资源位置查找。用户无需把`.env`、音量配置、提示词或素材跟着源码搬入新目录。

这些运行资源仍由使用者自行准备；目录整理没有增加私有资源。包初始化保持轻量，导入`core`、`dialogue`、`audio`、`settings`不会自动加载模型客户端或声卡。

通用宿主使用独立、已忽略的 `.local-host/`：`config.json` 保存本机配置，`vts/` 保存本插件的本机授权。可用 `--data-dir` 显式指定其他配置目录；宿主不自动读取旧 `.env`、私人直播配置或其他程序的 VTS 授权。模型由使用者在 VTS 中安装和加载，示例见 [Hiyori 适配说明](VTS_EXAMPLE.md)。

目录迁移只调整引用和资源定位，未修改模型参数、游戏规则、发声策略或记忆处理逻辑。源码模块所需的第三方依赖仍见[依赖列表](../requirements.txt)，无依赖的演示仍见[快速开始](GETTING_STARTED.md)。
