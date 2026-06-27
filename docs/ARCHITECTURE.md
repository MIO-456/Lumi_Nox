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

Each character runs its own real-time speech-to-speech session. The characters
stay aware of each other through **history mirroring**, take turns through a
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
| Voice out | `lumi_tts.py`, `cosyvoice_tts.py`, `tts_emitter.py`, `run_architecture.py` | Streaming TTS with voice-cloned timbres; `run_architecture.py` is the single source of truth for the text-vs-realtime pipeline that `tts_emitter.py` follows to pick the output path. |
| Voice in | `lumi_asr.py` | Streaming speech recognition. |
| Memory | `memory/` | Per-viewer and self memory in SQLite, distilled by an LLM extractor/summarizer. |
| Games | `buckshot_*.py`, `terraria_*.py`, `kingdom_rush_*.py`, `wordle_*.py`, `handle_*.py` | Game segments the AIs play over a bridge while narrating. Buckshot Roulette (turn-based), Terraria (A* pathfinding + five-layer goal planner over a tModLoader mod; see `terraria-behavior-tree.md`), Kingdom Rush (tower-defense AI via a LuaJIT mod reverse-engineered into the game's LÖVE engine; see `kingdom-rush-reverse-engineering.md`), and two word games — Wordle and Handle (汉兜, a Chinese-idiom Wordle), each a self-contained web frontend + an entropy solver that runs in a separate worker process (`solver_worker.py` / `solver_client.py`) so the heavy mid-game search never stalls the main loop. Kingdom Rush pairs its algorithmic tower-placement skeleton with a per-wave LLM strategist (`kr_strategy_llm.py`) that reads the in-game bestiary and adapts the build to each wave. |
| Per-character config | `voice_config.py` | Voice, avatar model, subtitle, audio routing (placeholder example characters). |

## Configuration and credentials

`voice_config.py` holds per-character runtime config; the example characters ship
with placeholder values. All API keys are read from environment variables (see
`.env.example`) — none are stored in the repository.

## What's here, and what's not

This repository contains the orchestration core, the realtime dual-AI engine, the
voice (TTS) and hearing (ASR) layers, long-term memory, and five games as a
showcase (Buckshot Roulette, Terraria, Kingdom Rush, Wordle and Handle).

Intentionally **not** included:

- The characters' persona prompts, IP and worldview (the closed "soul").
- The Live2D avatar / motion / expression layer — tied to specific character models.
- The commercial game binaries the game bridge talks to.
- The remaining game bot (Fireboy & Watergirl), vision, drawing, and the live
  director / control console — opened incrementally over time.

## Open-core

The framework is open. The characters' persona prompts, IP and worldview are
intentionally closed — the code is open, the characters are not.
