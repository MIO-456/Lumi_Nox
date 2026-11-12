import json
from datetime import datetime
from pathlib import Path

from memory.batch_fetch import parse_results_file
from memory.models import STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

PREFERRED_NAME_ALIASES = {
    "preferred_name",
    "preferred_nickname",
    "nickname",
    "preferred_call_name",
    "preferred_call",
    "preferred_calling",
    "call_name",
    "preferred_address",
}


def load_prompt(filename):
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def collect_viewer_fact_rows(storage, session_id):
    pending_rows = storage.list_pending_viewer_messages_for_fact(session_id)
    context_rows = storage.list_viewer_messages_for_fact_context(session_id)
    context_by_identity = {}
    for row in context_rows:
        context_by_identity.setdefault(row["identity_key"], []).append(row)
    for identity_rows in context_by_identity.values():
        identity_rows.sort(key=lambda item: int(item["id"]))

    rows = []
    for row in pending_rows:
        identity_rows = context_by_identity.get(row["identity_key"], [])
        target_index = next(
            (index for index, item in enumerate(identity_rows) if int(item["id"]) == int(row["id"])),
            None,
        )
        if target_index is None:
            window = [row]
        else:
            window = identity_rows[max(0, target_index - 2) : target_index + 3]
        rows.append(
            {
                "id": row["id"],
                "text": _format_viewer_fact_text(row, window),
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
    for item in sorted(results, key=lambda result: _parse_numeric_row_id(result["custom_id"])):
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
        category = payload["category"]
        fact_key = _normalize_viewer_fact_key(category, payload["fact_key"])
        # 写回键取自来源消息的 identity_key，不信 LLM 输出里的名字
        identity_key = storage.get_viewer_message_identity(message_id)
        if not identity_key:
            storage.mark_viewer_messages_fact_status([message_id], STATUS_FAILED, batch_id=batch_id, extracted_at=processed_at)
            continue
        profile = storage.get_viewer_profile(identity_key)
        display_name = (profile or {}).get("display_name") or ""
        try:
            outcome = storage.store_viewer_fact_if_newer(
                identity_key=identity_key,
                display_name=display_name,
                category=category,
                fact_key=fact_key,
                fact_value=payload["fact_value"],
                confidence=float(payload.get("confidence") or 0.0),
                source_message_id=message_id,
                created_at=processed_at,
            )
        except Exception:
            storage.mark_viewer_messages_fact_status(
                [message_id], STATUS_FAILED, batch_id=batch_id, extracted_at=processed_at
            )
            continue
        final_status = STATUS_DONE if outcome == "stored" else STATUS_SKIPPED
        storage.mark_viewer_messages_fact_status(
            [message_id], final_status, batch_id=batch_id, extracted_at=processed_at
        )


def apply_agent_fact_results(storage, results, processed_at=None, batch_id=None, keep_limit=3):
    processed_at = processed_at or _now_iso()
    for item in sorted(results, key=lambda result: _parse_numeric_row_id(result["custom_id"])):
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
        try:
            outcome = storage.store_agent_fact_if_newer(
                agent_name=payload["agent_name"],
                category=payload["category"],
                fact_key=payload["fact_key"],
                fact_value=payload["fact_value"],
                confidence=float(payload.get("confidence") or 0.0),
                source_message_id=message_id,
                created_at=processed_at,
                keep_limit=keep_limit,
            )
        except Exception:
            storage.mark_agent_messages_fact_status(
                [message_id], STATUS_FAILED, batch_id=batch_id
            )
            continue
        final_status = STATUS_DONE if outcome == "stored" else STATUS_SKIPPED
        storage.mark_agent_messages_fact_status(
            [message_id], final_status, batch_id=batch_id
        )


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


def _format_viewer_fact_text(target_row, window):
    target_id = int(target_row["id"])
    messages = []
    for row in window:
        messages.append(
            {
                "role": "target" if int(row["id"]) == target_id else "context",
                "message_id": int(row["id"]),
                "created_at": row.get("created_at") or "",
                "source_type": row.get("source_type") or "",
                "message_text": row["message_text"],
            }
        )
    return json.dumps(
        {
            "viewer_name": target_row["viewer_name"],
            "target_message_id": target_id,
            "messages": messages,
        },
        ensure_ascii=False,
        indent=2,
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


def _normalize_viewer_fact_key(category, fact_key):
    if category == "identity" and fact_key in PREFERRED_NAME_ALIASES:
        return "preferred_name"
    return fact_key


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")

