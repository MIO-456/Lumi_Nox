"""A single-worker public host, independent of private livestream configuration."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import math
import threading
import time
import uuid

from core.event_bus import EventBus
from core.speaker_scheduler import SpeakerScheduler
from core.speech_output_arbiter import POLICY_DROP, SpeechOutputArbiter
from core.state_machine import State, StateMachine
from public_host.providers import Cancelled, ProviderError
from public_host.telemetry import RequestTrace


@dataclass(frozen=True)
class _Request:
    request_id: str
    kind: str
    text: str
    speaker: str | None
    scope: str | None = None
    source: str = "manual"


class Session:
    """Own the queue, output identities and lifetime of one public session.

    Dependencies do their blocking work on our worker. They receive a
    threading.Event and must bound network waits; cancellation remains pending
    until they return. A stopped worker can never be replaced while still alive.
    """

    def __init__(self, config, *, chat, synth, player, avatar_factory=None, managed=False):
        self._config = deepcopy(config)
        roles = self._config.get("roles", [])
        names = [role.get("name") for role in roles]
        if not names or any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("至少需要一个有名称的角色。")
        if len(set(names)) != len(names):
            raise ValueError("角色名称不能重复。")
        self._roles = {role["name"]: role for role in roles}
        self._chat, self._synth, self._player = chat, synth, player
        self._avatar_factory = avatar_factory
        self._managed = managed
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._thread = None
        self._stop_event = threading.Event()
        self._stopping = False
        self._generation = 0
        self._turn_counter = 0
        self._pending = deque()
        self._current = None
        self._cancel = None
        self._output_id = None
        self._speaker = None
        self._phase = "idle"
        self._last_error = ""
        self._last_outcome = ""
        self._messages = deque(maxlen=100)
        self._events = deque(maxlen=200)
        self._results = {}
        self._trace = RequestTrace()
        self._run_id = ""
        self._event_seq = 0
        self._audio_level = 0.0
        self._cancelled_scopes = set()
        # Each entry is one completed conversation turn, including partner notes.
        self._histories = {name: deque(maxlen=20) for name in names}
        self._avatars = {}
        self.bus = EventBus()
        self.state_machine = StateMachine(self.bus)
        self.scheduler = SpeakerScheduler(names)
        self.arbiter = SpeechOutputArbiter(event_bus=self.bus)
        for event_type in (
            "state_changed", "speech_output_started", "speech_output_done",
            "speech_output_failed", "speech_output_cancelled", "session_started",
            "session_stopping", "session_stopped", "session_failed", "turn_queued",
            "turn_started", "turn_completed", "turn_simulated", "turn_cancelled",
            "turn_failed", "turn_phase",
        ):
            self.bus.subscribe(event_type, self._record_event)

    def start(self):
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                raise ProviderError("会话仍在运行或停止中，请等待结束。")
            self._generation += 1
            generation = self._generation
            self._stop_event = threading.Event()
            self._stopping = False
            self._pending.clear()
            self._messages.clear()
            self._events.clear()
            self._results.clear()
            self._trace = RequestTrace()
            self._run_id = uuid.uuid4().hex
            self._event_seq = 0
            self._audio_level = 0.0
            self._cancelled_scopes.clear()
            for history in self._histories.values():
                history.clear()
            self.scheduler.reset_rotation()
            self._last_error = self._last_outcome = ""
            self._phase = "starting"
            if not self._managed:
                self.state_machine.transition_to(State.OPENING)
            self._thread = threading.Thread(
                target=self._run, args=(generation, self._stop_event),
                name=f"public-host-{generation}", daemon=True,
            )
            self._thread.start()
            return self.snapshot()

    def stop(self):
        """Request cancellation immediately; cleanup remains owned by the worker."""
        with self._condition:
            if self._thread is None or not self._thread.is_alive():
                return self.snapshot()
            self._stopping = True
            self._phase = "stopping"
            self._stop_event.set()
            self._cancel_pending()
            self._cancel_current("session_stopped")
            self._publish("session_stopping", {})
            self._condition.notify_all()
            return self.snapshot()

    def interrupt(self):
        """Cancel only the current turn, preserving queued inputs."""
        with self._condition:
            self._cancel_current("user_interrupted")
            self._condition.notify_all()
            return self.snapshot()

    def submit_text(self, text, speaker=None, *, scope=None, source="manual"):
        return self._submit("chat", text, speaker, scope, source)

    def submit_speech(self, text, speaker, *, scope=None, source="manual"):
        return self._submit("speech", text, speaker, scope, source)

    def result(self, request_id, *, consume=False):
        """Exact terminal result, independent of the bounded display event log."""
        with self._lock:
            result = self._results.pop(request_id, None) if consume else self._results.get(request_id)
            return deepcopy(result)

    def cancel_scope(self, scope):
        """Retire a stage: reject late submissions and cancel its queued/current output."""
        if scope is None:
            raise ValueError("阶段标识不能为空。")
        with self._condition:
            self._cancelled_scopes.add(scope)
            self._cancel_pending(scope)
            if self._current is not None and self._current.scope == scope:
                self._cancel_current("stage_finished")
            self._condition.notify_all()

    def _cancel_pending(self, scope=None):
        keep = deque()
        while self._pending:
            request = self._pending.popleft()
            if scope is None or request.scope == scope:
                self._finish_request(request, "cancelled", request.speaker)
            else:
                keep.append(request)
        self._pending = keep

    def _finish_request(self, request, outcome, speaker, error=None):
        result = {"request_id": request.request_id, "kind": request.kind,
                  "speaker": speaker, "outcome": outcome, "scope": request.scope,
                  "source": request.source, **({"error": error} if error else {})}
        self._results[request.request_id] = result
        self._trace.finish(request.request_id, outcome, speaker=speaker, error=error)
        event_type = "turn_completed" if outcome == "complete" else f"turn_{outcome}"
        self._publish(event_type, result)

    def snapshot(self):
        with self._lock:
            state = self.state_machine.snapshot()
            requests = self._trace.snapshot()
            current = next((r for r in requests if self._current is not None
                            and r["request_id"] == self._current.request_id), None)
            return {
                "run_id": self._run_id, "mode": self._config.get("mode", "live"),
                "roles": [{"name": name} for name in self._roles],
                "requests": requests, "current_request": current,
                "audio_level": self._audio_level, "event_seq": self._event_seq,
                "running": self._thread is not None and self._thread.is_alive(),
                "stopping": self._stopping,
                "phase": self._phase,
                "state": state["state"],
                "state_metadata": state["metadata"],
                "speaker": self._speaker,
                "next_speaker": self.scheduler.next_speaker,
                "queue_size": len(self._pending),
                "last_error": self._last_error,
                "last_outcome": self._last_outcome,
                "messages": deepcopy(list(self._messages)),
                "events": deepcopy(list(self._events)),
            }

    def _submit(self, kind, text, speaker, scope=None, source="manual"):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("请输入非空文字。")
        if len(text) > 12000:
            raise ProviderError("单次输入不能超过 12000 字符。")
        if speaker is not None and speaker not in self._roles:
            raise ValueError("指定的角色不存在。")
        if kind == "speech" and speaker is None:
            raise ValueError("直接朗读需要指定角色。")
        request = _Request(uuid.uuid4().hex, kind, text.strip(), speaker, scope, source)
        with self._condition:
            if self._thread is None or not self._thread.is_alive() or self._stopping:
                raise ProviderError("会话尚未运行或正在停止。")
            if scope is not None and scope in self._cancelled_scopes:
                raise Cancelled()
            if len(self._pending) >= 100:
                raise ProviderError("等待队列已达到 100 条，请等待当前任务完成。")
            self._pending.append(request)
            self._trace.queued(request)
            self._publish("turn_queued", {
                "request_id": request.request_id, "kind": kind, "speaker": speaker,
                "scope": scope, "source": source,
            })
            self._condition.notify()
        return request.request_id

    def _run(self, generation, stop_event):
        try:
            for name, role in self._roles.items():
                if stop_event.is_set():
                    raise Cancelled()
                if not role.get("avatar", {}).get("enabled", False):
                    continue
                if self._avatar_factory is None:
                    raise ProviderError("已启用 VTS，但没有可用的皮套适配器。")
                avatar = self._avatar_factory(deepcopy(role))
                with self._lock:
                    self._avatars[name] = avatar
                    if stop_event.is_set():
                        raise Cancelled()
                avatar.connect(allow_authorize=False)
            with self._condition:
                if stop_event.is_set():
                    raise Cancelled()
                if not self._managed:
                    self.state_machine.transition_to(State.CHATTING)
                self._phase = "ready"
                self._publish("session_started", {})
                self._condition.notify_all()
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: stop_event.is_set() or self._pending, timeout=0.25,
                    )
                    if stop_event.is_set():
                        break
                    request = self._pending.popleft() if self._pending else None
                    avatars = list(self._avatars.values())
                # Read adapter-reported faults even while no audio is playing.
                # check_error performs no network I/O; the avatar owns transport.
                for avatar in avatars:
                    check = getattr(avatar, "check_error", None)
                    if callable(check):
                        check()
                if request is not None:
                    self._run_turn(request, generation, stop_event)
        except Cancelled:
            pass
        except Exception as exc:
            with self._lock:
                if not stop_event.is_set():
                    self._last_error = self._safe_error(exc)
                    self._publish("session_failed", {"error": self._last_error})
        finally:
            with self._condition:
                self._stopping = True
                self._phase = "stopping"
                stop_event.set()
                self._cancel_pending()
                self._cancel_current("session_stopped")
                avatars = list(self._avatars.values())
                self._avatars.clear()
            for avatar in avatars:
                try:
                    avatar.set_mouth_level(0.0, generation=self._turn_counter)
                except Exception as exc:
                    self._record_cleanup_error(exc)
                try:
                    avatar.close()
                except Exception as exc:
                    self._record_cleanup_error(exc)
            with self._condition:
                if not self._managed:
                    current_state = self.state_machine.state
                    if current_state not in (State.CHATTING, State.TRANSITIONING, State.ENDING, State.IDLE):
                        self.state_machine.transition_to(State.TRANSITIONING)
                    if self.state_machine.state not in (State.ENDING, State.IDLE):
                        self.state_machine.transition_to(State.ENDING)
                    if self.state_machine.state == State.ENDING:
                        self.state_machine.transition_to(State.IDLE)
                self._current = self._cancel = self._output_id = self._speaker = None
                self._phase = "idle"
                self._stopping = False
                self._publish("session_stopped", {})
                self._condition.notify_all()
            # snapshot/start use thread.is_alive(), not a prematurely cleared flag.

    def _run_turn(self, request, generation, stop_event):
        with self._lock:
            if stop_event.is_set() or request.scope in self._cancelled_scopes:
                self._finish_request(request, "cancelled", request.speaker)
                return
            speaker = request.speaker or self.scheduler.pick_speaker(request.text)
            role = self._roles[speaker]
            cancel = threading.Event()
            self._current, self._cancel, self._speaker = request, cancel, speaker
            self._turn_counter += 1
            mouth_generation = self._turn_counter
            self._last_error = ""
            self._phase = "generating" if request.kind == "chat" else "synthesizing"
            self._trace.update(request.request_id, self._phase, speaker=speaker)
            output = self.arbiter.request_start(speaker=speaker, source=request.kind, policy=POLICY_DROP)
            self._output_id = output.output_id if output else None
            if output:
                self.arbiter.mark_task_id(output.output_id, request.request_id)
            messages = [{"role": "system", "content": role.get("system_prompt", "")}]
            for history_turn in self._histories[speaker]:
                messages.extend(deepcopy(history_turn))
            messages.append({"role": "user", "content": request.text})
            self._publish("turn_started", {
                "request_id": request.request_id, "kind": request.kind, "speaker": speaker,
            })

        avatar_error = []

        def on_level(level):
            # Avatar.set_mouth_level is a nonblocking handoff to its own transport.
            with self._lock:
                if not self._is_current(request, generation, cancel, stop_event):
                    return False
                value = float(level)
                if not math.isfinite(value):
                    value = 0.0
                self._audio_level = max(0.0, min(1.0, value))
                avatar = self._avatars.get(speaker)
                if avatar is None:
                    return True
                try:
                    avatar.set_mouth_level(self._audio_level, generation=mouth_generation)
                    return True
                except Exception as exc:
                    avatar_error.append(exc)
                    cancel.set()
                    return False

        text = request.text
        outcome = ""
        error = None
        try:
            if output is None:
                raise ProviderError("语音输出仍被占用，本轮未执行。")
            self._check_current(request, generation, cancel, stop_event)
            if request.kind == "chat":
                text = self._chat.complete(messages, cancel)
                self._check_current(request, generation, cancel, stop_event)
                if not isinstance(text, str) or not text.strip():
                    raise ProviderError("聊天服务没有返回可朗读的文字。")
            with self._lock:
                self._check_current(request, generation, cancel, stop_event)
                self._trace.update(request.request_id, "synthesizing", text=text)
            self._set_phase("synthesizing", request, generation, cancel, stop_event)
            clip = self._synth.synthesize(text, role.get("voice", ""), cancel)
            self._check_current(request, generation, cancel, stop_event)
            self._set_phase("playing", request, generation, cancel, stop_event)
            outcome = self._player.play(clip, cancel, on_level)
            self._check_current(request, generation, cancel, stop_event)
            if outcome not in ("complete", "simulated"):
                raise ProviderError("音频播放未完整结束。")
            if outcome == "simulated" and self._config.get("mode") != "demo":
                raise ProviderError("真实会话没有完成音频播放。")
        except Cancelled:
            outcome = "failed" if avatar_error else "cancelled"
            if avatar_error:
                error = self._safe_error(avatar_error[0])
        except Exception as exc:
            if (cancel.is_set() or stop_event.is_set()) and not avatar_error:
                outcome = "cancelled"
            else:
                outcome, error = "failed", self._safe_error(avatar_error[0] if avatar_error else exc)

        with self._condition:
            if outcome in ("complete", "simulated"):
                try:
                    # A VTS fault can arrive after the last audio callback. Read
                    # the final transport state before committing this turn.
                    avatar = self._avatars.get(speaker)
                    check = getattr(avatar, "check_error", None)
                    if callable(check):
                        check()
                    self._check_current(request, generation, cancel, stop_event)
                except Cancelled:
                    outcome = "cancelled"
                except Exception as exc:
                    outcome, error = "failed", self._safe_error(exc)
            # A callback may arrive after play() returned: invalidate first.
            if outcome in ("complete", "simulated"):
                if request.kind == "chat":
                    self._commit_history(request, speaker, text)
                    self.scheduler.advance_from(speaker)
                    self._append_message(request, "user", speaker, request.text, outcome)
                self._append_message(request, "assistant" if request.kind == "chat" else "narration",
                                     speaker, text, outcome)
                self.arbiter.mark_done(self._output_id)
            elif outcome == "cancelled":
                if self._output_id:
                    self.arbiter.cancel_if_current(self._output_id, reason="turn_cancelled")
                    # The old provider has now returned. Retire its tombstone;
                    # mark_done emits no completion for an ID no longer current.
                    self.arbiter.mark_done(self._output_id)
            else:
                if self._output_id:
                    self.arbiter.fail_current(error or "输出失败。", output_id=self._output_id)
                self._last_error = error or "处理请求失败，请检查配置或服务状态。"
            self._last_outcome = outcome
            self._audio_level = 0.0
            self._zero_mouth(speaker, mouth_generation)
            self._current = self._cancel = self._output_id = self._speaker = None
            self._phase = "stopping" if self._stopping else "ready"
            self._finish_request(request, outcome, speaker,
                                 self._last_error if outcome == "failed" else None)
            self._condition.notify_all()

    def _is_current(self, request, generation, cancel, stop_event):
        return (
            self._generation == generation and self._current is request
            and not cancel.is_set() and not stop_event.is_set()
            and self.arbiter.is_current(self._output_id)
        )

    def _check_current(self, request, generation, cancel, stop_event):
        with self._lock:
            if not self._is_current(request, generation, cancel, stop_event):
                raise Cancelled()

    def _set_phase(self, phase, request, generation, cancel, stop_event):
        with self._lock:
            self._check_current(request, generation, cancel, stop_event)
            self._phase = phase
            self._trace.update(request.request_id, phase)
            self._publish("turn_phase", {"request_id": request.request_id,
                                        "speaker": self._speaker, "phase": phase})

    def _cancel_current(self, reason):
        if self._cancel is not None:
            self._cancel.set()
        if self._current is not None:
            self._trace.update(self._current.request_id, "cancelling", text="")
        self._audio_level = 0.0
        if self._output_id:
            self.arbiter.cancel_if_current(self._output_id, reason=reason)
        if self._speaker is not None:
            self._zero_mouth(self._speaker, self._turn_counter)

    def _zero_mouth(self, speaker, generation):
        avatar = self._avatars.get(speaker)
        if avatar is not None:
            try:
                avatar.set_mouth_level(0.0, generation=generation)
            except Exception as exc:
                self._record_cleanup_error(exc)

    def _commit_history(self, request, speaker, text):
        for name, history in self._histories.items():
            if name == speaker:
                history.append([
                    {"role": "user", "content": request.text},
                    {"role": "assistant", "content": text},
                ])
            else:
                history.append([{"role": "user", "content": f"[{speaker} said] {text}"}])

    def _append_message(self, request, role, speaker, text, outcome):
        self._messages.append({
            "request_id": request.request_id, "kind": request.kind,
            "role": role, "speaker": speaker, "text": text,
            "outcome": outcome, "timestamp": time.time(),
        })

    def _record_event(self, event):
        with self._lock:
            self._event_seq += 1
            self._events.append({
                "seq": self._event_seq, "run_id": self._run_id,
                "event_type": event.event_type, "data": deepcopy(event.data),
                "source": event.source, "timestamp": event.timestamp,
            })

    def _publish(self, event_type, data):
        self.bus.publish(event_type, data, source="public_host")

    def _record_cleanup_error(self, exc):
        with self._lock:
            self._last_error = self._safe_error(exc)
            self._publish("session_failed", {"error": self._last_error})

    @staticmethod
    def _safe_error(exc):
        if isinstance(exc, ProviderError):
            return str(exc)
        return "处理请求失败，请检查配置或服务状态。"
