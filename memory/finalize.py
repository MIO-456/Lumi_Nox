"""会话结束后同步直接调 LLM 把本场 pending fact/summary 处理掉。

替代原"上传 TOS + 调 ARK 批量推理 + 下次启动 reconcile"那条链路——那条
链路实测从未跑通过（进程退出过程中 access violation 导致 jobs.json 写不
出来，job_id 丢失，reconcile 找不回结果）。

直接同步调豆包 mini chat completion：
- 没有 TOS 上传 / ARK 批量任务这些中间状态
- 已写入 SQLite 的数据就是最终结果
- 进程崩溃风险下不会丢数据

复用现有 collect_*_rows / apply_*_results 函数，跟批处理路径产出的数据
格式完全一致。
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from openai import OpenAI

from memory.batch_utils import make_custom_id
from memory.guard_facts import backfill_guard_facts
from memory.extractor import (
    apply_agent_fact_results,
    apply_viewer_fact_results,
    collect_agent_fact_rows,
    collect_viewer_fact_rows,
    load_prompt as load_extract_prompt,
)
from memory.summarizer import (
    apply_agent_summary_results,
    apply_viewer_summary_results,
    collect_agent_summary_rows,
    collect_viewer_summary_rows,
    load_prompt as load_summary_prompt,
)

load_dotenv()

MODEL_ID = "doubao-seed-2-0-mini-260215"
TEMPERATURE = 0.3
# 思考模式开启后，输出 token 配额需要给思考过程留空间，否则最终 JSON 可能被截断
MAX_TOKENS = 2000
DEFAULT_CONCURRENCY = 8
# 会话结束 / 补救脚本场景不在乎延迟，开思考模式提升 fact 提取的语义判别能力
# （单次耗时 1-2s → 5-10s，但 LLM 有空间内部推理"跨场 vs 局内"等抽象边界）
THINKING_MODE = "enabled"

_ark_client = OpenAI(
    api_key=os.getenv("ARK_API_KEY_FAST"),
    base_url="https://ark.cn-beijing.volces.com/api/v3",
)


def _call_llm(system_prompt: str, user_text: str, retries: int = 2):
    """同步调一次豆包 mini。失败重试 retries 次；最终失败返回 None。"""
    for attempt in range(retries + 1):
        try:
            resp = _ark_client.chat.completions.create(
                model=MODEL_ID,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                extra_body={"thinking": {"type": THINKING_MODE}},
            )
            content = (resp.choices[0].message.content or "").strip()
            # 偶尔模型会把 JSON 包在 ```json ... ``` 里
            if content.startswith("```"):
                content = content.strip("`")
                if content.startswith("json"):
                    content = content[4:].strip()
                content = content.strip("`").strip()
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                if attempt < retries:
                    time.sleep(0.5)
                    continue
                return None
            # 防御：LLM 偶尔会把单条记忆包成 JSON 数组（[{...}]）而非对象。
            # 下游 apply_*_results 假设是 dict 调 .get()，拿到 list 会让整个 batch
            # 因 "'list' object has no attribute 'get'" 中断。这里强制只接受 dict。
            if isinstance(parsed, list):
                # 如果数组里只有一个 dict，把它解包出来用；其它情况丢弃
                if len(parsed) == 1 and isinstance(parsed[0], dict):
                    return parsed[0]
                return None
            if not isinstance(parsed, dict):
                return None
            return parsed
        except Exception:
            if attempt < retries:
                time.sleep(1.0 + attempt)
                continue
            return None
    return None


def _process_rows_concurrent(rows, system_prompt: str, task_type: str, concurrency: int):
    if not rows:
        return []
    results = []

    def _one(row):
        payload = _call_llm(system_prompt, row["text"])
        return {"custom_id": make_custom_id(task_type, row["id"]), "payload": payload}

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(_one, row) for row in rows]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def finalize_session_memory(
    storage,
    session_id: str,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    include_agent: bool = True,
    log_fn=None,
) -> dict:
    """会话结束时同步处理指定 session 的所有 pending fact/summary，立即写入数据库。

    返回统计字典：
        {
            "viewer_fact": int,       # 处理了多少条 viewer fact 任务
            "viewer_summary": int,
            "agent_fact": int,
            "agent_summary": int,
            "elapsed_sec": float,
            "skipped": bool,           # 没有 pending 时返回 True
        }
    """
    log = log_fn or (lambda s: None)
    t0 = time.time()
    batch_id = f"sync-{int(t0)}"

    # 确定性兜底：先把本场上舰事件补成 viewer_fact，不依赖 LLM 抽取（LLM 会把
    # "开通了舰长"误判成一次性动作而漏抽）。幂等、source='manual' 受保护。
    try:
        guard_written = backfill_guard_facts(storage, session_id=session_id, log_fn=log)
    except Exception as e:
        guard_written = 0
        log(f"[记忆·上舰兜底] 失败（不影响后续）：{e}")

    viewer_fact_rows = collect_viewer_fact_rows(storage, session_id)
    viewer_summary_rows = collect_viewer_summary_rows(storage, session_id)
    agent_fact_rows = (
        collect_agent_fact_rows(storage, session_id) if include_agent else []
    )
    agent_summary_rows = (
        collect_agent_summary_rows(storage, session_id) if include_agent else []
    )

    total = (
        len(viewer_fact_rows)
        + len(viewer_summary_rows)
        + len(agent_fact_rows)
        + len(agent_summary_rows)
    )
    if total == 0:
        log("[记忆·会话结束] 没有 pending 数据，跳过")
        return {"skipped": True, "elapsed_sec": 0.0, "guard_fact": guard_written}

    log(
        f"[记忆·会话结束] 同步处理本场 pending："
        f"viewer_fact={len(viewer_fact_rows)} "
        f"viewer_summary={len(viewer_summary_rows)} "
        f"agent_fact={len(agent_fact_rows)} "
        f"agent_summary={len(agent_summary_rows)} "
        f"共 {total} 次 LLM 调用，并发 {concurrency}"
    )

    stats = {"skipped": False, "guard_fact": guard_written}

    if viewer_fact_rows:
        results = _process_rows_concurrent(
            viewer_fact_rows,
            load_extract_prompt("viewer_fact_prompt.md"),
            "viewer_fact",
            concurrency,
        )
        apply_viewer_fact_results(storage, results, batch_id=batch_id)
        stats["viewer_fact"] = len(viewer_fact_rows)

    if viewer_summary_rows:
        results = _process_rows_concurrent(
            viewer_summary_rows,
            load_summary_prompt("viewer_summary_prompt.md"),
            "viewer_summary",
            concurrency,
        )
        apply_viewer_summary_results(
            storage, viewer_summary_rows, results, batch_id=batch_id
        )
        stats["viewer_summary"] = len(viewer_summary_rows)

    if agent_fact_rows:
        results = _process_rows_concurrent(
            agent_fact_rows,
            load_extract_prompt("agent_fact_prompt.md"),
            "agent_fact",
            concurrency,
        )
        apply_agent_fact_results(storage, results, batch_id=batch_id)
        stats["agent_fact"] = len(agent_fact_rows)

    if agent_summary_rows:
        results = _process_rows_concurrent(
            agent_summary_rows,
            load_summary_prompt("agent_summary_prompt.md"),
            "agent_summary",
            concurrency,
        )
        apply_agent_summary_results(
            storage, agent_summary_rows, results, batch_id=batch_id
        )
        stats["agent_summary"] = len(agent_summary_rows)

    stats["elapsed_sec"] = time.time() - t0
    log(f"[记忆·会话结束] 同步处理完成，耗时 {stats['elapsed_sec']:.1f}s")
    return stats
