# Architecture

> **Read when**: changing coordination code (`core/`), turn-taking, the realtime voice path, or memory write-back.
> **Skip when**: you only need to run the demo — see [GETTING_STARTED](GETTING_STARTED.md).
> **Review**: when a subsystem's responsibility or the end-to-end data flow changes.

Lumi_Nox runs two AI characters as co-hosts. This document describes how the
published modules and public programme host fit together.

## Start here

Source modules use qualified imports under `core/`, `dialogue/`, `audio/` and
`settings/`; `public_host/`, root `main.py` and existing `games/` entry points
remain available. See [the layout and migration guide](PACKAGE_LAYOUT.md).

`main.py` is a runnable demo that wires the real coordination modules (event bus,
state machine, speaker scheduler, speech-output arbiter) and drives two example
characters through a short co-hosted segment — with no API keys, audio or models.
Run `python main.py` and read it alongside this document; it is the smallest
complete picture of how the parts connect.

Run `python -m public_host` for an interactive local UI at
`http://127.0.0.1:8765`. Select offline demo plus manual conversation for a
standard-library-only session; the UI defaults to programme mode, whose Gomoku
activity requires aiohttp. Configure your own chat and speech services for actual audio.
See the [public host guide](PUBLIC_HOST.md), [programme guide](PUBLIC_DIRECTOR.md)
and [VTS example](VTS_EXAMPLE.md).

## Public host data flow

The host wires a small, explicit text-to-speech path:

```text
Local web UI -> Programme (core/program + public_host/program)
                  |                  |
                  |             Gomoku lifecycle -> browser board
                  |                  |
Manual input -> Session queue <--- result instruction
                  |                  |
           Speaker scheduler -> Speech arbiter
                                                          |
                                                    Chat adapter
                                                          |
                                                     TTS adapter
                                                          |
                        optional VTS mouth input <- Audio playback
                                                          |
                                 completion / cancellation / failure
```

`public_host/server.py` serves the local configuration and controls. One session
worker in `session.py` handles one or two enabled characters, viewer text and
direct speech. The chosen character's prompt and conversation history go to the
chat adapter; direct speech skips the chat model. Automatic rotation is per
submitted message. Manual mode does not speak without input. Programme mode
submits automatic chat only in a chat segment and waits for each request's
terminal outcome before scheduling the next one.

The adapters in `providers.py` currently implement non-streaming
`/chat/completions` and `/audio/speech` returning a complete 16-bit PCM WAV.
`playback.py` waits for actual audio completion before committing a successful
turn. Cancellation invalidates the old turn so late service results cannot speak
or enter completed history; a blocked network call may still need its configured
timeout to return. Demo uses explicit simulated results instead of claiming audio
was played.

`avatar.py` optionally sends the playing audio's level to a configured VTS input.
First authorization is an explicit user action. Users load their own model in VTS;
the adapter checks its ID, stops on a detected model change, and closes only its
own connection. A pair of characters must use separate VTS ports. Models and
private motion assets are not bundled.

Programme mode uses `core/program/` for timing, advancement, transition ordering
and ready/active/stopped receipt checks. `public_host/program.py` performs the
public I/O. A private production-director candidate delegates to the same core;
its production deployment and real livestream verification are still pending.

The first activity is the existing Gomoku rules and local move algorithms in
`minigames/`, not LLM-driven play. Preparation makes the board available without
moving; activation starts the game. Match results follow the director's clock,
and result speech waits for exact request/task outcomes before moving on. A stop
timeout remains an error, not a completed transition. The browser platform is
reused between game segments and closed when the programme releases its activity.

This host does not automatically connect ASR, realtime voice, long-term memory,
other game modules, Bilibili input or OBS control. The following overview also
includes separate building blocks; starting the public host does not enable them.

## The core idea

The published modules support a realtime speech-to-speech path and a text-model
path with separate TTS. This is a curated snapshot, not a description of every
current production default. The characters stay aware of each other through **history mirroring**, take turns through a
**scheduler**, and are kept from talking over each other by a **speech-output
arbiter**. Everything is coordinated over an **event bus** anchored to a global
**state machine**.

## Data flow supported by the separate conversation modules

1. Viewer chat and partner hand-offs enter the **speaker scheduler**, which decides
   who speaks next (@-mentions, prioritized viewer queue, rotation).
