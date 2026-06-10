import json
from pathlib import Path


def make_custom_id(task_type, row_id):
    return f"{task_type}:{row_id}"


def validate_batch_jsonl(file_path):
    file_path = Path(file_path)
    total = 0
    seen = set()
    with file_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            custom_id = row.get("custom_id")
            body = row.get("body")
            if not custom_id or not isinstance(custom_id, str):
                raise ValueError(f"invalid custom_id at line {total + 1}")
            if custom_id in seen:
                raise ValueError(f"duplicate custom_id: {custom_id}")
            if not isinstance(body, dict):
                raise ValueError(f"invalid body for custom_id: {custom_id}")
            seen.add(custom_id)
            total += 1
    return total
