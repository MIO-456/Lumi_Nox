"""Public I/O adapter for the same program decisions used by the live director."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta
import importlib.util
import queue
import threading
import time
import uuid

from core.program import (TransitionHooks, activation_accepted, clock_snapshot,
                          finish_program, next_decision, preparation_accepted,
                          run_transition, stop_confirmed, tick_decision)
from core.state_machine import State
from public_host.config import validate_program
from public_host.providers import Cancelled, ProviderError


def activity_catalog():
    available = importlib.util.find_spec("aiohttp") is not None
    return [{"id": "gomoku", "name": "五子棋", "roles_required": 2,
             "available": available,
             "unavailable_reason": "" if available else "请安装 requirements-host.txt 中的 aiohttp。",
             "description": "两个角色自动对弈，使用独立本机网页。需要 aiohttp。"}]


def make_activity(bus, roles, port):
    try:
        from public_host.activities.gomoku import GomokuActivity
    except ImportError as exc:
        raise ProviderError("五子棋依赖未安装，请安装 requirements-host.txt。") from exc
    return GomokuActivity(bus, roles, port=port)


class Program:
    """One director worker owns transitions; HTTP methods only signal commands."""
    def __init__(self, config, session, *, activity_factory=None, interval=0.05):
        self.config = deepcopy(config)
        self.settings = self.config["program"]
        self.roles = [r["name"] for r in self.config["roles"]]
        validate_program(self.settings, roles=self.roles)
        self.session = session
        # Session owns the one public bus/state/arbiter. Managed Session leaves
        # all lifecycle transitions to this worker; do not create a shadow bus.
        self.bus = session.bus
        self.state_machine = session.state_machine
        self._activity_factory = activity_factory or make_activity
        self._interval = interval
        self._lock = threading.RLock()
        self._commands = queue.Queue()
        self._inbox = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._activity = None
        self._activity_live = False
        self._scope = None
        self._index = -1
        self._started_at = None
        self._flow_state = "IDLE"
        self._phase = "idle"
        self._error = ""
        self._blocked = False
        self._events = deque(maxlen=200)
        self._event_seq = 0
        self._speech = {}
        self._stage_request = None
        self._auto_request = None
        self._next_auto = 0.0
        self._revision = 0
        self._instruction_callback = None
        self._finalized = False
        self._final_result = None

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise ProviderError("节目已经开始。")
            self._phase = "preparing"
            self._thread = threading.Thread(target=self._run, name="public-program", daemon=True)
            self._thread.start()

    def next(self):
        self._commands.put("next")

    def end(self):
        self._commands.put("end")

    def stop(self):
        self._stop.set()
        # Invalidate queued output immediately, even if prepare/stop is waiting.
        self.session.stop()
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._phase = "stopping"
            elif self._blocked:
                self._thread = threading.Thread(target=self._cleanup, name="public-program-cleanup", daemon=True)
                self._thread.start()

    def submit(self, text, speaker=None, *, speech=False):
        with self._lock:
            if self._phase not in ("opening", "chat", "activity", "ending") or self._stop.is_set():
                raise ProviderError("当前正在准备或清理环节，请稍候。")
            scope = self._scope
        submit = self.session.submit_speech if speech else self.session.submit_text
        return submit(text, speaker, scope=scope, source="manual")

    def snapshot(self):
        with self._lock:
            stage = self.settings["stages"][self._index] if self._index >= 0 else None
            clock = self._clock()
            result = {"running": bool(self._thread and self._thread.is_alive()),
                      "blocked": self._blocked, "phase": self._phase,
                      "stage_index": self._index, "stage": deepcopy(stage),
                      "stages": [{"id": s["id"], "title": s["title"], "kind": s["kind"]}
                                 for s in self.settings["stages"]],
                      "remaining_seconds": clock["remaining_sec"],
                      "last_error": self._error, "events": deepcopy(list(self._events)),
                      "event_seq": self._event_seq,
                      "activity": {}}
            activity = self._activity
        if activity:
            result["activity"] = activity.snapshot()
        return result

    def _clock(self):
        stage = self.settings["stages"][self._index] if self._index >= 0 else {}
        return clock_snapshot([s["duration"] for s in self.settings["stages"]],
                              started_at=self._started_at, current_index=self._index,
                              state=self._flow_state, revision=self._revision,
                              name=stage.get("title", ""), activity_id=stage.get("activity", ""))

    def _record(self, kind, **data):
        with self._lock:
            self._event_seq += 1
            self._events.append({"seq": self._event_seq, "event_type": kind,
                                 "data": data, "timestamp": time.time()})

    def _check(self):
        if self._stop.is_set():
            raise Cancelled()
        snapshot = self.session.snapshot()
        if not snapshot["running"] or snapshot["stopping"]:
            raise ProviderError(snapshot["last_error"] or "语音会话已经停止，节目无法继续。")

    def _safe_error(self, exc):
        if isinstance(exc, ProviderError):
            return str(exc)
        if self._activity:
            error = self._activity.snapshot().get("last_error")
            if error:
                return error
        return self.session._safe_error(exc)

    def _wait_session_ready(self):
        while self.session.snapshot()["phase"] == "starting":
            self._check()
            self._stop.wait(self._interval)
        self._check()

    def _run(self):
        try:
            self.state_machine.transition_to(State.OPENING)
            self._wait_session_ready()
            self._transition(0)
            while not self._stop.is_set() and self._flow_state != "FINISHED":
                self._check()
                self._drain_instructions()
                self._poll_results()
                if self._activity_live:
                    clock = self._clock()
                    self.bus.publish("segment_clock", clock, source="public_program")
                    if not self._finalized and clock["remaining_sec"] is not None and clock["remaining_sec"] <= 10:
                        self._finalize_activity()
                try:
                    command = self._commands.get_nowait()
                except queue.Empty:
                    command = None
                if command == "end":
                    if self._index != len(self.settings["stages"]) - 1:
                        self._transition(len(self.settings["stages"]) - 1)
                elif command == "next":
                    decision = next_decision(state=self._flow_state, current_index=self._index,
                                             segment_count=len(self.settings["stages"]))
                    self._apply(decision)
                elif self._stage_request is None:
                    decision = tick_decision(self._clock(), len(self.settings["stages"]),
                                             state=self._flow_state)
                    self._apply(decision)
                if self._flow_state == "FINISHED":
                    break
                self._auto_chat()
                if self._activity_live:
                    status = self._activity.snapshot()
                    if status.get("last_error"):
                        raise ProviderError(status["last_error"])
                self._stop.wait(self._interval)
        except Cancelled:
            pass
        except Exception as exc:
            with self._lock:
                self._error = self._safe_error(exc)
            self._record("program_failed", error=self._error)
        finally:
            self._cleanup()

    def _apply(self, decision):
        if decision.kind == "transition":
            self._transition(decision.target)
        elif decision.kind == "finish":
            # Exact opening/ending speech must have completed before tick reaches here.
            self._flow_state = "FINISHED"

    def _detach_instructions(self):
        if self._instruction_callback:
            self.bus.unsubscribe("director_instruction", self._instruction_callback)
            self._instruction_callback = None

    def _close_stage(self):
        self._detach_instructions()
        if self._scope:
            self.session.cancel_scope(self._scope)
        self._stage_request = self._auto_request = None
        self._poll_results()
        if self._activity_live:
            receipt = self._activity.stop(reason="stage_finished")
            if not stop_confirmed(receipt, "closed"):
                raise ProviderError("五子棋尚未确认停止，不能进入下一环节。")
            self._activity_live = False
        # A cancelled provider may still be returning. No new stage can speak
        # until the single speech owner has acknowledged its old work.
        while True:
            self._check()
            snapshot = self.session.snapshot()
            if snapshot["phase"] in ("ready", "idle"):
                break
            self._stop.wait(self._interval)

    def _finalize_activity(self):
        """Freeze the same timed match as production, then speak its real score."""
        if not self._activity_live or self._finalized:
            return
        self._check()
        with self._lock:
            self._phase = "finalizing"
        self._detach_instructions()
        if self._scope:
            self.session.cancel_scope(self._scope)
        self._scope = uuid.uuid4().hex
        clock = self._clock()
        self.bus.publish("activity_finalizing", {**clock, "reason": "schedule_finalizing"},
                         source="public_program")
        snapshot = self._activity.snapshot()
        result = snapshot.get("result", {})
        scores = result.get("scores", {})
        if (result.get("phase") != "match_over" or result.get("session_id") != snapshot.get("session_id")
                or result.get("segment_index") != self._index
                or result.get("clock_revision") != self._revision
                or set(scores) != set(self.roles)
                or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in scores.values())):
            raise ProviderError("五子棋没有返回与当前环节匹配的整场比分，无法完成结算。")
        first, second = self.roles
        winner = None if scores[first] == scores[second] else max(self.roles, key=scores.get)
        if result.get("winner") != winner or result.get("draw") is not (winner is None):
            raise ProviderError("五子棋整场胜者与比分不一致，无法完成结算。")
        self._final_result = deepcopy(result)
        prompt = (f"五子棋整场结束，最终比分：{first} {scores[first]} 比 {scores[second]} {second}。"
                  + ("双方平局。" if winner is None else f"获胜者是{winner}。")
                  + "请按这个真实结果简短总结，并告诉观众接下来继续节目；不要虚构对局细节。")
        request_id = self.session.submit_text(prompt, scope=self._scope, source="activity_result")
        while True:
            self._check()
            self._poll_results()
            terminal = self.session.result(request_id, consume=True)
            if terminal is not None:
                if terminal["outcome"] not in ("complete", "simulated"):
                    raise ProviderError(terminal.get("error") or "五子棋整场结算发言没有完成。")
                self._finalized = True
                self._record("match_finalized", result=result, speech_outcome=terminal["outcome"])
                return
            self._stop.wait(self._interval)

    def _transition(self, target):
        stage = self.settings["stages"][target]
        new_scope = uuid.uuid4().hex
        target_state = {"opening": State.OPENING, "chat": State.CHATTING,
                        "activity": State.PLAYING_MINIGAME, "ending": State.ENDING}[stage["kind"]]

        def enter():
            if self._index >= 0:
                previous = self.settings["stages"][self._index]
                self.state_machine.transition_to(State.TRANSITIONING, metadata={
                    "from": self.state_machine.state.value, "to": target_state.value,
                    "from_game_id": previous.get("activity", ""),
                    "game_id": stage.get("activity", ""), "stage_id": stage["id"],
                    "stage_index": target, "previous_stage_id": previous["id"]})
            with self._lock:
                self._phase = "preparing"
            self._record("stage_preparing", index=target, title=stage["title"])

        def prepare():
            if stage["kind"] == "activity":
                if self._activity is None:
                    self._activity = self._activity_factory(self.bus, self.roles,
                                                            self.settings.get("gomoku_port", 8791))
                receipt = self._activity.prepare(cancel_event=self._stop)
                if not preparation_accepted(receipt) or receipt.get("status") != "ready":
                    raise ProviderError("五子棋没有完成准备。")

        def commit():
            with self._lock:
                self._index, self._scope = target, new_scope
                # Preparation/old cleanup never consumes the new segment's time.
                self._started_at = datetime.now() - timedelta(
                    seconds=sum(s["duration"] for s in self.settings["stages"][:target]))
                self._revision += 1
                self._flow_state = "RUNNING"
                self._finalized, self._final_result = False, None

        def activate():
            if stage["kind"] == "activity":
                callback = lambda event: self._inbox.put((new_scope, deepcopy(event.data)))
                self._instruction_callback = callback
                self.bus.subscribe("director_instruction", callback)
                # Mark owned before activation: a failed activation still needs cleanup.
                self._activity_live = True
                receipt = self._activity.activate(cancel_event=self._stop)
                if not activation_accepted(receipt):
                    raise ProviderError("五子棋没有确认开始运行。")
                self.bus.publish("segment_clock", self._clock(), source="public_program")

        def complete():
            if self.state_machine.state != target_state:
                metadata = None
                if target_state == State.PLAYING_MINIGAME:
                    activity = self._activity.snapshot()
                    metadata = {"game_id": stage["activity"], "stage_id": stage["id"],
                                "stage_index": target,
                                "instance_id": activity.get("instance_id", activity.get("session_id", ""))}
                self.state_machine.transition_to(target_state, metadata=metadata)
            with self._lock:
                self._phase = stage["kind"]
            self._next_auto = time.monotonic()
            if stage["kind"] in ("opening", "ending"):
                self._stage_request = self.session.submit_text(stage["prompt"], scope=new_scope,
                                                               source=stage["kind"])
            self._record("stage_started", index=target, title=stage["title"], scope=new_scope)
            return True

        def failed(exc):
            raise exc

        def current():
            self._check()
            return True

        result = run_transition(TransitionHooks(before=self._finalize_activity, enter=enter, stop=self._close_stage,
                    prepare=prepare, commit=commit, activate=activate, complete=complete,
                    failed=failed, current=current))
        if not result:
            raise Cancelled()

    def _drain_instructions(self):
        while True:
            try:
                scope, data = self._inbox.get_nowait()
            except queue.Empty:
                break
            if scope != self._scope or self._phase != "activity" or self._stop.is_set():
                continue
            text, speaker = data.get("text"), data.get("speaker")
            request_id = self.session.submit_text(text, speaker, scope=scope, source="activity")
            self._speech[request_id] = scope
            self.bus.publish("director_instruction_dispatched", {
                "original_text": text, "speaker": speaker, "task_id": request_id,
                "instruction_id": data.get("instruction_id")}, source="public_program")

    def _poll_results(self):
        for request_id in list(self._speech):
            result = self.session.result(request_id, consume=True)
            if result is None:
                continue
            self._speech.pop(request_id)
            if result["outcome"] == "complete":
                self.bus.publish("tts_done", {"task_id": request_id, "output_id": request_id},
                                 source="public_program")
            elif result["outcome"] == "simulated":
                self.bus.publish("tts_simulated", {"task_id": request_id}, source="public_program")
            else:
                self.bus.publish("speech_task_terminal", {"task_id": request_id,
                                 "status": result["outcome"], "error": result.get("error", "")},
                                 source="public_program")
        if self._stage_request:
            result = self.session.result(self._stage_request, consume=True)
            if result is not None:
                self._stage_request = None
                if result["outcome"] not in ("complete", "simulated"):
                    raise ProviderError(result.get("error") or "开场或收尾发言未完成，节目已停止。")
        if self._auto_request:
            result = self.session.result(self._auto_request, consume=True)
            if result is not None:
                self._auto_request = None
                self._next_auto = time.monotonic() + self.settings["auto_chat_interval"]
                if result["outcome"] == "failed":
                    raise ProviderError(result.get("error") or "自动聊天失败。")

    def _auto_chat(self):
        if self._phase != "chat" or self._auto_request or time.monotonic() < self._next_auto:
            return
        snapshot = self.session.snapshot()
        if snapshot["phase"] != "ready" or snapshot["queue_size"]:
            return
        stage = self.settings["stages"][self._index]
        self._auto_request = self.session.submit_text(stage["prompt"], scope=self._scope, source="auto")

    def _cleanup(self):
        with self._lock:
            self._phase = "stopping"
        self._detach_instructions()
        self._stage_request = self._auto_request = None
        self.session.stop()

        def close_active():
            if self._activity:
                if self._activity_live:
                    receipt = self._activity.stop(reason="program_stopped")
                    if not stop_confirmed(receipt, "closed"):
                        raise ProviderError("五子棋仍在清理，停止尚未完成。")
                    self._activity_live = False
                self._activity.close()
            return True

        def drain():
            while self.session.snapshot()["running"]:
                time.sleep(self._interval)
            self._poll_results()
            return True

        def complete():
            # Retain the actual running/transition state on cleanup failure.
            # Only a confirmed activity close AND drained speech may reach IDLE.
            state = self.state_machine.state
            if state not in (State.IDLE, State.CHATTING, State.TRANSITIONING, State.ENDING):
                self.state_machine.transition_to(State.TRANSITIONING, metadata={
                    "from": state.value, "to": State.ENDING.value,
                    "game_id": self.state_machine.metadata.get("game_id", ""),
                    "reason": "program_cleanup"})
            if self.state_machine.state not in (State.IDLE, State.ENDING):
                self.state_machine.transition_to(State.ENDING)
            if self.state_machine.state == State.ENDING:
                self.state_machine.transition_to(State.IDLE)
            with self._lock:
                self._blocked = False
                self._phase = "failed" if self._error else "finished" if self._flow_state == "FINISHED" else "stopped"
            self._record("program_stopped", outcome=self._phase)

        try:
            if not finish_program(close_active, drain, complete):
                raise ProviderError("节目资源尚未完成清理，请重试停止。")
        except Exception as exc:
            with self._lock:
                self._blocked = True
                self._phase = "cleanup_failed"
                self._error = self._safe_error(exc)
            self._record("cleanup_failed", error=self._error)
