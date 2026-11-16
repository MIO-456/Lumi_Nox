# GitNexus Engineering Plan

> Task: 建立可本地运行、舞台优先的虚拟直播间，并提供可追踪的调试体验。
> Evidence verified at commit 60e674552adcd45ed59a633769b030d67b5cc2e5. GitNexus 1.6.12, analyze --index-only --pdg completed: 26238 nodes, 85467 edges, 197 clusters, 372 flows.
> Provenance is embedded verbatim in §11. WSL views Windows checkout file attributes differently; worktree bytes are authoritative. Windows Git reports clean before implementation. Generated plan path alone excluded.

## 1. Objective

单命令启动，浏览器内看到示例双角色、处理状态、字幕、本地观众输入、同屏五子棋与可筛选调试记录；保留真实服务配置。

## 2. Current Behaviour

[verified] public_host/server.py:48,73,121 supplies snapshots, starts manual/program sessions and queues requests. public_host/session.py:298 serializes generation/synthesis/playback and commits history only on successful completion. public_host/web/app.js:184 polls status. Default live/one role configuration conflicts with the programme's two-role Gomoku prerequisite (public_host/config.py:30).

## 3. Relevant Architecture

[verified] Session owns the only player and arbitration; Program owns stage lifecycle and scope cancellation. Activity snapshot already includes board state (public_host/activities/gomoku.py:153). Keep these boundaries and use additive read-model fields. Standard-library manual demo remains available. No cloud/device calls on page load.

## 4. GitNexus Findings

[graph] query -r E:/Lumi_Nox 'Session snapshot submit_text program' --limit 3 links renderStatus/renderProgramStatus and public host/program tests. context _run_turn --file public_host/session.py confirms Session._run caller and scheduler/arbiter callees. impact snapshot --file public_host/session.py --direction upstream --depth 2 --include-tests reports 8 direct dependents: Session.start/stop/interrupt; session test managed-session test and cleanup; program test cleanup; real Gomoku start_program and cleanup. Keep all old snapshot fields and run these suites. Graph is a lower bound: 23 unresolved receiver call sites, cross-language property gaps, process truncation. Source and full regression remain required.

## 5. Statement-Level PDG Findings

[graph] impact _run_turn --mode pdg --line 359 --direction upstream returned pdg-no-block-at-line, zero statement blocks; only callgraph bridge to _run. No statement dependence claims are made. [verified] source cancellation/generation checks before phase/player and completion commit must remain intact (session.py:350–415,424–440).

## 6. Proposed Changes

- [inferred] Add public_host/telemetry.py: bounded per-run request read model, stable event seq, request phase timings, pending/current text, terminal outcome, cancellation and level reset. Session.snapshot returns additive run_id/mode/roles/requests/current/event_seq; old messages/results unchanged. Event recording never performs network or filesystem I/O.
- [inferred] Session._submit records accepted requests immediately; _run_turn records selected role, model text before synthesis, phase transitions and sampled level. Cancellation clears stage text immediately even while provider returns late; terminal result remains authoritative. A consumed result does not erase display trace.
- [inferred] Add isolated demo preset/start path to HostApp: build an in-memory demo config with two generic roles, short script, no stored credentials/avatar. No saving over existing user config. Capability preflight exposes aiohttp availability; manual demo works without dependencies. Return effective mode/roles for refresh and second-tab recovery.
- [inferred] DemoPlayer uses optional clip duration for paced visible simulation while default direct-test demo stays fast. DemoChat provides short contextual samples for opening/chat/ending. Stop/interrupt remain responsive.
- [inferred] Rebuild index.html/style.css around a 16:9 stage, SVG/CSS generic role figures, activity canvas, subtitle, audience feed and controls. Move existing config/program/probe UI into expandable settings, preserve IDs and behavior where practical.
- [inferred] Add room.js for pure stage/board/request rendering. Consume activity.state rather than iframe or a second game engine. Stage only shows text for active playback (pending text clearly labelled), resets on cancel/stop/run change/disconnect. Single/two role layouts and reduced motion supported.
- [inferred] Extend app.js: sequential polling with reconnect state, bounded event cursor/merge and chronological sort, request/error filters, safe export whitelist omitting config/prompts/content by default; user input comes from accepted request trace immediately. Existing controls use actual server state, no optimistic completion. Optional run_id rejects stale new-client mutations while legacy API remains compatible.

