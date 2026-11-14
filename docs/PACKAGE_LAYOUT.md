# 目录与旧版导入迁移

> **何时读**：要新增文件、判断某模块该放哪个子包，或要修旧式根目录导入。
> **何时不用读**：只在已有模块内改逻辑，不动文件位置。
> **复核**：下一次目录调整时；这篇记录的是 2026-11-12 那次迁移，再搬动模块就需要重写而不是追加。

2026-11-12，原来平铺在根目录的22个模块按用途分组。根目录保留`main.py`，下载后仍可运行`python main.py`，无需安装本项目或配置`PYTHONPATH`。游戏、记忆、原型接口及其示例目录保持原位。

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

这些文件主要由调用者导入，移动不会新增独立启动器。需要自己接线时，在仓库根目录的调用脚本中使用上述导入方式。现有入口仍是`python main.py`、对应的`python -m games...`及`python -m examples.prototype_contracts`；服务和设备条件见各模块指南。

## 配置和素材的位置

`core.paths.PROJECT_ROOT`标识仓库根目录。移动源码后，音量配置仍在根目录`role_volumes.json`，快脑读取的相对提示词路径仍从仓库根开始，字幕与相关素材仍从原有资源位置查找。用户无需把`.env`、音量配置、提示词或素材跟着源码搬入新目录。

这些运行资源仍由使用者自行准备；目录整理没有增加私有资源或改变公开范围。包初始化保持轻量，导入`core`、`dialogue`、`audio`、`settings`不会自动加载模型客户端或声卡。

目录迁移只调整引用和资源定位，未修改模型参数、游戏规则、发声策略或记忆处理逻辑。源码模块所需的第三方依赖仍见[依赖列表](../requirements.txt)，无依赖的演示仍见[快速开始](GETTING_STARTED.md)。
