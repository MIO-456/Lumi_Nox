import json
from datetime import datetime
from pathlib import Path

from memory.batch_fetch import parse_results_file
from memory.models import FACT_ACTIVE, STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(filename):
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def collect_viewer_fact_rows(storage, session_id):
    rows = []
    for row in storage.list_pending_viewer_messages_for_fact(session_id):
        rows.append(
            {
                "id": row["id"],
                "text": _format_viewer_fact_text(row),
                "identity_key": row["identity_key"],
                "display_name": row["viewer_name"],
                "source_message_id": row["id"],
                "source_type": row["source_type"],
            }
        )
    return rows


def collect_agent_fact_rows(storage, session_id):
    rows = []
    for row in storage.list_pending_agent_messages_for_fact(session_id):
        rows.append(
            {
                "id": row["id"],
                "text": _format_agent_fact_text(row),
                "agent_name": row["agent_name"],
                "source_message_id": row["id"],
                "message_kind": row["message_kind"],
            }
        )
    return rows


def apply_viewer_fact_results(storage, results, processed_at=None, batch_id=None):
    processed_at = processed_at or _now_iso()
    for item in results:
        message_id = _parse_numeric_row_id(item["custom_id"])
        payload = item.get("payload")
        if not payload:
            storage.mark_viewer_messages_fact_status([message_id], STATUS_FAILED, batch_id=batch_id, extracted_at=processed_at)
            continue
        should_store = bool(payload.get("should_store"))
        if not should_store:
            storage.mark_viewer_messages_fact_status([message_id], STATUS_SKIPPED, batch_id=batch_id, extracted_at=processed_at)
            continue
        required = ("category", "fact_key", "fact_value")
        if any(not payload.get(field) for field in required):
            storage.mark_viewer_messages_fact_status([message_id], STATUS_FAILED, batch_id=batch_id, extracted_at=processed_at)
            continue
        # 写回键取自来源消息的 identity_key，不信 LLM 输出里的名字
        identity_key = storage.get_viewer_message_identity(message_id)
        if not identity_key:
            storage.mark_viewer_messages_fact_status([message_id], STATUS_FAILED, batch_id=batch_id, extracted_at=processed_at)
            continue
        profile = storage.get_viewer_profile(identity_key)
        display_name = (profile or {}).get("display_name") or ""
        # manual 守门：如果同三元组已有 source='manual' 的 active fact，跳过 LLM 新版本以保护人工修正
        if storage.has_manual_viewer_fact(
            identity_key=identity_key,
            category=payload["category"],
            fact_key=payload["fact_key"],
        ):
            storage.mark_viewer_messages_fact_status([message_id], STATUS_SKIPPED, batch_id=batch_id, extracted_at=processed_at)
            continue
        storage.invalidate_viewer_fact(
            identity_key=identity_key,
            category=payload["category"],
            fact_key=payload["fact_key"],
            invalidated_at=processed_at,
        )
        storage.add_viewer_fact(
            identity_key=identity_key,
            display_name=display_name,
            category=payload["category"],
            fact_key=payload["fact_key"],
            fact_value=payload["fact_value"],
            confidence=float(payload.get("confidence") or 0.0),
            source_message_id=message_id,
            status=FACT_ACTIVE,
            created_at=processed_at,
        )
        storage.mark_viewer_messages_fact_status([message_id], STATUS_DONE, batch_id=batch_id, extracted_at=processed_at)


def apply_agent_fact_results(storage, results, processed_at=None, batch_id=None, keep_limit=3):
    processed_at = processed_at or _now_iso()
    for item in results:
        message_id = _parse_numeric_row_id(item["custom_id"])
        payload = item.get("payload")
        if not payload:
            storage.mark_agent_messages_fact_status([message_id], STATUS_FAILED, batch_id=batch_id)
            continue
        should_store = bool(payload.get("should_store"))
        if not should_store:
            storage.mark_agent_messages_fact_status([message_id], STATUS_SKIPPED, batch_id=batch_id)
            continue
        required = ("agent_name", "category", "fact_key", "fact_value")
        if any(not payload.get(field) for field in required):
            storage.mark_agent_messages_fact_status([message_id], STATUS_FAILED, batch_id=batch_id)
            continue
        # manual 守门：保护人工添加的 agent fact 不被 LLM 覆盖
        if storage.has_manual_agent_fact(
            agent_name=payload["agent_name"],
            category=payload["category"],
            fact_key=payload["fact_key"],
        ):
            storage.mark_agent_messages_fact_status([message_id], STATUS_SKIPPED, batch_id=batch_id)
            continue
        storage.invalidate_agent_fact(
            agent_name=payload["agent_name"],
            category=payload["category"],
            fact_key=payload["fact_key"],
            invalidated_at=processed_at,
        )
        storage.add_agent_fact(
            agent_name=payload["agent_name"],
            category=payload["category"],
            fact_key=payload["fact_key"],
            fact_value=payload["fact_value"],
            confidence=float(payload.get("confidence") or 0.0),
            source_message_id=message_id,
            status=FACT_ACTIVE,
            created_at=processed_at,
        )
        storage.prune_agent_active_facts(
            agent_name=payload["agent_name"],
            category=payload["category"],
            keep_limit=keep_limit,
            invalidated_at=processed_at,
        )
        storage.mark_agent_messages_fact_status([message_id], STATUS_DONE, batch_id=batch_id)


def load_and_apply_viewer_fact_results(storage, results_path, processed_at=None, batch_id=None):
    apply_viewer_fact_results(storage, parse_results_file(results_path), processed_at=processed_at, batch_id=batch_id)


def load_and_apply_agent_fact_results(storage, results_path, processed_at=None, batch_id=None, keep_limit=3):
    apply_agent_fact_results(
        storage,
        parse_results_file(results_path),
        processed_at=processed_at,
        batch_id=batch_id,
        keep_limit=keep_limit,
    )


def _format_viewer_fact_text(row):
    return "\n".join(
        (
            f"viewer_name: {row['viewer_name']}",
            f"source_type: {row.get('source_type') or ''}",
            f"message_id: {row['id']}",
            f"message_text: {row['message_text']}",
        )
    )


def _format_agent_fact_text(row):
    return "\n".join(
        (
            f"agent_name: {row['agent_name']}",
            f"message_kind: {row.get('message_kind') or ''}",
            f"message_id: {row['id']}",
            f"message_text: {row['message_text']}",
        )
    )


def _parse_numeric_row_id(custom_id):
    return int(str(custom_id).split(":", 1)[1])


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")

