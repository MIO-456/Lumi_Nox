import json
from datetime import datetime
from pathlib import Path

from memory.batch_build import build_batch_file
from memory.batch_fetch import download_batch_outputs, get_job_status
from memory.batch_submit import submit_batch_file
from memory.extractor import (
    collect_agent_fact_rows,
    collect_viewer_fact_rows,
    load_and_apply_agent_fact_results,
    load_and_apply_viewer_fact_results,
    load_prompt as load_extract_prompt,
)
from memory.identity import BILI_PREFIX
from memory.retrieval import build_memory_context
from memory.storage import MemoryStorage
from memory.summarizer import (
    collect_agent_summary_rows,
    collect_viewer_summary_rows,
    load_and_apply_agent_summary_results,
    load_and_apply_viewer_summary_results,
    load_prompt as load_summary_prompt,
)

DEFAULT_BATCH_MODEL_NAME = "doubao-seed-2-0-mini"
DEFAULT_BATCH_MODEL_VERSION = "260215"


class MemoryRuntime:
    def __init__(self, db_path="memory/memory.db"):
        self.storage = MemoryStorage(db_path)
        self.storage.init_db()

    def on_viewer_message(self, identity_key, display_name, source_type, text,
                          session_id=None, created_at=None):
        created_at = created_at or _now_iso()
        # 自动认领：带 uid 的发言（bili:），若同名 legacy 记忆恰好唯一匹配，则归并
        if str(identity_key).startswith(BILI_PREFIX) and display_name:
            matches = self.storage.find_legacy_identities_by_name(display_name)
            if len(matches) == 1 and matches[0] != identity_key:
                report = self.storage.claim_legacy_identity(matches[0], identity_key, display_name)
                self._log_claim_report(report)
        self.storage.upsert_viewer_profile(
            identity_key=identity_key,
            display_name=display_name,
            source_type=source_type,
            session_id=session_id,
            created_at=created_at,
        )
        return self.storage.insert_viewer_message(
            session_id=session_id,
            identity_key=identity_key,
            display_name=display_name,
            source_type=source_type,
            message_text=text,
            created_at=created_at,
        )

    def _log_claim_report(self, report):
        print(
            f"[记忆·认领] {report['legacy_key']} → {report['target']}："
            f"messages={report.get('messages', 0)} facts={report.get('facts', 0)} "
            f"summaries={report.get('summaries', 0)}"
        )

    def on_agent_reply(self, agent_name, text, session_id=None, created_at=None, user_input=None):
        created_at = created_at or _now_iso()
        message_text = _format_agent_turn_text(user_input=user_input, agent_reply=text)
        return self.storage.insert_agent_message(
            session_id=session_id,
            agent_name=agent_name,
            message_text=message_text,
            message_kind="turn",
            created_at=created_at,
        )

    def build_fast_brain_memory_prompt(self, identity_key, agent_name, current_input):
        return build_memory_context(
            self.storage,
            identity_key=identity_key,
            agent_name=agent_name,
            current_input=current_input,
        )

    def run_session_end_batch(self, session_id):
        return self.prepare_session_end_batches(session_id)

    def prepare_session_end_batches(self, session_id, out_dir=None):
        out_dir = Path(out_dir or Path(self.storage.db_path).resolve().parent / "batches" / session_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        viewer_fact_rows = collect_viewer_fact_rows(self.storage, session_id)
        viewer_summary_rows = collect_viewer_summary_rows(self.storage, session_id)
        agent_fact_rows = collect_agent_fact_rows(self.storage, session_id)
        agent_summary_rows = collect_agent_summary_rows(self.storage, session_id)

        return {
            "viewer_fact_extraction": _build_task_manifest(
                rows=viewer_fact_rows,
                prompt_text=load_extract_prompt("viewer_fact_prompt.md"),
                task_type="viewer_fact",
                out_dir=out_dir,
            ),
            "viewer_summary_generation": _build_task_manifest(
                rows=viewer_summary_rows,
                prompt_text=load_summary_prompt("viewer_summary_prompt.md"),
                task_type="viewer_summary",
                out_dir=out_dir,
            ),
            "agent_fact_extraction": _build_task_manifest(
                rows=agent_fact_rows,
                prompt_text=load_extract_prompt("agent_fact_prompt.md"),
                task_type="agent_fact",
                out_dir=out_dir,
            ),
            "agent_summary_generation": _build_task_manifest(
                rows=agent_summary_rows,
                prompt_text=load_summary_prompt("agent_summary_prompt.md"),
                task_type="agent_summary",
                out_dir=out_dir,
            ),
        }

    def submit_session_end_batches(
        self,
        session_id,
        out_dir=None,
        model_name=DEFAULT_BATCH_MODEL_NAME,
        model_version=DEFAULT_BATCH_MODEL_VERSION,
    ):
        out_dir = Path(out_dir or Path(self.storage.db_path).resolve().parent / "batches" / session_id)
        manifests = self.prepare_session_end_batches(session_id=session_id, out_dir=out_dir)
        jobs = {
            "session_id": session_id,
            "created_at": _now_iso(),
            "model_name": model_name,
            "model_version": model_version,
            "tasks": [],
        }
        for task_name, manifest in manifests.items():
            task_record = {
                "task_name": task_name,
                "count": manifest["count"],
                "rows": manifest["rows"],
                "input_path": manifest["input_path"],
                "job_id": None,
                "status": "skipped" if manifest["count"] == 0 else "pending_submit",
            }
            if manifest["count"] > 0:
                input_key = f"memory/{session_id}/{task_name}/batch_input.jsonl"
                output_key = f"memory/{session_id}/{task_name}/"
                try:
                    job_id = submit_batch_file(
                        local_input=manifest["input_path"],
                        input_object_key=input_key,
                        output_object_key=output_key,
                        model_name=model_name,
                        model_version=model_version,
                        job_name=f"memory-{task_name}-{session_id}",
                    )
                    task_record["job_id"] = job_id
                    task_record["status"] = "submitted"
                    task_record["results_path"] = str(out_dir / f"{task_name}_results.jsonl")
                    task_record["errors_path"] = str(out_dir / f"{task_name}_errors.jsonl")
                    task_record["output_object_key"] = output_key
                except Exception as e:
                    task_record["status"] = "submit_failed"
                    task_record["error"] = str(e)
            jobs["tasks"].append(task_record)

        jobs_path = out_dir / "jobs.json"
        jobs_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
        return jobs_path

    def reconcile_pending_batch_jobs(self, root_dir=None):
        root_dir = Path(root_dir or Path(self.storage.db_path).resolve().parent / "batches")
        if not root_dir.exists():
            return []

        applied = []
        for jobs_path in root_dir.glob("**/jobs.json"):
            payload = json.loads(jobs_path.read_text(encoding="utf-8"))
            changed = False
            for task in payload.get("tasks", []):
                if task.get("status") != "submitted" or not task.get("job_id"):
                    continue
                job = get_job_status(task["job_id"])
                phase = getattr(getattr(job, "status", None), "phase", "")
                task["remote_phase"] = phase
                changed = True
                if phase == "Completed":
                    try:
                        download_batch_outputs(
                            job_id=task["job_id"],
                            output_prefix=task["output_object_key"],
                            results_path=task["results_path"],
                            errors_path=task["errors_path"],
                        )
                        self._apply_task_results(task)
                        task["status"] = "applied"
                        task["applied_at"] = _now_iso()
                        applied.append(task["task_name"])
                    except Exception as e:
                        task["status"] = "apply_failed"
                        task["error"] = str(e)
                elif phase in ("Failed", "Terminated"):
                    task["status"] = phase.lower()
            if changed:
                jobs_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return applied

    def _apply_task_results(self, task):
        results_path = task["results_path"]
        rows = task.get("rows") or []
        task_name = task["task_name"]
        batch_id = task.get("job_id")

        if task_name == "viewer_fact_extraction":
            load_and_apply_viewer_fact_results(self.storage, results_path, batch_id=batch_id)
            return
        if task_name == "agent_fact_extraction":
            load_and_apply_agent_fact_results(self.storage, results_path, batch_id=batch_id)
            return
        if task_name == "viewer_summary_generation":
            load_and_apply_viewer_summary_results(self.storage, rows, results_path, batch_id=batch_id)
            return
        if task_name == "agent_summary_generation":
            load_and_apply_agent_summary_results(self.storage, rows, results_path, batch_id=batch_id)
            return
        raise ValueError(f"unknown task_name: {task_name}")


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _format_agent_turn_text(user_input, agent_reply):
    user_input = (user_input or "").strip()
    agent_reply = (agent_reply or "").strip()
    if user_input:
        return f"user_input: {user_input}\nagent_reply: {agent_reply}"
    return f"agent_reply: {agent_reply}"


def _build_task_manifest(rows, prompt_text, task_type, out_dir):
    if not rows:
        return {"count": 0, "rows": [], "input_path": None}
    input_path = build_batch_file(
        rows=rows,
        prompt_text=prompt_text,
        task_type=task_type,
        out_dir=out_dir,
    )
    return {
        "count": len(rows),
        "rows": rows,
        "input_path": str(input_path),
    }
