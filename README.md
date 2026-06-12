# Lumi_Nox

> **Two AI characters co-hosting one live show — the engine and orchestration that let them share a stage.**

*Battle-tested by a real daily livestream.*

![Two AI characters co-hosting a livestream](docs/assets/hero.png)

**▶ Watch Lumi & Nox co-host live on Bilibili: [Lumi和Nox-AI搭档](https://space.bilibili.com/544387533)**

## What it does

Lumi and Nox are two AI VTubers who host a livestream together. They hold a
real-time voice conversation with *each other*, banter, react to viewer chat,
remember their regulars across streams, and play games together — in the spirit
of the Neuro × Evil dynamic.

## Try it now (zero setup)

```bash
python main.py
```

No API keys, audio hardware or models required. `main.py` is a runnable taste of
the **real** coordination core: it drives two example characters through the real
scheduler and speech arbiter, so you can watch the turn-taking, @-mention routing
and one-voice-at-a-time logic run in your terminal in a few seconds. The LLM and
voice are swapped for tiny stand-ins here; the production engine lives in the
files below.

## How it works

- **Realtime dual-session engine** (`realtime_chat.py`, `realtime_chat_protocol.py`)
  — each character runs on its own end-to-end speech-to-speech session (doubao
  SC2.0 over websocket); audio is attributed and routed per speaker, so two
  characters can be live at the same time.
- **Turn orchestration & cross-character mirroring** (`conversation.py`,
  `speaker_scheduler.py`) — who speaks next is decided live from @-mentions,
  partner hand-offs and a prioritized viewer-chat queue; what one says is mirrored
  into the other's context as a stage note, so neither mistakes its partner's
  lines for its own.
- **Speech-output arbitration** (`speech_output_arbiter.py`) — only one voice holds
  the floor at a time (QUEUE / DROP / INTERRUPT), released only when a character's
  *audio* has truly finished, so the two never talk over each other.
- **Voice** (`lumi_tts.py`, `cosyvoice_tts.py`, `tts_emitter.py`) — streaming
  text-to-speech with voice-cloned timbres (CosyVoice), or borrowed from the
  realtime engine so both pipelines sound identical.
- **Hearing** (`lumi_asr.py`) — streaming speech recognition for live voice input.
- **Long-term memory** (`memory/`) — per-viewer and self memory in SQLite, distilled
  by an LLM, so the characters recognize regulars and stay consistent across streams.
- **Playing games** (`buckshot_*.py`, `terraria_*.py`, `kingdom_rush_*.py`,
  `wordle_*.py`, `handle_*.py`) — bridges that let the AIs play games as stream
  segments, making decisions and calling tools while they narrate. **Buckshot
  Roulette** (turn-based), **Terraria** (**A\* pathfinding** + a **five-layer goal
  planner** over a tModLoader mod), **Kingdom Rush** — a tower-defense AI driven by a
  **LuaJIT mod reverse-engineered into the game's LÖVE engine** (see
  [docs/kingdom-rush-reverse-engineering.md](docs/kingdom-rush-reverse-engineering.md))
  — and two word games, **Wordle** and **Handle** (汉兜, a Chinese-idiom Wordle), each
  a self-contained web frontend + solver.
- **Fast brain** (`fast_brain.py`) — a per-character lightweight LLM for tool-driven
  decisions alongside the realtime voice chat.
- **Coordination backbone** (`event_bus.py`, `state_machine.py`) — every module talks
  through an in-process event bus anchored to one global state machine.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## Repository layout

```
main.py                    # runnable demo — two AI characters co-hosting (no keys needed)
realtime_chat.py           # dual end-to-end session pool, attribution, audio routing
realtime_chat_protocol.py  # doubao SC2.0 websocket protocol codec
conversation.py            # turn orchestration + cross-character history mirroring
fast_brain.py              # per-character lightweight LLM for tool-driven decisions
speaker_scheduler.py       # who-speaks-next: @-mentions, hand-offs, viewer queue
speech_output_arbiter.py   # one-voice-at-a-time arbitration (QUEUE/DROP/INTERRUPT)
event_bus.py               # in-process pub/sub + request/response
state_machine.py           # global stream state + transitions
voice_config.py            # per-character runtime config (example characters)
lumi_tts.py                # streaming TTS + subtitles
cosyvoice_tts.py           # CosyVoice voice-cloned synthesizer
tts_emitter.py             # picks the voice output path per run architecture
lumi_asr.py                # streaming speech recognition
memory/                    # long-term per-viewer & self memory (SQLite + LLM extraction)
buckshot_bot.py            # Buckshot Roulette: decision engine
buckshot_bridge.py         # TCP bridge to the game
buckshot_prompt_context.py # game state -> prompt context
terraria_bot.py            # Terraria bot: A* pathfinding + five-layer goal planning
terraria_bridge.py         # TCP bridge to a tModLoader mod
kingdom_rush_ai.py         # Kingdom Rush: tower-defense AI / strategy
kingdom_rush_bot.py        # game-loop driver
kingdom_rush_bridge.py     # Python side of the TCP bridge
kingdom_rush_bridge.lua    # LuaJIT mod injected into the game's LOVE engine
kr_strategy_llm.py         # LLM strategy hook
kr_battle_history.py       # Kingdom Rush battle-history tracking
patch_kingdom_rush.py      # injects the bridge mod into a local game install
wordle_bot.py / wordle_bridge.py / wordle_engine.py / wordle.html      # Wordle solver + self-contained frontend
handle_bridge.py / handle_engine.py / handle.html                      # Handle (汉兜, Chinese-idiom Wordle)
docs/ARCHITECTURE.md       # full design
docs/terraria-behavior-tree.md            # Terraria bot's atomic-behavior architecture
docs/kingdom-rush-reverse-engineering.md  # the LuaJIT reverse-engineering notes
```

## Getting started

The coordination layer and the `main.py` demo are pure standard library. To run
the real voice engine, install the dependencies and provide your own keys:

```bash
pip install -r requirements.txt
cp .env.example .env     # then fill in your doubao / DashScope keys
```

### What you need to bring

This repo is the **open-core** of a system that runs a live show every day — the
architecture and engineering are here to read and build on, not a turnkey product.
Running the full thing needs the pieces below: some are yours to bring, some are
intentionally kept closed.

- **API keys** (only to run the live voice — the `main.py` demo needs none). The
  LLM brain takes any **OpenAI-compatible** endpoint (doubao ARK, DashScope,
  OpenAI, …). The realtime speech-to-speech voice currently uses **doubao SC2.0**
  and the streaming TTS/ASR use **DashScope** (CosyVoice / fun-asr). Put your keys
  in `.env`.
- **A Live2D model** — the avatar / motion / expression layer is tied to specific
  character models and is **not** included; bring your own and wire it in.
- **The game** — the game bridge talks to a commercial game over TCP; you supply
  the game itself.
- **Personas** — `voice_config.py` ships placeholder example characters; the real
  Lumi / Nox persona prompts and worldview are intentionally closed.

Vision, drawing, the live director / control console, and the other game bots run
in the production system and are opened incrementally (see Roadmap).

## Roadmap

This is an open window into a real, running system — not a product roadmap. More of
it opens over time as the daily stream evolves: more game bots (Wordle, Handle, …)
and other subsystems, as time allows.

## License & open-core

[MIT](LICENSE). Open-core: the engine, games and architecture are here to read and
build on; the characters' persona, IP, worldview and operations data are not.
