import json
from datetime import datetime
from pathlib import Path

from memory.batch_fetch import parse_results_file
from memory.models import STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(filename):
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def collect_viewer_summary_rows(storage, session_id):
    grouped = {}
    for row in storage.list_pending_viewer_messages_for_summary(session_id):
        grouped.setdefault(row["identity_key"], []).append(row)

    rows = []
    for identity_key, items in grouped.items():
        start_id = items[0]["id"]
        end_id = items[-1]["id"]
        display_name = items[-1]["viewer_name"]  # 取该批最后一条的显示名
        row_id = f"{identity_key}|{start_id}|{end_id}"
        rows.append(
            {
                "id": row_id,
                "text": _format_viewer_summary_text(display_name, session_id, items),
                "identity_key": identity_key,
                "display_name": display_name,
                "session_id": session_id,
                "start_message_id": start_id,
                "end_message_id": end_id,
                "message_ids": [item["id"] for item in items],
            }
        )
    return rows


def collect_agent_summary_rows(storage, session_id):
    grouped = {}
    for row in storage.list_pending_agent_messages_for_summary(session_id):
        key = row["agent_name"]
        grouped.setdefault(key, []).append(row)

    rows = []
    for agent_name, items in grouped.items():
        start_id = items[0]["id"]
        end_id = items[-1]["id"]
        row_id = f"{agent_name}|{start_id}|{end_id}"
        rows.append(
            {
                "id": row_id,
                "text": _format_agent_summary_text(agent_name, session_id, items),
                "agent_name": agent_name,
                "session_id": session_id,
                "start_message_id": start_id,
                "end_message_id": end_id,
                "message_ids": [item["id"] for item in items],
            }
        )
    return rows


def apply_viewer_summary_results(storage, rows, results, processed_at=None, batch_id=None):
    processed_at = processed_at or _now_iso()
    row_map = {str(row["id"]): row for row in rows}
    for item in results:
        row_id = str(item["custom_id"]).split(":", 1)[1]
        row = row_map.get(row_id)
        if not row:
            continue
        payload = item.get("payload")
        if not payload:
            storage.mark_viewer_messages_summary_status(row["message_ids"], STATUS_FAILED, batch_id=batch_id, extracted_at=processed_at)
            continue
        should_store = bool(payload.get("should_store"))
        if not should_store:
            storage.mark_viewer_messages_summary_status(row["message_ids"], STATUS_SKIPPED, batch_id=batch_id, extracted_at=processed_at)
            continue
        summary_text = payload.get("summary_text")
        if not summary_text:
            storage.mark_viewer_messages_summary_status(row["message_ids"], STATUS_FAILED, batch_id=batch_id, extracted_at=processed_at)
            continue
        storage.add_viewer_summary(
            identity_key=row["identity_key"],
            display_name=row["display_name"],
            session_id=row["session_id"],
            start_message_id=row["start_message_id"],
            end_message_id=row["end_message_id"],
            summary_text=summary_text,
            keywords_json=json.dumps(payload.get("keywords") or [], ensure_ascii=False),
            created_at=processed_at,
        )
        storage.mark_viewer_messages_summary_status(row["message_ids"], STATUS_DONE, batch_id=batch_id, extracted_at=processed_at)


def apply_agent_summary_results(storage, rows, results, processed_at=None, batch_id=None):
    processed_at = processed_at or _now_iso()
    row_map = {str(row["id"]): row for row in rows}
    for item in results:
        row_id = str(item["custom_id"]).split(":", 1)[1]
        row = row_map.get(row_id)
        if not row:
            continue
        payload = item.get("payload")
        if not payload:
            storage.mark_agent_messages_summary_status(row["message_ids"], STATUS_FAILED, batch_id=batch_id)
            continue
        should_store = bool(payload.get("should_store"))
        if not should_store:
            storage.mark_agent_messages_summary_status(row["message_ids"], STATUS_SKIPPED, batch_id=batch_id)
            continue
        summary_text = payload.get("summary_text")
        if not summary_text:
            storage.mark_agent_messages_summary_status(row["message_ids"], STATUS_FAILED, batch_id=batch_id)
            continue
        storage.add_agent_summary(
            session_id=row["session_id"],
            agent_name=row["agent_name"],
            start_message_id=row["start_message_id"],
            end_message_id=row["end_message_id"],
            summary_text=summary_text,
            keywords_json=json.dumps(payload.get("keywords") or [], ensure_ascii=False),
            created_at=processed_at,
        )
        storage.mark_agent_messages_summary_status(row["message_ids"], STATUS_DONE, batch_id=batch_id)


def load_and_apply_viewer_summary_results(storage, rows, results_path, processed_at=None, batch_id=None):
    apply_viewer_summary_results(storage, rows, parse_results_file(results_path), processed_at=processed_at, batch_id=batch_id)


def load_and_apply_agent_summary_results(storage, rows, results_path, processed_at=None, batch_id=None):
    apply_agent_summary_results(storage, rows, parse_results_file(results_path), processed_at=processed_at, batch_id=batch_id)


def _format_viewer_summary_text(display_name, session_id, items):
    lines = [
        f"speaker_name: {display_name}",
        f"session_id: {session_id}",
        "messages:",
    ]
    lines.extend(f"- [{item['created_at']}] {item['message_text']}" for item in items)
    return "\n".join(lines)


def _format_agent_summary_text(agent_name, session_id, items):
    lines = [
        f"agent_name: {agent_name}",
        f"session_id: {session_id}",
        "messages:",
    ]
    lines.extend(f"- [{item['created_at']}] {item['message_text']}" for item in items)
    return "\n".join(lines)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")

