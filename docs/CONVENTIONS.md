# 代码与文档约定

> **何时读**：要新增模块、改动公开接口，或评审里反复出现"这个风格不统一"的分歧。
> **何时不用读**：只是修一个明显的笔误或跑测试。
> **复核**：`core/` 出现新写法时；约定被证明无法执行时（那就改这篇，而不是继续破例）。

## 语言

- **文档与注释用中文**，标识符、路径、命令、日志键名保留英文。现有模块 docstring 都是中文。
- 同一个词只用一个译法：`发言权`（floor）、`仲裁`（arbiter）、`镜像`（mirroring）、`快脑`（fast brain）。完整对照见 [GLOSSARY](GLOSSARY.md)。

## 导入与模块边界

```python
# 对：全限定包导入
from core.event_bus import EventBus
from settings.voice_config import get_speaker_config

# 错：根目录旧式导入（2026-11-12 已迁移，根目录不再提供包装模块）
from event_bus import EventBus
```

依赖方向是单向的，不要绕回去：

```
settings/  core/          只用标准库，可被任何层导入
dialogue/  audio/  memory/  games/   可依赖 core/ 与 settings/
```

- `core/`、`settings/` **禁止** import `dialogue/`、`audio/`、`games/`、`memory/`。这条边界是零依赖演示与离线测试的前提。
- 包 `__init__.py` 保持轻量：import 包不得自动加载模型客户端或声卡。
- 新增文件时放在所属子包内，不在根目录新增模块。位置选择见 [PACKAGE_LAYOUT](PACKAGE_LAYOUT.md)。

## 数据结构与类型

- 有状态的协作对象优先用 `@dataclass`，可变默认值一律 `field(default_factory=...)`：

```python
@dataclass
class SpeakerScheduler:
    active_speakers: list
    _next_idx: int = 0
    _input_queue: deque = field(default_factory=deque)
    _queue_lock: threading.Lock = field(default_factory=threading.Lock)
```

- 公开函数写类型注解；返回"可能没有"时用 `Optional[...]` 并让调用方显式处理 `None`。
- 需要前向引用时用 `from __future__ import annotations`（见 `core/speech_output_arbiter.py`），不要靠字符串注解混搭。

## 并发

- 共享可变结构用 `threading.Lock` 保护，锁的粒度贴在数据结构上（如 `_queue_lock`），不要用一个全局大锁。
- 回调可能跑在后台 worker 上：请求 / 响应类订阅者由 daemon worker 执行，普通 `publish()` 仍是同步的。改这部分前先读 `core/event_bus.py` 的超时与 `event.is_active` 语义。
- 超时只表示"不再等结果"，**不会中断**已经开始的回调，也不会撤销它的副作用。注释和文档里不要写成"超时会取消任务"。
- 不在持锁时调用用户回调——容易死锁。

## 错误处理与降级

- 订阅者抛异常不能拖垮广播：捕获后用 `logger.exception(...)` 记录，继续派发（见 `core/event_bus.py`）。
- 可选能力缺失时**降级而不是崩溃**：`settings/voice_registry.py` 读不到声线配置就回退占位值，这是有意设计。
- 日志沿用现有形式：`logger = logging.getLogger("<模块名>")`，消息用中文并带上下文（事件类型、来源、关联 id）。
- 不要 `except Exception: pass`。要么处理，要么记录，要么让它抛。

## 占位实现要显式

公开演示需要真实模块 + 占位替身同时存在，替身必须一眼可辨：

- 类名 / 函数名带 `Demo`：`DemoBrain`、`demo_speak()`
- docstring 明确写"这不是生产实现，真实实现在 X"
- 不要给占位实现加"看起来像真的"的参数或分支

## 命名

| 对象 | 风格 | 例子 |
|---|---|---|
| 模块 / 包 | `snake_case` | `speaker_scheduler.py` |
| 类 | `PascalCase` | `SpeechOutputArbiter` |
| 函数 / 变量 | `snake_case` | `detect_addressed_speaker` |
| 常量 | `UPPER_SNAKE` | `POLICY_QUEUE` |
| 私有成员 | 前缀下划线 | `_input_queue`、`_next_idx` |
| 策略 / 状态枚举 | `UPPER_SNAKE` 值 | `State.CHATTING`、`POLICY_INTERRUPT` |

角色名只有 `Lumi` 与 `Nox` 两个；示例角色是占位，不代表真实人设。

## 注释与文档

- 模块顶部 docstring 说明"这个模块解决什么问题、边界在哪"，必要时引用内部 spec 章节（如"spec 第 3 节"）。**不要**为了让注释看起来完整而编造编号。
- 行为改变时同步改注释与专题文档，三者（代码 / 注释 / 文档）不一致时以代码为准，但必须当场修文档。
- 文档写法见 [docs/README](README.md#写作规则)：一篇一主题、给命令、标注适用范围。

## 提交

- 一次一个主题；不夹带无关重构或整文件格式化（见 [CONTRIBUTING](../CONTRIBUTING.md)）。
- 提交信息用 `type: 摘要` 或 `type(scope): 摘要`，与现有历史一致，例如 `refactor: group public modules by responsibility`、`feat(core): refresh request lifecycle and memory consistency`。
- 提交前跑 [TESTING](TESTING.md) 里的验证命令，并在 PR 描述里写出实际执行的命令与结果。
