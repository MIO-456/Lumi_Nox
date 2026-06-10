import json
from pathlib import Path

from memory.batch_utils import make_custom_id


DEFAULT_MAX_TOKENS = 500
DEFAULT_TEMPERATURE = 0.3


def build_batch_file(rows, prompt_text, task_type, out_dir, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{task_type}_batch_input.jsonl"

    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            request = {
                "custom_id": make_custom_id(task_type, row["id"]),
                "body": {
                    "messages": [
                        {"role": "system", "content": prompt_text},
                        {"role": "user", "content": row["text"]},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "thinking": {"type": "disabled"},
                },
            }
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")

    return output_path
