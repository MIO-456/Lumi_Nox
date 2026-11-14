"""Offline host lifecycle contracts, using controlled provider completion gates."""
from copy import deepcopy
import threading
import time
import unittest

from public_host.session import Session
from public_host.providers import ProviderError


WAIT = 3.0


def config(mode="live"):
    return {
        "mode": mode,
        "roles": [
            {"name": "Lumi", "system_prompt": "You are the example Lumi.",
             "voice": "example-lumi", "avatar": {"enabled": False}},
            {"name": "Nox", "system_prompt": "You are the example Nox.",
             "voice": "example-nox", "avatar": {"enabled": False}},
        ],
    }


class Step:
    """A provider may return after cancellation, as a real blocking SDK can."""
    def __init__(self, result, *, held=False, error=None):
        self.result = result
        self.error = error
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancel = None
        if not held:
            self.release.set()

    def run(self, cancel):
        self.cancel = cancel
        self.entered.set()
        if not self.release.wait(WAIT):
            raise AssertionError("Test provider completion gate was not released")
        if self.error is not None:
            raise self.error
        return self.result


class Providers:
    def __init__(self, *, chat=None, synth=None, player=None):
        self.steps = {
            "chat": list(chat or []), "synth": list(synth or []),
            "player": list(player or []),
        }
        self.calls = {name: [] for name in self.steps}
        self._lock = threading.Lock()

    def _call(self, stage, args, cancel):
        with self._lock:
            index = len(self.calls[stage])
            self.calls[stage].append(args)
            if index < len(self.steps[stage]):
                step = self.steps[stage][index]
            else:
                result = {"chat": "Example reply", "synth": object(),
                          "player": "complete"}[stage]
                step = Step(result)
                self.steps[stage].append(step)
        return step.run(cancel)

    def complete(self, messages, cancel):
        return self._call("chat", deepcopy(messages), cancel)

    def synthesize(self, text, voice, cancel):
        return self._call("synth", {"text": text, "voice": voice}, cancel)

    def play(self, clip, cancel, on_level):
        return self._call("player", {"clip": clip, "on_level": on_level}, cancel)

    def release_all(self):
        with self._lock:
            for steps in self.steps.values():
                for step in steps:
                    step.release.set()


class FakeAvatar:
    def __init__(self):
        self.operations = []
        self.closed = threading.Event()
        self._error = None
        self._lock = threading.Lock()

    def connect(self, *, allow_authorize):
        with self._lock:
            self.operations.append(("connect", allow_authorize))

    def set_mouth_level(self, level, *, generation):
        with self._lock:
            self.operations.append(("level", level, generation))

    def fail(self, error):
        with self._lock:
            self._error = error

    def check_error(self):
        with self._lock:
            if self._error is not None:
                raise self._error

    def close(self):
        with self._lock:
            self.operations.append(("close",))
        self.closed.set()

    def snapshot(self):
        with self._lock:
            return list(self.operations)


