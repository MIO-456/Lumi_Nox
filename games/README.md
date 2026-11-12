# Game integrations

Each game lives with its bridge, decision logic and local assets. Commercial game
binaries are never included.

| Directory | Contents | Example entry point |
|---|---|---|
| `buckshot/` | Buckshot Roulette decision engine and TCP bridge | `python -m games.buckshot.bot` |
| `terraria/` | Terraria bridge, bundled focus-safe tModLoader mod, A* navigation and goal planner | `python -m games.terraria.bot --demo walk` |
| `kingdom_rush/` | Lua bridge, patcher and tower-defense AI | `python -m games.kingdom_rush.ai --dry-run` |
| `word_games/wordle/` | Wordle frontend and entropy solver | `python -m games.word_games.wordle.bot --no-llm` |
| `word_games/handle/` | Handle frontend and Chinese-idiom solver | `python -m games.word_games.handle.bridge` |

The word games share the isolated worker process in `word_games/solver_client.py`
and `word_games/solver_worker.py` so expensive entropy search cannot stall the host.

Terraria setup and the background-focus regression check are documented in
[`terraria/mod/README.md`](terraria/mod/README.md).

## 2026-11-12 module refresh

- Kingdom Rush adds `startup.py`: shared startup timeout/cancellation, handshake
  checks and owned-process cleanup. Target selection and skill use are checked
  against the observed game state. Consumers should retain/check `session_id`
  when handling asynchronous events.
  The Lua bridge also automatically pulses eligible level click interactions
  (such as thawing towers or boss interruption), without moving the OS mouse.
- Buckshot adds `duel_engine.py`, `duel_protocol.py` and `state_semantics.py` for
  role-specific information and causal action results. Solo remains the default;
  duel requires a compatible external game-side bridge, which is not included.
  Publishing these helpers is not a claim that a two-player game is turnkey.
  **Callback migration:** `decision_dispatched` means a command was sent;
  `decision_executed` is now reserved for a confirmed duel `action_executed`
  acknowledgement. Solo clients observing the old send notification should
  subscribe to `decision_dispatched`; dispatch does not prove execution.
- Wordle and Handle limit candidate sampling for large entropy searches. Rankings
  are approximate and may vary; this is an existing maintenance improvement,
  not a new game or a new contribution priority.
- Terraria keeps the public package's bundled mod, version checks and background
  focus fixes. This refresh does not replace them with the live project's layout.

The added regressions run without games, model services or credentials. Actual
launcher behavior, native bridges and gameplay need validation in the user's
game environment; the present update did not start any game.
