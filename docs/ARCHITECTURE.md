# Architecture

Lumi_Nox runs two AI characters as co-hosts of a single livestream. This document
describes how the pieces in this repository fit together to let two AIs share one
stage in real time.

## Start here

`main.py` is a runnable demo that wires the real coordination modules (event bus,
state machine, speaker scheduler, speech-output arbiter) and drives two example
characters through a short co-hosted segment — with no API keys, audio or models.
Run `python main.py` and read it alongside this document; it is the smallest
complete picture of how the parts connect.

## The core idea

The published modules support a realtime speech-to-speech path and a text-model
path with separate TTS. This is a curated snapshot, not a description of every
current production default. The characters stay aware of each other through **history mirroring**, take turns through a
**scheduler**, and are kept from talking over each other by a **speech-output
arbiter**. Everything is coordinated over an **event bus** anchored to a global
**state machine**.

## Data flow of a chat turn

1. Viewer chat and partner hand-offs enter the **speaker scheduler**, which decides
   who speaks next (@-mentions, prioritized viewer queue, rotation).
2. The chosen character's **realtime session** (`realtime_chat.py`) produces
   speech-to-speech output; audio is attributed to that speaker and routed to its
   own output device. (`conversation.py` orchestrates the equivalent turn for the
   text pipeline.)
3. Before any audio plays, the speaker requests the floor from the **speech-output
   arbiter**; only one speaker holds it at a time.
4. When the character has *truly* finished speaking (audio done, not merely text),
   the arbiter releases the floor and the scheduler advances.
5. What was said is mirrored into the partner's history as a stage note, so the
   other character can react on its next turn without confusing the line for its own.
6. After the session, the **memory** subsystem distills durable facts about the
   viewer (and the character's own continuity) into SQLite for future streams.

## Subsystems

| Area | Files | Responsibility |
|---|---|---|
| Coordination | `event_bus.py`, `state_machine.py` | In-process pub/sub + request/response, and the single source of truth for what the stream is doing. |
| Turn-taking | `speaker_scheduler.py`, `speech_output_arbiter.py` | Who speaks next; and one voice at a time (QUEUE / DROP / INTERRUPT). |
| Realtime voice | `realtime_chat.py`, `realtime_chat_protocol.py` | Dual end-to-end session pool over doubao SC2.0; attribution; audio routing; cross-character history mirroring. |
| Turn orchestration | `conversation.py`, `web_search.py`, `viewer_name.py` | Drives a turn for the text pipeline and formats the cross-character mirror messages; augments a reply with live web search when the turn needs it, and normalizes / folds viewer names before they reach the model. |
| Brain | `fast_brain.py` | Per-character lightweight LLM for tool-driven decisions (e.g. game moves). |
| Voice out | `lumi_tts.py`, `cosyvoice_tts.py`, `tts_emitter.py`, `run_architecture.py`, `voice_registry.py` | Streaming TTS with user-supplied voice ids; `run_architecture.py` selects the text-vs-realtime path, while the public voice registry reads local environment variables and never contains production voice ids. |
| Voice in | `lumi_asr.py` | Streaming speech recognition. |
| Independent Qwen output | `qwen3_tts.py`, `qwen_audio_tts.py`, `role_volume.py`, `speech_pauses.py`, `spoken_output.py` | Complete-sentence PCM, ordered playback, cancellation and outcome reporting; explicit service and device setup. These modules are not automatically wired into the older public launcher. See [voice setup](VOICE_MODULES.md). |
| Memory | `memory/` | Per-viewer and self memory in SQLite, distilled by an LLM extractor/summarizer. `decay.py` hides stale unpinned facts at read time without deleting them; `guard_facts.py` deterministically protects membership identity facts. The database itself is never published. |
| Games | `games/` | Game segments grouped by integration: Buckshot Roulette; Terraria (bundled focus-safe tModLoader bridge, A* pathfinding and five-layer goal planner; see `games/terraria/mod/README.md` and `docs/games/terraria-behavior-tree.md`); Kingdom Rush (LuaJIT bridge and per-wave LLM strategist; see `docs/games/kingdom-rush-reverse-engineering.md`); and the Wordle/Handle frontends with a shared isolated entropy-solver worker. Commercial game binaries are not included. |
| Per-character config | `voice_config.py` | Voice, avatar model, subtitle, audio routing (placeholder example characters). |
| Prototype boundaries | `interfaces/`, `examples/` | Extracted lightweight game protocol, drawing message shapes and song asset records, with synthetic offline samples. No game host, drawing server or singing service is included; see [prototype guide](PROTOTYPE_INTERFACES.md). |

## Configuration and credentials

Request/response subscribers now execute on a daemon worker so slow callbacks do
not hold up the response wait. A timeout rejects late responses; it cannot kill a
callback or undo its side effects. Consumers can check `event.is_active` before
acting. Ordinary `publish()` remains synchronous.

Kingdom Rush startup shares one cancellation/deadline context and verifies the
bridge handshake. Stop targets only the process owned by that startup. Buckshot
includes independent duel decision/protocol helpers, but a compatible game-side
duel bridge is not bundled. See [game notes](../games/README.md).

`voice_config.py` holds per-character runtime config; the example characters ship
with placeholder values. `voice_registry.py` resolves optional cloned voices from
the user's environment and falls back safely when none are supplied. All API keys
are read from environment variables (see `.env.example`) — none are stored in the
repository.

## What's here, and what's not

This repository contains the orchestration core, the realtime dual-AI engine, the
voice (TTS) and hearing (ASR) layers, long-term memory, and five games as a
showcase (Buckshot Roulette, Terraria, Kingdom Rush, Wordle and Handle).

Intentionally **not** included:

- The characters' persona prompts, IP and worldview (the closed "soul").
- The Live2D avatar / motion / expression layer — tied to specific character models.
- Production voice ids, voiceprints, viewer databases, logs and operations data.
- The commercial game binaries the game bridge talks to.
- The remaining game bot (Fireboy & Watergirl), vision, drawing, and the live
  director / control console — opened incrementally over time.

## Open-core

The framework is open. The characters' persona prompts, IP and worldview are
intentionally closed — the code is open, the characters are not.
