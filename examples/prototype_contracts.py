"""Inspect sample contracts and create disposable synthetic audio, with no I/O services.

Run from the repository root: python -m examples.prototype_contracts
No game is implemented, no audio is played, and no model/service is contacted.
"""
from dataclasses import asdict
import json
import math
from pathlib import Path
import struct
import tempfile
import wave

from interfaces.minigame import DecisionRequest
from interfaces.singing import SongAssets, SongRecord


FIXTURES = Path(__file__).with_name("fixtures")


def sample_decision() -> DecisionRequest:
    """A tiny empty-board example, not a universal state schema or game choice."""
    return DecisionRequest(
        decision_id="sample-decision-1", session_id="sample-session", revision=1,
        actor="speaker_a", spectator="speaker_b",
        state={"size": 5, "board": [[0] * 5 for _ in range(5)]},
        legal_actions=tuple(
            {"action": "place_stone", "row": row, "col": col}
            for row in range(5) for col in range(5)
        ),
    )


def write_synthetic_song(root: Path) -> SongRecord:
    """Write a 1 s test tone and stems to a caller-owned empty directory.

    This demonstrates the existing filename convention only. It does not
    synthesize a voice, validate music quality, or enable a production song.
    """
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Synthetic song fixtures require an empty output directory")
    payload = json.loads((FIXTURES / "singing_catalog.json").read_text(encoding="utf-8"))
    record = SongRecord(**payload["songs"][0])
    record.assets = SongAssets(
        master=root / f"{record.basename}.wav",
        vocal=root / f"{record.basename}人声.wav",
        instrumental=root / f"{record.basename}伴奏.wav",
        lyrics=root / f"{record.basename}.lrc",
        translation=root / f"{record.basename}.zh.txt",
    )
    sample_rate = 24000
    tone = b"".join(struct.pack("<h", int(2000 * math.sin(2 * math.pi * 440 * i / sample_rate)))
                    for i in range(sample_rate))
    silence = bytes(len(tone))
    for path, pcm in ((record.assets.master, tone), (record.assets.vocal, tone),
                      (record.assets.instrumental, silence)):
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(pcm)
    record.assets.lyrics.write_text("[00:00.00]测试音轨（没有演唱）\n", encoding="utf-8")
    (root / "singing_catalog.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def main():
    decision = sample_decision()
    # A host must validate pending id / actor / revision and action legality
    # before applying this response. Constructing a dataclass does none of that.
    response = {"decision_id": decision.decision_id, "actor": decision.actor,
                "action": dict(decision.legal_actions[0])}
    assert response["action"] in decision.legal_actions
    print("Game decision example:", json.dumps(asdict(decision), ensure_ascii=False))
    print("Game response example:", json.dumps(response, ensure_ascii=False))

    drawing = json.loads((FIXTURES / "drawing.json").read_text(encoding="utf-8"))
    # This is an example reply, not evidence that any renderer has drawn a frame.
    example_ack = {"type": "draw_complete", "draw_id": drawing["draw_id"],
                   "consumer": "stage", "visible": True}
    print("Drawing sample:", drawing["draw_id"], "strokes:", len(drawing["strokes"]))
    print("Example completion shape (no renderer):", json.dumps(example_ack))

    with tempfile.TemporaryDirectory(prefix="public-song-fixture-") as folder:
        song = write_synthetic_song(Path(folder))
        with wave.open(str(song.assets.master), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        print("Synthetic song asset example:", song.id, "seconds:", duration,
              "enabled:", song.enabled)
    print("Contract examples complete; temporary audio removed; no playback or live host.")


if __name__ == "__main__":
    main()
