"""
main.py — a runnable demo of the dual-AI co-hosting orchestration core.

This wires the **real** coordination modules of the system — the event bus, the
global state machine, the speaker scheduler and the speech-output arbiter — and
drives two example AI characters through a short co-hosted chat segment.

To keep the demo runnable with no API keys, audio hardware or external game, the
two pieces that need them in production are replaced by small, clearly marked
stand-ins:

  * ``DemoBrain`` stands in for the real per-character LLM brains — see
    ``fast_brain.py`` (text pipeline) and the realtime speech-to-speech engine in
    ``realtime_chat.py``. It returns canned lines instead of calling a model.
  * ``demo_speak()`` stands in for real voice output — see ``lumi_tts.py`` /
    ``cosyvoice_tts.py``. It prints the line instead of speaking it aloud.

Everything else — who-speaks-next, @-mention routing, the viewer-chat queue, and
one-voice-at-a-time arbitration — is the real production code from this repo.

Run it:

    python main.py
"""
import time

from event_bus import EventBus
from state_machine import StateMachine, State
from speaker_scheduler import SpeakerScheduler
from speech_output_arbiter import SpeechOutputArbiter, POLICY_QUEUE
from voice_config import get_speaker_config


# --- demo stand-ins for the key/hardware-gated production modules ------------

class DemoBrain:
    """Stand-in for a real per-character LLM brain.

    In production each character is driven by ``fast_brain.py`` (text pipeline) or
    the realtime speech-to-speech engine in ``realtime_chat.py``: it builds a
    prompt from the character's persona, the running history, the viewer messages
    and the partner's last line, then calls an LLM. Here we just rotate canned
    lines so the demo runs with no API key. These are placeholder personalities,
    not the real characters.
    """

    def __init__(self, name: str, lines: dict):
        self.name = name
        self._lines = lines
        self._i = 0

    def respond(self, viewer_msgs: list, partner_last: str) -> str:
        # A real brain would build a prompt from persona + history + viewer_msgs
        # + the partner's last line, then call the LLM. We just rotate canned lines.
        if viewer_msgs:
            who = viewer_msgs[-1].get("label") or "someone"
            return self._lines["to_viewer"].format(who=who)
        line = self._lines["banter"][self._i % len(self._lines["banter"])]
        self._i += 1
        return line


def demo_speak(arbiter: SpeechOutputArbiter, speaker: str, text: str,
               *, source: str = "chat", policy: str = POLICY_QUEUE) -> bool:
    """Stand-in for real TTS voice output, routed through the REAL arbiter.

    Production speech goes through ``lumi_tts.speak()``. The part that matters —
    that only one character holds the floor at a time — is the real arbiter.
    """
    output = arbiter.request_start(speaker=speaker, source=source, policy=policy)
    if output is None:
        # The arbiter queued or dropped us because someone else holds the floor.
        return False
    print(f"  {speaker}: {text}")
    time.sleep(0.4)                      # simulate the time it takes to say it out loud
    arbiter.mark_done(output.output_id)  # release the floor only when audio is done
    return True


# --- placeholder characters (NOT the real personas) -------------------------

DEMO_LINES = {
    "Lumi": {
        "banter": [
            "Welcome in, everyone! It's so cozy with all of you here.",
            "Nox, smile a little - the chat can hear you sulking~",
            "Ooh, someone sent a gift! You're all far too kind to us.",
            "Okay okay, what should we play after this?",
        ],
        "to_viewer": "Ooh, {who} asks a good one - let me think!",
    },
    "Nox": {
        "banter": [
            "Another stream. Joy. Hello, I suppose.",
            "Lumi, your enthusiasm is a workplace hazard.",
            "Chat, your taste is questionable. But you showed up — respect.",
            "If we must do this, let's at least do it well.",
        ],
        "to_viewer": "{who}, bold of you to assume I'd make this easy.",
    },
}


def log_bus_event(event):
    """Print the coordination events flowing across the real event bus."""
    if event.event_type == "state_changed":
        print(f"     [state] {event.data['old']} -> {event.data['new']}")
    elif event.event_type == "speech_output_queued":
        print(f"     [arbiter] {event.data['speaker']} queued - floor is busy")


def main():
    active = ["Lumi", "Nox"]

    # Everything below is the real production coordination layer.
    bus = EventBus()
    state = StateMachine(bus)
    scheduler = SpeakerScheduler(active_speakers=active)
    arbiter = SpeechOutputArbiter(event_bus=bus)

    for et in ("state_changed", "speech_output_queued"):
        bus.subscribe(et, log_bus_event)

    brains = {name: DemoBrain(name, DEMO_LINES[name]) for name in active}
    for name in active:
        cfg = get_speaker_config(name)  # real per-character config (placeholder values)
        print(f"loaded character: {cfg.name}  (subtitle color {cfg.subtitle_color})")

    # Boot the stream: IDLE -> OPENING -> CHATTING.
    state.transition_to(State.OPENING)
    state.transition_to(State.CHATTING)
    scheduler.reset_rotation("Lumi")  # Lumi opens

    # A viewer message arrives. It does not interrupt; it goes into the queue and
    # is picked up on the next turn. This one @-mentions Nox.
    scheduler.enqueue_input("弹幕：mona：Nox tell us a cold joke",
                            source="danmaku", label="mona")

    print("\n=== co-hosted chat segment ===")
    print("(characters alternate by default; an @-mention overrides who answers)\n")
    partner_last = {name: "" for name in active}

    for turn in range(6):
        # Midway, another viewer chimes in — this one @-mentions Lumi.
        if turn == 3:
            scheduler.enqueue_input("弹幕：rin：Lumi what are we playing next?",
                                    source="danmaku", label="rin")

        viewer_msgs = scheduler.pop_all_inputs(max_items=8)
        last_text = viewer_msgs[-1]["text"] if viewer_msgs else None

        # Real routing: an @-mention picks that character, otherwise next_speaker.
        speaker = scheduler.pick_speaker(last_text)
        if viewer_msgs:
            print(f"  >> viewer @-mentioned {speaker} -> routed to {speaker}")
        else:
            print(f"  >> next turn -> {speaker}")

        line = brains[speaker].respond(viewer_msgs, partner_last[speaker])
        demo_speak(arbiter, speaker, line)

        # Cross-character mirroring: the partner sees this as a stage note,
        # never as its own words.
        for other in active:
            if other != speaker:
                partner_last[other] = f"[{speaker} said] {line}"

        scheduler.advance_from(speaker)
        time.sleep(0.15)

    # Show the arbiter keeping the floor to one voice at a time.
    print("\n=== one voice at a time (arbitration) ===")
    held = arbiter.request_start(speaker="Lumi", source="chat")
    print(f"  Lumi takes the floor: {held.output_id}")
    blocked = arbiter.request_start(speaker="Nox", source="chat", policy=POLICY_QUEUE)
    print(f"  Nox requests the floor while Lumi holds it -> "
          f"{'blocked (queued)' if blocked is None else 'got floor (unexpected!)'}")
    arbiter.mark_done(held.output_id)
    print("  Lumi finishes; the floor is free again.")

    # Close the stream.
    state.transition_to(State.ENDING)
    state.transition_to(State.IDLE)
    print("\n=== demo complete ===")


if __name__ == "__main__":
    main()