## 7. Implementation Sequence

1. Create feature branch, telemetry model and focused cancellation/phase/run tests.
2. Wire Session tracing, then HostApp temporary demo/capabilities/run guard and HTTP tests. Preserve exact results/history semantics.
3. Build stage/room renderer, replace shell/CSS, integrate setup and sequential polling. Canvas is display only; renderer handles activity lifecycle.
4. Add request timeline and safe export, keyboard/responsive/reconnect behavior.
5. Update host/director/start/testing docs and release progress. Run targeted and full checks; browser exercise demo/chat/interrupt/refresh/two tabs/game/end/stop and narrow viewport. No live cloud/device validation claim.

## 8. Test Strategy

Existing test_public_host_session.py gates providers with Events; extend with pending visibility, pre-play text, phase durations, cancellation suppresses late output, bounded traces and restart run IDs. Extend test_public_host_server.py for isolated demo leaves saved config untouched, dependency preflight, status role/mode metadata, stale run control and sanitized export. Existing programme/Gomoku suites verify lifecycle and result compatibility. Add pure renderer tests with Node's built-in test runner if useful, and browser check real DOM and canvas. Commands: python -m unittest discover -s tests; python main.py; python -m compileall -q .; python scripts/check_docs.py; bash ./init.sh; node --check public_host/web/app.js and room.js.

## 9. Risk and Impact Analysis

[verified] All eight graph direct dependents rely on existing fields; additive snapshots avoid breaking them. [inferred] Telemetry lock ordering must remain Session lock then telemetry lock only. Bounded trace eviction never removes active/queued records or result bookkeeping. No audio output moved to browsers. Demo pacing isolated from existing short-duration programme tests. Captured timestamps are diagnostic, not scheduling authority. Failures/stopping do not claim completion. No new external dependencies or broad core refactor.

## 10. Files Expected to Change

public_host/{telemetry.py,session.py,server.py,providers.py,playback.py}; public_host/web/{index.html,style.css,app.js,room.js}; tests/test_public_host_{session,server}.py and focused room tests; docs/{PUBLIC_HOST,PUBLIC_DIRECTOR,GETTING_STARTED,TESTING,README}.md, README.md, README.en.md, progress.md, feature_list.json as warranted. Plan recorded under docs/plans/ and linked in doc index. No private configuration read or committed.

## 11. Reusable Implementation Context