2. The chosen character's **realtime session** (`audio/realtime_chat.py`) produces
   speech-to-speech output; audio is attributed to that speaker and routed to its
   own output device. (`dialogue/conversation.py` orchestrates the equivalent turn for the
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
| Public host | `public_host/` | Local configuration and UI, explicit chat/TTS adapters, one- or two-character session lifecycle, audio playback and optional VTS mouth movement. |
| Programme | `core/program/`, `public_host/program.py` | Shared timing and transition decisions, plus public stage/request ownership and activity I/O. |
| Gomoku | `minigames/`, `public_host/activities/gomoku.py` | Local rule-based moves, match scoring, exact result speech completion, and a loopback browser board. Requires two distinct enabled roles and optional aiohttp. |
| Coordination | `core/event_bus.py`, `core/state_machine.py` | In-process pub/sub + request/response, and the single source of truth for what the stream is doing. |
| Turn-taking | `core/speaker_scheduler.py`, `core/speech_output_arbiter.py` | Who speaks next; and one voice at a time (QUEUE / DROP / INTERRUPT). |
| Realtime voice | `audio/realtime_chat.py`, `audio/realtime_chat_protocol.py` | Dual end-to-end session pool over doubao SC2.0; attribution; audio routing; cross-character history mirroring. |
| Turn orchestration | `dialogue/conversation.py`, `dialogue/web_search.py`, `dialogue/viewer_name.py` | Drives a turn for the text pipeline and formats the cross-character mirror messages; augments a reply with live web search when the turn needs it, and normalizes / folds viewer names before they reach the model. |
| Brain | `dialogue/fast_brain.py` | Per-character lightweight LLM for tool-driven decisions (e.g. game moves). |
| Voice out | `audio/lumi_tts.py`, `audio/cosyvoice_tts.py`, `audio/tts_emitter.py`, `core/run_architecture.py`, `settings/voice_registry.py` | Streaming TTS with user-supplied voice ids; `core/run_architecture.py` selects the text-vs-realtime path, while the public voice registry reads local environment variables and never contains production voice ids. |
| Voice in | `audio/lumi_asr.py` | Streaming speech recognition. |
| Independent Qwen output | `audio/qwen3_tts.py`, `audio/qwen_audio_tts.py`, `audio/role_volume.py`, `audio/speech_pauses.py`, `audio/spoken_output.py` | Complete-sentence PCM, ordered playback, cancellation and outcome reporting; explicit service and device setup. These modules are not automatically wired into the older public launcher. See [voice setup](VOICE_MODULES.md). |
| Memory | `memory/` | Per-viewer and self memory in SQLite, distilled by an LLM extractor/summarizer. `decay.py` hides stale unpinned facts at read time without deleting them; `guard_facts.py` deterministically protects membership identity facts. The database itself is never published. |
| Games | `games/` | Game segments grouped by integration: Buckshot Roulette; Terraria (bundled focus-safe tModLoader bridge, A* pathfinding and five-layer goal planner; see `games/terraria/mod/README.md` and `docs/games/terraria-behavior-tree.md`); Kingdom Rush (LuaJIT bridge and per-wave LLM strategist; see `docs/games/kingdom-rush-reverse-engineering.md`); and the Wordle/Handle frontends with a shared isolated entropy-solver worker. Commercial game binaries are not included. |
| Per-character config | `settings/voice_config.py` | Voice, avatar model, subtitle, audio routing (placeholder example characters). |
| Prototype boundaries | `interfaces/`, `examples/` | Extracted lightweight game protocol, drawing message shapes and song asset records, with synthetic offline samples. These examples do not start the Gomoku host, a drawing server or a singing service; see [prototype guide](PROTOTYPE_INTERFACES.md). |

## Configuration and credentials

Request/response subscribers now execute on a daemon worker so slow callbacks do
not hold up the response wait. A timeout rejects late responses; it cannot kill a
callback or undo its side effects. Consumers can check `event.is_active` before
acting. Ordinary `publish()` remains synchronous.

Kingdom Rush startup shares one cancellation/deadline context and verifies the
bridge handshake. Stop targets only the process owned by that startup. Buckshot
includes independent duel decision/protocol helpers, but a compatible game-side
duel bridge is not bundled. See [game notes](../games/README.md).

The public host keeps its own configuration and credentials in ignored
`.local-host/` files (or the explicitly selected `--data-dir`). It never loads a
parent `.env`, private production configuration or production VTS tokens. The UI
does not receive saved API keys; users configure their own service endpoints,
models and voices. See [the host guide](PUBLIC_HOST.md) for storage and setup.

For the older modules, `settings/voice_config.py` holds per-character runtime config; the example characters ship
with placeholder values. `settings/voice_registry.py` resolves optional cloned voices from
the user's environment and falls back safely when none are supplied. Their API keys
are read from environment variables (see `.env.example`). Actual credentials are
not distributed in the repository.

## What's here, and what's not

This repository contains the generic public host, its basic VTS adapter, the
orchestration core, the realtime dual-AI engine, the
voice (TTS) and hearing (ASR) layers, long-term memory, the programme console and
runnable Gomoku, plus separate integrations for Buckshot Roulette, Terraria,
Kingdom Rush, Wordle and Handle.

Intentionally **not** included:

- The characters' persona prompts, IP and worldview (the closed "soul").
- Live2D model files and private motion/expression assets. The generic VTS
  authentication and audio-level mouth adapter is public; see [VTS setup](VTS_EXAMPLE.md).
- Production voice ids, voiceprints, viewer databases, logs and operations data.
- The commercial game binaries the game bridge talks to.
- The remaining game bot (Fireboy & Watergirl), vision and drawing runtimes, and
  production-specific platform/device wiring. The generic programme console,
  shared scheduling core and Gomoku host are included.

## Open-core

The framework is open. Character persona prompts, IP and worldview remain private.