class PublicHostSessionTests(unittest.TestCase):
    def wait_for(self, predicate, message="host did not reach the expected state"):
        deadline = time.monotonic() + WAIT
        wake = threading.Event()
        while True:
            value = predicate()
            if value:
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(message)
            # Public snapshots have no condition variable. Provider ordering is
            # controlled by Events; this bounded wait only observes final state.
            wake.wait(min(0.01, remaining))

    def make_session(self, providers=None, *, mode="live", session_config=None,
                     avatar_factory=None):
        providers = providers or Providers()
        session = Session(session_config or config(mode), chat=providers, synth=providers,
                          player=providers, avatar_factory=avatar_factory)

        def cleanup():
            providers.release_all()
            session.stop()
            self.wait_for(lambda: not session.snapshot()["running"],
                          "test worker did not stop during cleanup")

        self.addCleanup(cleanup)
        self.assertTrue(session.start()["running"])
        return session, providers

    def output_for(self, session, request_id):
        return next((message for message in session.snapshot()["messages"]
                     if message.get("request_id") == request_id
                     and message.get("role") == "assistant"), None)

    def test_playback_completion_controls_rotation_and_queued_input(self):
        first, second = Step("complete", held=True), Step("complete", held=True)
        session, providers = self.make_session(Providers(player=[first, second]))
        one = session.submit_text("First viewer message")
        self.assertIsInstance(one, str)
        self.assertTrue(first.entered.wait(WAIT))
        self.assertEqual(session.snapshot()["next_speaker"], "Lumi")
        self.assertIsNone(self.output_for(session, one))

        two = session.submit_text("Second viewer message")
        self.assertNotEqual(one, two)
        self.assertEqual(session.snapshot()["queue_size"], 1)
        self.assertEqual(len(providers.calls["chat"]), 1)
        first.release.set()
        self.assertTrue(second.entered.wait(WAIT))
        self.assertEqual(self.output_for(session, one)["speaker"], "Lumi")
        self.assertEqual(session.snapshot()["next_speaker"], "Nox")
        self.assertIsNone(self.output_for(session, two))
        second.release.set()
        self.wait_for(lambda: self.output_for(session, two))
        self.assertEqual(self.output_for(session, two)["speaker"], "Nox")
        self.assertEqual(self.output_for(session, two)["outcome"], "complete")
        self.assertEqual(session.snapshot()["next_speaker"], "Lumi")

    def test_explicit_role_routes_voice_and_partner_history_is_user_context(self):
        partner = "Nox finished this exact thought."
        next_chat = Step("Lumi reply", held=True)
        session, providers = self.make_session(Providers(chat=[Step(partner), next_chat]))
        first = session.submit_text("Please answer", speaker="Nox")
        self.wait_for(lambda: self.output_for(session, first))
        self.assertEqual(self.output_for(session, first)["speaker"], "Nox")
        self.assertEqual(providers.calls["synth"][0]["voice"], "example-nox")
        self.assertEqual(session.snapshot()["next_speaker"], "Lumi")

        second = session.submit_text("What do you think?", speaker="Lumi")
        self.assertTrue(next_chat.entered.wait(WAIT))
        messages = providers.calls["chat"][1]
        mirrored = [m for m in messages if partner in str(m.get("content", ""))]
        self.assertTrue(mirrored)
        self.assertTrue(all(m["role"] == "user" for m in mirrored))
        self.assertTrue(any("Nox" in m["content"] for m in mirrored))
        self.assertIn("example Lumi", messages[0]["content"])
        next_chat.release.set()
        self.wait_for(lambda: self.output_for(session, second))

    def test_late_results_after_interrupt_are_rejected_at_every_provider_stage(self):
        for stage in ("chat", "synth", "player"):
            with self.subTest(stage=stage):
                old = Step({"chat": "OLD_REPLY_MUST_NOT_COMMIT",
                            "synth": object(), "player": "complete"}[stage], held=True)
                session, providers = self.make_session(Providers(**{stage: [old]}))
                previous = session.submit_text("OLD_VIEWER_INPUT")
                self.assertTrue(old.entered.wait(WAIT))
                session.interrupt()
                self.assertTrue(old.cancel.wait(WAIT))
                current = session.submit_text("CURRENT_VIEWER_INPUT")
                old.release.set()
                self.wait_for(lambda: self.output_for(session, current))
                messages = session.snapshot()["messages"]
                self.assertFalse(any(m.get("request_id") == previous for m in messages))
                self.assertEqual(self.output_for(session, current)["speaker"], "Lumi")
                self.assertEqual(self.output_for(session, current)["outcome"], "complete")
                events = session.snapshot()["events"]
                cancelled_ids = {event["data"]["output_id"] for event in events
                                 if event["event_type"] == "speech_output_cancelled"}
                completed_ids = {event["data"]["output_id"] for event in events
                                 if event["event_type"] == "speech_output_done"}
                self.assertTrue(cancelled_ids)
                self.assertTrue(cancelled_ids.isdisjoint(completed_ids))
                self.assertTrue(all(not session.arbiter.is_cancelled(output_id)
                                    for output_id in cancelled_ids))
                if stage == "chat":
                    self.assertEqual(len(providers.calls["synth"]), 1)
                if stage in ("chat", "synth"):
                    self.assertEqual(len(providers.calls["player"]), 1)
                self.assertNotIn("OLD_VIEWER_INPUT", str(providers.calls["chat"][-1]))
                session.stop()
                self.wait_for(lambda: not session.snapshot()["running"])

    def test_stop_returns_before_blocking_provider_exits_and_blocks_restart(self):
        held = Step("LATE_REPLY", held=True)
        session, providers = self.make_session(Providers(chat=[held]))
        old_id = session.submit_text("Stop this request")
        self.assertTrue(held.entered.wait(WAIT))
        returned = threading.Event()
        result = []

        def stop():
            try:
                result.append(session.stop())
            finally:
                returned.set()

        stopper = threading.Thread(target=stop, daemon=True)
        stopper.start()
        self.assertTrue(returned.wait(0.5), "stop must not wait for a blocked provider")
        stopper.join(WAIT)
        self.assertTrue(held.cancel.is_set())
        self.assertTrue(result[0]["stopping"])
        try:
            session.start()
        except (RuntimeError, ValueError):
            pass
        self.assertTrue(session.snapshot()["stopping"])
        self.assertEqual(len(providers.calls["chat"]), 1)
        held.release.set()
        self.wait_for(lambda: not session.snapshot()["running"])
        self.assertFalse(session.snapshot()["running"])
        self.assertIsNone(self.output_for(session, old_id))
        self.assertEqual(providers.calls["synth"], [])
        self.assertTrue(session.start()["running"])
        new_id = session.submit_text("New session")
        self.wait_for(lambda: self.output_for(session, new_id))

    def test_provider_failures_release_the_turn_and_do_not_expose_unknown_errors(self):
        secret = "synthetic_private_token_DO_NOT_DISPLAY"
        for stage in ("chat", "synth", "player"):
            with self.subTest(stage=stage):
                failed = Step(None, error=RuntimeError(secret))
                session, _ = self.make_session(Providers(**{stage: [failed]}))
                bad = session.submit_text("Failed viewer message")
                self.wait_for(lambda: session.snapshot()["last_error"])
                state = session.snapshot()
                self.assertNotIn(secret, str(state))
                self.assertFalse(any(m.get("request_id") == bad for m in state["messages"]))
                self.assertEqual(state["next_speaker"], "Lumi")
                good = session.submit_text("Recovery viewer message")
                self.wait_for(lambda: self.output_for(session, good))
                self.assertEqual(self.output_for(session, good)["outcome"], "complete")
                self.assertEqual(self.output_for(session, good)["speaker"], "Lumi")
                session.stop()
                self.wait_for(lambda: not session.snapshot()["running"])

    def test_direct_speech_bypasses_chat_history_and_rotation(self):
        spoken = "DIRECT_SPEECH_MUST_NOT_ENTER_CHAT_HISTORY"
        next_chat = Step("Normal chat reply", held=True)
        session, providers = self.make_session(Providers(chat=[next_chat]))
        direct = session.submit_speech(spoken, speaker="Nox")
        self.assertIsInstance(direct, str)
        self.wait_for(lambda: session.snapshot()["last_outcome"] == "complete")
        self.assertEqual(providers.calls["chat"], [])
        self.assertEqual(providers.calls["synth"][0], {"text": spoken, "voice": "example-nox"})
        self.assertEqual(session.snapshot()["next_speaker"], "Lumi")
        followup = session.submit_text("A normal viewer message")
        self.assertTrue(next_chat.entered.wait(WAIT))
        self.assertNotIn(spoken, str(providers.calls["chat"][0]))
        next_chat.release.set()
        self.wait_for(lambda: self.output_for(session, followup))
        self.assertEqual(self.output_for(session, followup)["speaker"], "Lumi")

    def test_simulated_output_advances_only_in_demo_mode(self):
        session, _ = self.make_session(Providers(player=[Step("simulated")]), mode="demo")
        request_id = session.submit_text("Demo input")
        message = self.wait_for(lambda: self.output_for(session, request_id))
        self.assertEqual(message["outcome"], "simulated")
        self.assertEqual(session.snapshot()["next_speaker"], "Nox")

    def test_simulated_output_cannot_be_reported_as_live_success(self):
        session, _ = self.make_session(Providers(player=[Step("simulated")]))
        request_id = session.submit_text("Live input")
        self.wait_for(lambda: session.snapshot()["last_error"])
        self.assertIsNone(self.output_for(session, request_id))
        self.assertEqual(session.snapshot()["next_speaker"], "Lumi")
        self.assertNotEqual(session.snapshot()["last_outcome"], "complete")

    def test_empty_roles_are_rejected_before_starting_providers(self):
        empty = config()
        empty["roles"] = []
        providers = Providers()
        with self.assertRaises(ValueError):
            session = Session(empty, chat=providers, synth=providers, player=providers)
            session.start()
        self.assertTrue(all(not calls for calls in providers.calls.values()))

    def test_avatar_routes_levels_by_role_and_rejects_an_old_playback_callback(self):
        avatars = {name: FakeAvatar() for name in ("Lumi", "Nox")}
        configured = config()
        for role in configured["roles"]:
            role["avatar"]["enabled"] = True
        first, second = Step("complete", held=True), Step("complete", held=True)
        session, providers = self.make_session(
            Providers(player=[first, second]), session_config=configured,
            avatar_factory=lambda role: avatars[role["name"]],
        )
        one = session.submit_text("First turn", speaker="Lumi")
        self.assertTrue(first.entered.wait(WAIT))
        old_level = providers.calls["player"][0]["on_level"]
        self.assertTrue(old_level(0.4))
        self.assertFalse(any(op[0] == "level" for op in avatars["Nox"].snapshot()))
        first.release.set()
        self.wait_for(lambda: self.output_for(session, one))

        two = session.submit_text("Second turn", speaker="Nox")
        self.assertTrue(second.entered.wait(WAIT))
        self.assertEqual(session.snapshot()["phase"], "playing")
        self.assertTrue(providers.calls["player"][1]["on_level"](0.7))
        before = {name: avatar.snapshot() for name, avatar in avatars.items()}
        self.assertFalse(old_level(0.9))
        self.assertEqual({name: avatar.snapshot() for name, avatar in avatars.items()}, before)
        lumi_frames = [op for op in before["Lumi"] if op[0] == "level"]
        nox_frames = [op for op in before["Nox"] if op[0] == "level"]
        self.assertEqual(lumi_frames[0][1], 0.4)
        self.assertEqual(lumi_frames[-1][1], 0.0)
        self.assertEqual(nox_frames[0][1], 0.7)
        self.assertTrue(all(type(op[2]) is int for op in lumi_frames + nox_frames))
        self.assertGreater(nox_frames[0][2], lumi_frames[0][2])

        second.release.set()
        self.wait_for(lambda: self.output_for(session, two))
        session.stop()
        self.wait_for(lambda: not session.snapshot()["running"])
        for avatar in avatars.values():
            operations = avatar.snapshot()
            self.assertEqual(operations[0], ("connect", False))
            self.assertEqual(operations[-1], ("close",))
            self.assertEqual(operations[-2][0:2], ("level", 0.0))
            generations = [op[2] for op in operations if op[0] == "level"]
            self.assertTrue(all(type(value) is int for value in generations))
            self.assertEqual(generations, sorted(generations))

    def test_history_and_snapshots_stay_bounded_and_snapshot_edits_are_isolated(self):
        configured = config()
        configured["roles"] = configured["roles"][:1]
        inspect_next = Step("Final answer", held=True)
        replies = [Step(f"Completed answer {i}") for i in range(55)]
        session, providers = self.make_session(
            Providers(chat=[*replies, inspect_next]), session_config=configured,
        )
        for i in range(55):
            request_id = session.submit_text(f"Viewer message {i}")
            self.wait_for(lambda: self.output_for(session, request_id))
        state = session.snapshot()
        self.assertLessEqual(len(state["messages"]), 100)
        self.assertLessEqual(len(state["events"]), 200)
        self.assertTrue(state["messages"] and state["events"])
        state["messages"][0]["text"] = "MUTATED_SNAPSHOT_ONLY"
        state["events"][-1]["data"]["mutation"] = "MUTATED_SNAPSHOT_ONLY"
        self.assertNotIn("MUTATED_SNAPSHOT_ONLY", str(session.snapshot()))
        state["messages"].clear()
        state["events"].clear()
        self.assertTrue(session.snapshot()["messages"] and session.snapshot()["events"])

        request_id = session.submit_text("Final viewer input")
        self.assertTrue(inspect_next.entered.wait(WAIT))
        outgoing = providers.calls["chat"][-1]
        self.assertLessEqual(len(outgoing), 1 + 20 * 2 + 1)
        self.assertEqual(outgoing[0]["role"], "system")
        self.assertEqual([m["content"] for m in outgoing if m["role"] == "assistant"],
                         [f"Completed answer {i}" for i in range(35, 55)])
        self.assertEqual([m["content"] for m in outgoing if m["role"] == "user"],
                         [f"Viewer message {i}" for i in range(35, 55)] + ["Final viewer input"])
        inspect_next.release.set()
        self.wait_for(lambda: self.output_for(session, request_id))

    def test_avatar_background_error_stops_an_idle_session_and_closes_avatar(self):
        avatar = FakeAvatar()
        configured = config()
        configured["roles"] = configured["roles"][:1]
        configured["roles"][0]["avatar"]["enabled"] = True
        session, providers = self.make_session(
            session_config=configured, avatar_factory=lambda role: avatar,
        )
        self.wait_for(lambda: session.snapshot()["phase"] == "ready")
        safe_error = "Example avatar connection was lost; reconnect it."
        avatar.fail(ProviderError(safe_error))
        self.assertTrue(avatar.closed.wait(WAIT), "idle background error must close the avatar")
        self.wait_for(lambda: not session.snapshot()["running"])
        state = session.snapshot()
        self.assertEqual(state["last_error"], safe_error)
        self.assertFalse(state["stopping"])
        self.assertEqual(state["state"], "IDLE")
        self.assertEqual(avatar.snapshot()[-1], ("close",))
        self.assertTrue(all(not calls for calls in providers.calls.values()))

    def test_avatar_fault_at_playback_end_is_not_committed_as_a_completed_turn(self):
        avatar = FakeAvatar()
        configured = config()
        configured["roles"][0]["avatar"]["enabled"] = True
        safe_error = "Example avatar disconnected at the end of playback."

        class EndFaultProviders(Providers):
            def play(self, clip, cancel, on_level):
                on_level(0.4)
                # The transport fails after the final audio-level notification,
                # before the player reports that the audio device has drained.
                avatar.fail(ProviderError(safe_error))
                return "complete"

        session, _ = self.make_session(
            EndFaultProviders(), session_config=configured,
            avatar_factory=lambda role: avatar,
        )
        request_id = session.submit_text("Do not commit this failed avatar turn")
        self.wait_for(lambda: not session.snapshot()["running"])
        state = session.snapshot()
        self.assertEqual(state["last_outcome"], "failed")
        self.assertEqual(state["last_error"], safe_error)
        self.assertEqual(state["next_speaker"], "Lumi")
        self.assertEqual(state["messages"], [])
        events = [event for event in state["events"]
                  if event["data"].get("request_id") == request_id]
        self.assertTrue(any(event["event_type"] == "turn_failed" for event in events))
        self.assertFalse(any(event["event_type"] == "turn_completed" for event in events))
        self.assertFalse(any(event["event_type"] == "speech_output_done"
                             for event in state["events"]))

    def test_input_and_pending_queue_limits_reject_work_without_running_it(self):
        held = Step("Cancelled reply", held=True)
        session, providers = self.make_session(Providers(chat=[held]))
        with self.assertRaises(ProviderError):
            session.submit_text(" " * 12000 + "x")
        self.assertEqual(session.snapshot()["queue_size"], 0)
        self.assertTrue(all(not calls for calls in providers.calls.values()))

        session.submit_text("Hold the active turn")
        self.assertTrue(held.entered.wait(WAIT))
        for index in range(100):
            session.submit_text(f"Pending viewer message {index}")
        self.assertEqual(session.snapshot()["queue_size"], 100)
        with self.assertRaises(ProviderError):
            session.submit_text("One beyond the pending limit")
        self.assertEqual(session.snapshot()["queue_size"], 100)
        self.assertEqual(len(providers.calls["chat"]), 1)

        session.stop()
        self.assertTrue(held.cancel.is_set())
        self.assertEqual(session.snapshot()["queue_size"], 0)
        held.release.set()
        self.wait_for(lambda: not session.snapshot()["running"])
        self.assertEqual(len(providers.calls["chat"]), 1)
        self.assertEqual(providers.calls["synth"], [])
        self.assertEqual(providers.calls["player"], [])
        self.assertEqual(session.snapshot()["messages"], [])


if __name__ == "__main__":
    unittest.main()