```json
{
  "implementation_context": {
    "task_summary": "Local virtual livestream room",
    "evidence_provenance": {
      "schema_version": 2,
      "head_commit": "60e674552adcd45ed59a633769b030d67b5cc2e5",
      "generated_plan_path": "docs/plans/2026-11-16-gitnexus-plan-local-virtual-live-room.md",
      "global_dirty_digest": {
        "algorithm": "sha256",
        "canonicalization": "gitnexus-evidence-provenance-v2 NUL-framed UTF-8 records",
        "value": "56a1d3f7eb227d11b6faf863a7732e64a609f505f1e5ffb4e8d31b9879a31d28"
      },
      "cited_path_manifest": [
        {
          "path": "public_host/activities/gomoku.py",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:1c5af1e871f1d0a3e89fac14facbe183897a35e5b543fb8f731419b3e6402258",
          "index_digest": "sha256:1c5af1e871f1d0a3e89fac14facbe183897a35e5b543fb8f731419b3e6402258",
          "worktree_digest": "sha256:96391eb8cae82470ac94fec82b6c6e2a655cb8324f85b825b80c884118128291",
          "untracked_digest": "absent"
        },
        {
          "path": "public_host/config.py",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:cf7fb624ef816b8f6353a18792d4610e93be84da73ee8adadeab57fff0f235eb",
          "index_digest": "sha256:cf7fb624ef816b8f6353a18792d4610e93be84da73ee8adadeab57fff0f235eb",
          "worktree_digest": "sha256:fc259afcd63f7c8292ffb029ff6bce271281ab2e296545a9b7f56bdfd749569c",
          "untracked_digest": "absent"
        },
        {
          "path": "public_host/playback.py",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:4e744def8d74336e6c677d98ac47cedd8c9d9dd8fd177e61b3bd23c12365689e",
          "index_digest": "sha256:4e744def8d74336e6c677d98ac47cedd8c9d9dd8fd177e61b3bd23c12365689e",
          "worktree_digest": "sha256:4c4d4c044a1041d655486d6878c5fdf230fbc10d004995b92e2f290e4b8749f2",
          "untracked_digest": "absent"
        },
        {
          "path": "public_host/program.py",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:1e2074fd66530331a5b90b8246ed29c0b8f47e8d634b7435a4577077a3382331",
          "index_digest": "sha256:1e2074fd66530331a5b90b8246ed29c0b8f47e8d634b7435a4577077a3382331",
          "worktree_digest": "sha256:5e9d7387f42c82ff9dc19ed6d07a4a35ce0330f1eff8cfe5026d58739a20358e",
          "untracked_digest": "absent"
        },
        {
          "path": "public_host/providers.py",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:40a364f18a449ea11fddbf99a4a1b2438e7ccceba2bbb772ff329f0fe75b692d",
          "index_digest": "sha256:40a364f18a449ea11fddbf99a4a1b2438e7ccceba2bbb772ff329f0fe75b692d",
          "worktree_digest": "sha256:25d3bbcc6535df92a911f7670bfc048188f9ad9ff80439b2fbbb1bcaa33f6541",
          "untracked_digest": "absent"
        },
        {
          "path": "public_host/server.py",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:123cd43906fb81b87fd2062fc55b875aecb1abebe0e42c3284b32621eed68166",
          "index_digest": "sha256:123cd43906fb81b87fd2062fc55b875aecb1abebe0e42c3284b32621eed68166",
          "worktree_digest": "sha256:9ea17273fa2ef3d676042fde3f459d9172014123b72925977cb254cbdcbf920f",
          "untracked_digest": "absent"
        },
        {
          "path": "public_host/session.py",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:c690fc465310e71a0243658bd8bd1dc909c8a26a26298d6b56e4a1cd21d744ee",
          "index_digest": "sha256:c690fc465310e71a0243658bd8bd1dc909c8a26a26298d6b56e4a1cd21d744ee",
          "worktree_digest": "sha256:2d8162739d7918898688d6cc541131da7d71fee3548f0a2568f2c2d475fae0b0",
          "untracked_digest": "absent"
        },
        {
          "path": "public_host/web/app.js",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:0e4dbe5abf73d9813a26ad325f06d75c9d3f70baf2b117331b1a585ad7776562",
          "index_digest": "sha256:0e4dbe5abf73d9813a26ad325f06d75c9d3f70baf2b117331b1a585ad7776562",
          "worktree_digest": "sha256:185e0ca7e40b705f24a1dcd35e42c9aca37c7dd31a1c1076badbde8ea21bf8b4",
          "untracked_digest": "absent"
        },
        {
          "path": "public_host/web/index.html",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:16cb5dd8b72742c3464754d29b546a1755e70033db837c3eeb0647733f20284b",
          "index_digest": "sha256:16cb5dd8b72742c3464754d29b546a1755e70033db837c3eeb0647733f20284b",
          "worktree_digest": "sha256:5b558f167dd762ccedecb9a4044b31c95336d8dd5294a77c43d424d019d34c63",
          "untracked_digest": "absent"
        },
        {
          "path": "public_host/web/style.css",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:6310375d342f8adc49312e59c2e782e47fbd0d076dcece256093507902109794",
          "index_digest": "sha256:6310375d342f8adc49312e59c2e782e47fbd0d076dcece256093507902109794",
          "worktree_digest": "sha256:32493d01a7a6b2aa90943417a6bd40c54140b8b78f94a5a9db3082aed43007ce",
          "untracked_digest": "absent"
        },
        {
          "path": "tests/test_public_host_server.py",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:07189d4c53424e4ed2e466c9c2add2202c8d9392acd31fba19acf1303df55648",
          "index_digest": "sha256:07189d4c53424e4ed2e466c9c2add2202c8d9392acd31fba19acf1303df55648",
          "worktree_digest": "sha256:1765496caf94dced4918a67c3ec1705b38613d4c13294fa096c7c34346b409ba",
          "untracked_digest": "absent"
        },
        {
          "path": "tests/test_public_host_session.py",
          "object_kind": {
            "head": "regular",
            "index": "regular",
            "worktree": "regular",
            "untracked": "absent"
          },
          "state": "unstaged",
          "rename_from": null,
          "rename_to": null,
          "head_digest": "sha256:402375fd7c429839fd5d420d35596c8a7ab4d59dc479b21b4cce450c788f36bf",
          "index_digest": "sha256:402375fd7c429839fd5d420d35596c8a7ab4d59dc479b21b4cce450c788f36bf",
          "worktree_digest": "sha256:7c58a1811e1c43a0a53db36ec640297baff0ecf474e3c44a48f737eb11b225c4",
          "untracked_digest": "absent"
        }
      ]
    },
    "primary_symbols": [
      "Session._run_turn",
      "Session.snapshot",
      "HostApp.start",
      "renderStatus"
    ],
    "related_symbols": [],
    "execution_path": [
      "queue",
      "generate",
      "synthesize",
      "play",
      "terminal"
    ],
    "pdg_constraints": [],
    "architectural_patterns": [
      "single worker; preserve generation and history guards"
    ],
    "files_to_modify": [
      "public_host/session.py",
      "public_host/server.py",
      "public_host/web/app.js",
      "public_host/web/index.html",
      "public_host/web/style.css"
    ],
    "tests": [
      {
        "file": "tests/test_public_host_session.py",
        "scenarios": [
          "pending",
          "text",
          "cancel",
          "run identity"
        ]
      },
      {
        "file": "tests/test_public_host_server.py",
        "scenarios": [
          "config isolation",
          "stale run"
        ]
      }
    ],
    "verification_commands": [
      "python -m unittest discover -s tests",
      "python main.py",
      "python -m compileall -q .",
      "python scripts/check_docs.py",
      "bash ./init.sh",
      "node --check public_host/web/app.js"
    ],
    "risks": [
      "PDG unavailable",
      "graph lower bound",
      "nonblocking callback"
    ],
    "assumptions": [
      "generic figures accepted"
    ],
    "open_questions": [],
    "avoid": [
      "No second player",
      "No private config",
      "No commits or push"
    ]
  }
}
```

## 12. Assumptions and Open Questions

[assumed] Generic built-in role figures suffice for the first stage, as previously agreed. Actual Live2D/VTS video capture is deferred. [verified] No pause contract exists, so no pause button is introduced. ASR, platform chat, OBS, memory, cloud validation and additional games remain separate work. PDG statement evidence unavailable for Python target; current source and gated tests are the authority. WSL provenance may label file-mode differences dirty; no normalization of repository files is performed.

## 13. Definition of Done

One-command local entry with obvious silent demo; two roles, subtitles and input trace visible; real Gomoku board/score in same stage then returns to chat; interrupt/stop/run change removes stale speech; refresh/two tabs observe one worker; errors and missing dependencies visible; diagnostic export excludes secrets/configuration; offline regression and browser scenarios pass with actual results documented; no commit/push without user request.
