# 测试

> **何时读**：要新增 / 修改测试，或需要判断"这次改动算不算验证通过"。
> **何时不用读**：只改文档，或只想跑一次 `python main.py`。
> **复核**：`tests/` 增删文件、CI 配置变化、测试数不再是 88 时。

## 一条不可让步的原则

**所有测试必须离线**：不联网、不打开声卡、不启动游戏、不调用模型、不读 `.env`。
原因不是洁癖——CI 没有凭据、没有音频设备、没有游戏本体；一旦测试依赖外部世界，它就只在作者机器上通过。需要外部能力时，用替身（Fake / mock / 临时目录）把边界换掉，而不是跳过测试。

## 测试怎么分组

当前 88 项，10 个文件：

| 文件 | 用例 | 覆盖什么 | 替身方式 |
|---|---|---|---|
| `test_core_refresh.py` | 5 | 事件总线超时与迟到响应、仲裁器取消语义、观众名归一化 | 真实模块 + 慢回调 |
| `test_public_core.py` | 3 | 零安装演示、记忆衰减过滤、成员身份回填幂等 | `FakeStorage` |
| `test_package_layout.py` | 3 | 从无关工作目录跑演示、包导入保持轻量、资源根与唯一入口文件 | 子进程 + 临时 cwd |
| `test_voice_refresh.py` | 24 | TTS / ASR / 播放契约，SDK 与设备被替换 | `FakeStream`、`FakePA`、`ManualTimer` |
| `test_memory_refresh.py` | 9 | 记忆构建、检索、落盘、待处理会话 | 临时 SQLite + mock 模型边界 |
| `test_prototype_contracts.py` | 5 | 游戏 / 绘画 / 演唱样本的交接形状 | 离线 fixture |
| `test_game_refresh_buckshot.py` | 17 | 知识库、协议、生命周期 | 离线局面 |
| `test_game_refresh_kingdom_rush.py` | 14 | 启动超时、套接字生命周期、握手、决策 | `FakeSocket` |
| `test_terraria_package.py` | 4 | mod 源码完整、焦点复位、协议版本 | 源码静态检查 |
| `test_game_refresh_word_solvers.py` | 4 | 求解器工作量有上限 | 固定采样 |

定位方式：先按"改的是哪个子系统"选文件；纯调度 / 仲裁的回归放 `test_core_refresh.py`，跨模块的公开行为放 `test_public_core.py`。

## 怎么跑

```bash
python -m unittest discover -s tests -v          # 全量（-v 便于看用例名）
python -m unittest tests.test_core_refresh -v    # 单个文件
python -m unittest tests.test_core_refresh.CoreRefreshTests.test_stale_cancel_does_not_interrupt_new_speech -v
python -m compileall -q .                        # 编译检查，无输出即通过
```

`python main.py` 是被测对象之一（`test_public_core.zero_setup_demo` 会真的运行它），不要把它当成"额外的手工验证"而跳过。

## 新增测试的规则

1. **放在对应文件里**，不新建"杂项测试"文件；确需新文件时同步更新本页表格与数量。
2. **离线**：用 `unittest.mock`、自写 Fake、`tempfile.TemporaryDirectory()`。真实网络 / 设备 / 游戏一律替换。
3. **不依赖时序巧合**：不要用 `sleep` 赌竞态；用 `threading.Event`、`ManualTimer` 或可注入的时钟。
4. **不写死环境**：不假设 Windows、不假设存在 `role_volumes.json`、不假设 cwd 是仓库根。
5. **替身命名直白**：`FakeStream`、`FakeStorage` 这类名字让人一眼看出"这里没有真实现"。
6. **一个用例一个行为**：失败信息要能直接指出哪条契约破了。
7. **测试里不出现真实凭据、真实 IP、个人路径**。

## 什么算验证通过

| 结论 | 需要的证据 |
|---|---|
| "改好了" | 上述命令实际执行过，测试全绿，且新行为有对应用例 |
| "只改文档" | `python scripts/check_docs.py` 通过（链接与行数） |
| "演示可跑" | `python main.py` 输出到 `=== demo complete ===` |
| "语音 / 游戏链路可用" | **本仓库的离线测试无法证明**——需要真实凭据、设备或游戏本体，且必须在说明里写清环境 |

不要用"测试通过"掩盖"没验证真实链路"；也不要为了绿灯而放宽离线约束。

## 与 CI 的一致性

[CI](../.github/workflows/ci.yml) 在 Ubuntu / Python 3.12 上执行：

```yaml
python -m compileall -q .
python -m unittest discover -s tests -v
python scripts/check_docs.py
```

本地能过不代表 CI 能过：路径大小写、换行符、默认编码都可能只在另一种系统上暴露。涉及这些的改动，至少跑一遍全量测试。

## 失败排查

| 现象 | 先查 |
|---|---|
| `ERROR: ModuleNotFoundError` | 完整路径模块被测试无意导入；离线测试不应需要 `requirements.txt` |
| 只在 Windows 失败 | 路径分隔符、文件编码、`\r\n` |
| 偶发失败 | 时序依赖，检查是否用了 `sleep` 或真实线程调度 |
| 改了行为但测试仍绿 | 缺用例：契约没有被断言，补一条 |
