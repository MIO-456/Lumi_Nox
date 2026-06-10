"""
Per-character runtime configuration.

Each AI character maps to one set of runtime parameters: voice, Live2D model,
subtitle styling, persona prompt file, and audio routing. This is the single
source of truth that the speaker scheduler and routing layers read by the
current speaker.

When streaming with a single character, ``active_speakers`` holds one entry;
in dual-character mode it holds two, e.g. ["Lumi", "Nox"].

NOTE: The example characters below ("Lumi" / "Nox") ship with PLACEHOLDER
values only. The real voice ids, Live2D models, persona prompt files and
behavioral wording are part of the closed "soul" of the project and are NOT
included in this open-source repository. Replace the placeholders with your
own characters' values.
"""
from dataclasses import dataclass, field


# VTS port allocation (computed at runtime from active_speakers ordering,
# not stored on SpeakerConfig):
#   active_speakers[i] uses port VTS_BASE_PORT + i
# Each VTube Studio instance must have the matching model loaded manually.
VTS_BASE_PORT = 8001


@dataclass
class SpeakerConfig:
    name: str                                # e.g. "Lumi" / "Nox"
    voice_name: str                          # voice-library key, resolved to a real voice id by the TTS layer
    realtime_voice_id: str                   # speaker_id for the end-to-end realtime voice model
    vts_model_name: str                      # Live2D model name registered in VTube Studio
    subtitle_color: str                      # subtitle prefix color (hex)
    subtitle_label: str                      # display name in subtitles
    prompt_file: str                         # persona prompt filename (text-pipeline)
    audio_cable_keyword: str                 # virtual audio cable name keyword for fuzzy device matching
    realtime_character_manifest_file: str = ""               # cleaned persona for the realtime pipeline; empty = char skips realtime
    logit_bias: dict = field(default_factory=dict)           # per-character logit_bias (overrides brand default)
    game_role_addon_controller: str = (
        "## Game role: CONTROLLER\n"
        "You are the one playing this round; speak in first person about your own actions.\n"
        "You are a streamer first: still say only one short line per turn, but the topic is\n"
        "free - banter, react to chat, riff with your partner. You do not have to narrate the move.\n"
        "Hard requirement: still call the tool to actually make each move.\n"
    )
    game_role_addon_spectator: str = (
        "## Game role: SPECTATOR\n"
        "You are not playing this round; you hold no controls and call no action tools.\n"
        "You are a streamer first: still say only one short line per turn, topic free -\n"
        "banter, react to chat, riff with the controller. You just don't make decisions for them.\n"
    )


SPEAKER_CONFIGS: dict[str, SpeakerConfig] = {
    "Lumi": SpeakerConfig(
        name="Lumi",
        voice_name="character_a_voice",
        realtime_voice_id="REPLACE_WITH_YOUR_VOICE_ID",
        vts_model_name="Character A Model",
        subtitle_color="#FFC0CB",
        subtitle_label="Lumi",
        prompt_file="persona/character_a.md",
        realtime_character_manifest_file="persona/character_a_realtime.md",
        audio_cable_keyword="CABLE Input",
    ),
    "Nox": SpeakerConfig(
        name="Nox",
        voice_name="character_b_voice",
        realtime_voice_id="REPLACE_WITH_YOUR_VOICE_ID",
        vts_model_name="Character B Model",
        subtitle_color="#FFA500",
        subtitle_label="Nox",
        prompt_file="persona/character_b.md",
        realtime_character_manifest_file="persona/character_b_realtime.md",
        audio_cable_keyword="Hi-Fi Cable Input",
    ),
}


def get_speaker_config(name: str) -> SpeakerConfig:
    if name not in SPEAKER_CONFIGS:
        raise ValueError(f"Unknown character: {name}; available: {list(SPEAKER_CONFIGS.keys())}")
    return SPEAKER_CONFIGS[name]
