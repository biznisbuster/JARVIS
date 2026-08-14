# JARVIS — Production Readiness & Stabilization Plan

> **Purpose:** executable engineering roadmap for stabilizing JARVIS before
> substantial new feature development.
>
> **Audience:** human developer or coding AI agent.
>
> **Rule:** execute this plan **phase by phase**. Do not attempt a single
> “rewrite everything” change.
>
> **Companion document:** `DEVELOPER-GUIDE.md`
>
> **Audit date:** 2026-08-13

---

# 0. How an AI coding agent must use this plan

This section is mandatory.

If this file is given to an AI coding agent, use the following operating
contract.

## AI execution contract

For every phase:

1. Read the complete phase before editing.
2. Inspect the named files and current tests.
3. Confirm the bug/design issue still exists in the current branch.
4. Add or update regression tests **before or together with the fix**.
5. Make only changes required by the current phase.
6. Do not opportunistically rewrite unrelated modules.
7. Run the phase validation commands.
8. Fix failures caused by the phase.
9. Summarize:
   - files changed,
   - root cause,
   - implementation,
   - tests run,
   - remaining risks.
10. Stop at the phase checkpoint unless explicitly instructed to continue.
11. Mark completed checklist items in this document if the workflow allows
    documentation updates.
12. Prefer one PR per major phase.

If new serious bugs are discovered:

- record them in **Appendix A — Newly discovered issues**,
- assign P0/P1/P2,
- do not derail the active phase unless the bug blocks it or is P0.

## Forbidden execution pattern

Do **not**:

```text
refactor media
+ rewrite agent loop
+ migrate config
+ change frontend store
+ redesign local models
+ change all tool schemas
```

in one PR.

The point of this roadmap is to preserve debuggability.

---

# 1. Success criteria

JARVIS is considered to have a stable development foundation when all of the
following are true:

- YT Music play/pause/resume/next/previous are verified against real state.
- There is one authoritative media state path.
- YTM browser failure automatically degrades/reconnects cleanly.
- Local model fragmented tool calls are assembled correctly.
- Local tool capability detection validates real structured tool calls.
- A local model cannot be selected for chat before it is ready.
- Local-to-cloud fallback is explicit in both backend events and UI.
- Tool execution has a consistent result/error contract.
- Tools have centralized timeout handling.
- `agent/tools.py` is no longer the implementation home for unrelated
  domains.
- Existing `.env` is never silently overwritten by setup.
- Clean setup installs dependencies required by default configuration.
- Application lifecycle closes long-lived resources.
- Session/state persistence is safe under concurrent activity.
- Frontend asynchronous state does not race backend readiness.
- Core behavior has regression tests.
- CI runs backend and frontend validation on every PR.
- Runtime versions/defaults/documentation agree.
- Important failures can be diagnosed from structured logs/events.

---

# 2. Current confirmed/high-confidence findings

## P0-1 — YTM next/previous fallback can pause playback

Current desktop fallback sends:

```text
next/previous key
then Space
```

Space is a play/pause toggle.

If the next track starts normally, the second command can pause it.

Relevant area:

```text
jarvis/agent/tools.py
_ytm_send_transport()
```

Required fix:

- never blindly send play/pause toggle after next/previous,
- verify track transition,
- retry the intended command or use another adapter.

---

## P0-2 — YTM has competing control/state paths

There are currently overlapping concepts:

```text
jarvis/media/ytm_web.py      headless Playwright YTM player
jarvis/agent/tools.py        desktop Safari Web App / Quartz / AppleScript
_YTM_STATE                   mirrored state
jarvis/media/nowplaying.py   generic macOS now-playing
```

This creates ambiguous playback ownership.

Target:

```text
MediaService
  -> YtmWebAdapter
  -> YtmDesktopAdapter
  -> optional SystemMediaAdapter
```

One service owns verification and state.

---

## P0-3 — Local Ollama tool-call streaming is assembled incorrectly

Cloud streaming maintains indexed tool-call slots and assembles fragments.

The local Ollama streaming path currently appends tool calls encountered in
each streaming delta.

A model that streams one call over multiple chunks can therefore produce
duplicate/incomplete calls.

Relevant files:

```text
jarvis/llm.py
jarvis/local_models.py
```

Target:

- one shared `ToolCallAccumulator`,
- provider-independent canonical `ToolCall[]`.

---

## P0-4 — Local tool capability probe can produce false positives

Current behavior treats a successful HTTP response as tool support.

That only proves the endpoint accepted the request.

The model must return a valid structured tool call for the probe to count as
tool capable.

---

## P1-1 — Local model UI selection races loading

Frontend currently sets the chosen local model as active before model loading
is confirmed.

Backend may therefore see:

```text
selected local model
but RUNNER not ready
```

and fall back to cloud.

User perceives this as inconsistent local-model behavior.

Relevant file:

```text
web-ui/src/lib/actions.ts
```

---

## P1-2 — World state and YTM state may disagree

`context.py` reads generic now-playing state while YTM has a dedicated DOM
state reader because generic MediaRemote behavior is known to be unreliable
for the YTM web-app case.

Target: `context.py` reads `MediaService.get_state()`.

---

## P1-3 — `agent/tools.py` is a god module

It currently combines unrelated domains:

- reminders,
- calendar,
- browser/web search,
- YouTube,
- YT Music,
- Quartz,
- AppleScript,
- clipboard,
- volume,
- Kilo,
- schemas,
- registry.

This makes regression risk grow with every tool.

---

## P1-4 — setup can overwrite user configuration

`scripts/setup.sh` rebuilds existing `.env` values from `.env.example` except
for values heuristically treated as secrets.

This can silently reset user settings.

Target rule:

```text
.env absent  -> create
.env exists  -> do not mutate
```

---

## P1-5 — default TTS dependency mismatch

The configured default TTS backend is Edge, but `requirements.txt` currently
contains a malformed/commented optional-install line where `edge-tts` is not
installed as a normal dependency.

A clean install can therefore fail with the default configuration.

---

## P1-6 — config/version/documentation drift

Observed runtime/docs version/defaults are not consistently sourced.

Target:

- one version constant,
- one runtime default source,
- docs/examples updated in same PR when defaults change.

---

## P1-7 — application shutdown does not centrally close all resources

YT Music Playwright exposes shutdown logic, but lifespan cleanup is not yet a
single comprehensive service shutdown path.

Long-running shared HTTP clients/background tasks should also be closed.

---

# 3. Phase 0 — Establish the safety net

**Priority:** P0  
**Goal:** make every following refactor measurable and reversible.

## Tasks

- [x] Run current backend test suite and record baseline.
- [x] Run frontend typecheck/build and record baseline.
- [x] Add missing regression tests for already-confirmed defects.
- [x] Add a deterministic fake-adapter test layer where platform services are
      currently difficult to test.
- [x] Ensure tests can run without real Google/YTM/Ollama credentials.

## Required regression tests

- [x] `test_next_does_not_send_unconditional_toggle`
- [x] `test_previous_does_not_send_unconditional_toggle`
- [x] `test_local_fragmented_tool_call_is_one_call`
- [x] `test_local_multiple_tool_calls_are_assembled_by_index`
- [x] `test_tool_capability_probe_requires_real_tool_call`
- [x] `test_local_model_not_active_until_ready`
- [x] `test_dead_ytm_page_is_not_reported_ready`
- [ ] `test_world_state_uses_authoritative_media_state` after MediaService
      exists.

The MediaService-dependent world-state test remains deferred until Phase 2,
because the service does not exist in the current implementation. The known
unfixed defects are strict expected-failure characterization tests in Phase 0;
they will become passing regression tests when their scoped fixes land.

## Validation

Backend:

```bash
pytest
ruff check .
```

If formatter is part of project policy:

```bash
ruff format --check .
```

Frontend:

```bash
cd web-ui
npm ci
npm run typecheck
npm run build
```

## Definition of done

- Baseline failures are documented.
- Confirmed defects have reproducing tests where practical.
- No behavior refactor yet except test-enabling seams.

## Checkpoint

STOP and report baseline before Phase 1.

## Phase 0 report (2026-08-14)

### Completed

- Recorded the pre-change backend and frontend baselines.
- Added deterministic fake Ollama transports for streaming and capability-probe tests.
- Added regression characterization for the known YTM transport defects.
- Added local fragmented/multiple tool-call and capability-probe regression coverage.
- Added the dead-page readiness regression.
- Added a frontend Vitest safety-net test for local model activation before readiness.
- Confirmed the suite does not require real Google, YT Music or Ollama credentials.
- No production behavior or architecture was changed in Phase 0.

### Files changed

- `tests/fakes/__init__.py`
- `tests/fakes/ollama.py`
- `tests/test_local_models.py`
- `tests/test_phase0_media_regressions.py`
- `tests/test_ytm_web.py`
- `web-ui/package.json`
- `web-ui/package-lock.json`
- `web-ui/src/lib/model-selection.test.ts`
- `PRODUCTION-READINESS-PLAN.md`

### Root causes fixed

- None. Phase 0 intentionally preserves the known defects and records them as
  strict expected-failure tests for their later scoped phases.

### Tests

Baseline before Phase 0 changes:

- `./.venv/bin/pytest` -> PASS (`95 passed`)
- `npm ci` -> PASS
- `npm run typecheck` -> PASS
- `npm run build` -> PASS
- `./.venv/bin/ruff check .` -> FAIL (6 pre-existing findings)
- `./.venv/bin/ruff format --check .` -> FAIL (8 pre-existing files)

Final Phase 0 validation:

- `./.venv/bin/pytest` -> PASS (`96 passed, 5 xfailed`)
- `./.venv/bin/ruff check tests/fakes tests/test_local_models.py tests/test_phase0_media_regressions.py` -> PASS
- `npm ci` -> PASS
- `npm run typecheck` -> PASS
- `npm run test` -> PASS (1 expected-failure test)
- `npm run build` -> PASS
- `./.venv/bin/ruff check .` -> FAIL (same 6 pre-existing findings; no new findings in Phase 0 files)
- `./.venv/bin/ruff format --check .` -> FAIL (same 8 pre-existing files; no new formatting issue in Phase 0 files)
- `git diff --check` -> FAIL (pre-existing trailing whitespace in the
  user-modified `AGENTS.md`)
- `npm audit --omit=dev` -> NOT VERIFIED (npm advisory endpoint DNS was
  unavailable in the restricted network environment)

### Manual validation

- Real macOS YT Music `play`, `pause`, `resume`, `next` and `previous` remain
  required in Phase 1; Phase 0 deliberately does not alter them.
- Real Ollama fragmented/multiple tool calls and capability behavior remain
  required in Phase 3.
- Real local-model UI selection during loading remains required in Phase 4.
- The world-state authoritative-media test remains deferred until MediaService
  exists in Phase 2.

### Remaining risks

- The five strict xfails reproduce the known Phase 1/3 defects and are not yet
  passing regression tests.
- The frontend readiness test is an expected-failure characterization until
  Phase 4 changes `onModelChange` semantics.
- Full Ruff validation remains red on pre-existing code/docs findings.
- `npm ci` reports 5 dependency vulnerabilities (3 moderate, 1 high, 1
  critical); advisory detail could not be retrieved in the restricted network
  environment.

### New issues discovered

- See Appendix A for the frontend dependency-audit finding.

### Ready for next phase

YES — Phase 0 checkpoint is complete. Stop here; do not begin Phase 1 without
explicit approval.

---

# 4. Phase 1 — Fix immediate YT Music correctness bugs

**Priority:** P0  
**Goal:** stop returning success for logically incorrect transport behavior.

## Files to inspect

```text
jarvis/agent/tools.py
jarvis/media/ytm_web.py
jarvis/media/nowplaying.py
tests/test_ytm_web.py
YT Music related tool tests
```

## Tasks

- [x] Remove unconditional Space/play toggle after `next`.
- [x] Remove unconditional Space/play toggle after `previous`.
- [x] Capture state before transition.
- [x] Verify next/previous using changed track identity when possible.
- [x] If track identity is unavailable, use a clearly documented degraded
      verification strategy.
- [x] Do not repeat a delivered `next`/`previous` command when verification is
      unavailable or proves that no transition occurred.
- [x] Allow bounded additional state reads after delivery, and allow fallback
      transports only when the prior transport did not deliver the command.
- [x] Do not set mirrored state to playing merely because a key event was
      delivered.
- [x] Return `ok=False` when no channel can verify the intended effect.
- [x] Add adapter/method metadata to diagnostic result.
- [x] Reject generic macOS now-playing evidence as YT Music verification.
- [x] Make `tool_done` reflect structured tool result status when available.
- [x] Preserve current working play/pause behavior while strengthening
      verification.
- [x] Add explicit per-device YT Music connection states separate from page,
      search and player readiness.
- [x] Add a headed persistent-profile Connect YT Music flow with safe backend
      status APIs and frontend backend-truth display.
- [x] Allow connected/search-ready pages without a loaded player to start the
      first `ytm_play` request.
- [x] Route normal YT Music play/pause/resume/next/previous through the same
      dedicated web session and remove desktop deep-link fallback from the
      normal playback path.
- [x] Select a playable YT Music watch result and verify the resulting DOM
      player state.
- [x] Explicit Connect brings the existing dedicated headed YT Music page to
      the front without launching a duplicate persistent browser.
- [x] Do not auto-restore a profile directory until a prior authenticated
      YT Music connection has been observed.
- [x] Re-scan all live persistent-context pages and adopt the usable
      `music.youtube.com` page after Google login or tab replacement.
- [x] Treat explicit Google/YT Music login evidence as `NEEDS_LOGIN` without
      treating a missing avatar selector as proof of logout.
- [x] Let normal connection polling detect login completion and persist the
      marker only after the dedicated YT Music surface is verified usable.
- [x] Resolve the live Polymer search-result shape, stale successive-search
      behavior and Playwright verification call so direct YTM playback can
      select and verify two different requested tracks.
- [x] Use the live `ytmusic-player-bar` custom-control shape for next/previous
      and keep transition verification to one delivered action plus bounded
      state reads only.
- [x] Route YT Music volume up/down/mute through the dedicated HTML media
      element with clamping and readback verification; keep `system_volume`
      as the macOS-wide volume tool.
- [x] Keep normal saved-profile restore headed but minimized, while explicit
      Connect still presents the dedicated browser for login/reconnection.
- [ ] Complete real macOS/YT Music connection and audible playback validation.

## Suggested verification semantics

### Pause

```text
success = after.playing is False
```

### Resume

```text
success = after.playing is True
```

### Next / previous

Preferred:

```text
before.track != after.track
```

Fallback:

```text
title/artist changed
```

A key event being posted is **not success**.

## Definition of done

- next/previous never blindly toggle playback afterward.
- regression tests fail on old implementation and pass on new.
- tool result accurately distinguishes:
  - delivered,
  - verified,
  - failed.

## Suggested commit

```text
fix ytm transport verification
```

## Checkpoint

Manually test YTM:

```text
play
pause
resume
next x5
previous x3
```

No random pause after track changes.

STOP before architectural extraction.

---

## Phase 1 report (2026-08-14)

### Root cause

The desktop YT Music fallback treated Quartz/AppleScript key delivery as
proof that playback changed, then sent Space after `next` and `previous`.
Space is a play/pause toggle, so a successfully started next/previous track
could be paused immediately. The Playwright adapter had a related verification
gap: it treated `playing=True` as proof of a track transition. The generic
now-playing fallback also accepted `playing=True` when no before/after track
identity existed.

The generic fallback also retained retry behavior after a successful
`next`/`previous` delivery when transition verification was unavailable.
Because those actions are non-idempotent, that could skip multiple tracks
before the system returned a failure.

Real manual validation then confirmed a second contamination path: when the
dedicated YT Music web adapter was unavailable, the desktop fallback accepted
generic macOS now-playing state. It treated an already-playing unrelated
track as proof that a new YT Music request had started, and could observe
JARVIS TTS as if it were the requested YT Music track. The same run confirmed
that `_execute_tool()` published `tool_done.ok=True` for any tool that returned
without raising, even when its JSON result contained `{"ok": false}`.

### Completed

- Removed the unconditional play/pause key after desktop `next` and
  `previous`.
- Added before/after verification using track ID first and title/artist
  metadata as the documented degraded identity fallback.
- Made delivered-but-unverified actions return `ok=False` with explicit
  `delivered`, `verified`, `verification`, `degraded`, `adapter` and `method`
  metadata.
- Updated the mirrored playing state only from observed verified state.
- Preserved pause/resume no-op behavior when an observed state already matches
  the requested result, while verifying all delivered commands.
- Hardened the existing desktop YT Music play fallbacks against the same
  command-delivery false-success behavior.
- Restricted YT Music transport verification to dedicated `ytm_web` state;
  generic macOS now-playing state is now rejected and produces an explicit
  delivered-but-unverified/degraded result.
- Removed the desktop `ytm_play` shortcut that treated any existing playing
  state as the requested track, and stopped sending Space when requested
  playback cannot be verified from YT Music-specific state.
- Made `tool_done` include the structured tool result status and diagnostic
  fields when a tool returns JSON with an `ok` field.
- Converted the Phase 0 strict YT Music xfails into passing regressions and
  added web/generic transport verification coverage.

### Files changed

- `jarvis/agent/loop.py`
- `jarvis/agent/tools.py`
- `jarvis/media/nowplaying.py`
- `jarvis/media/ytm_web.py`
- `tests/test_phase0_media_regressions.py`
- `tests/test_loop_tool_events.py`
- `tests/test_ytm_web.py`
- `tests/test_nowplaying.py`
- `PRODUCTION-READINESS-PLAN.md`

### Tests added/changed

- Converted `test_next_does_not_send_unconditional_toggle` and
  `test_previous_does_not_send_unconditional_toggle` from strict xfails to
  passing tests with before/after state.
- Added delivered-without-state failure coverage and verified pause/resume
  transport coverage.
- Added web-adapter track-transition success/failure coverage.
- Added generic now-playing coverage proving a transition requires an
  observed identity change.
- Added generic transport coverage proving delivered `next`/`previous`
  commands are sent once when verification is unavailable or shows no change,
  while a not-delivered transport may fall back and a later state read can
  verify the transition.
- Kept play/pause retry behavior separately covered as idempotent transport.
- Added regressions proving JARVIS/TTS generic now-playing state cannot verify
  YT Music transitions or a new `ytm_play` request.
- Added a focused regression proving `tool_done.ok` reflects a structured
  tool failure.

### Validation

- `./.venv/bin/pytest tests/test_phase0_media_regressions.py tests/test_ytm_web.py tests/test_nowplaying.py tests/test_loop_tool_events.py -q` -> PASS (`36 passed`)
- `./.venv/bin/pytest -q` -> PASS (`120 passed, 3 xfailed`; the xfails are the known Phase 3 local-model regressions)
- `./.venv/bin/ruff check .` -> FAIL (4 pre-existing findings in `jarvis/audio/focus.py`, `jarvis/log.py`, `jarvis/media/ytm_web.py` and `tests/test_web_ui_7b.py`)
- `./.venv/bin/ruff format --check .` -> FAIL (8 pre-existing formatted files; no added Phase 1 formatting issue)
- `git diff --check` -> PASS
- `cd web-ui && npm run typecheck` -> PASS
- `cd web-ui && npm run test` -> PASS (1 test)
- `cd web-ui && npm run build` -> PASS

### Manual validation still required

- Manual validation was performed on real macOS/YT Music and FAILED. The
  dedicated web adapter reported `ready=False` because its player bar was not
  ready, so the run used the desktop Quartz path. `ytm_play` and transport
  results were not reliable, and generic now-playing evidence was shown to be
  unsafe for YT Music verification.
- The runtime log identifies the failed paths as `ytm_play(fallback)` with a
  scraped video ID and the desktop Quartz transport path (`pid=48245`). For
  `Relja Popovic`, the old log recorded `sent space=False verified=True` even
  though the state was the already-playing generic `Top Gun`/`Relja`; for
  `Vlado Georgiev`, it recorded `sent space=True verified=False` and the model
  retried. The old code did not log the complete JSON result; this correction
  now logs the complete result with adapter/path metadata.
- After this correction, repeat on macOS with YT Music logged in to the
  persistent browser profile. Start JARVIS with `./scripts/start.sh`, then
  issue `ytm_play` for a different song/artist while another track is already
  playing, followed by `pause`, `resume`, `next` five times and `previous`
  three times. Confirm successful results identify `adapter=ytm_web`, have
  `verified=true`, and refer to the requested/actual YT Music track. If the
  web adapter is unavailable, confirm the desktop path returns explicit
  `ok=false` delivered/degraded results rather than claiming success.

### New issues discovered

- The Phase 1 review found that generic now-playing fallback retry behavior
  could repeat a delivered non-idempotent command when identity verification
  was unavailable. It was fixed in this Phase 1 correction; see Appendix A.
- Real manual validation found generic JARVIS/TTS now-playing contamination
  and a misleading `tool_done.ok` event; both are fixed in this correction.
  Existing Phase 3 xfails, the Phase 0 frontend dependency advisory and the
  deferred MediaService world-state test remain outside this phase.

### Remaining risks

- The desktop fallback can deliver YT Music commands but cannot claim success
  without dedicated YT Music state; when the web adapter is unavailable it
  intentionally returns delivered-but-unverified/degraded results.
- A delivered desktop `next`/`previous` command now returns an explicit
  unverified/degraded failure when identity remains unavailable; callers must
  decide whether and when a user-requested retry is appropriate.
- Full Ruff validation remains red on pre-existing repository findings.
- Phase 2 MediaService/state-ownership work has intentionally not started.

### Ready for next phase

NO — manual Phase 1 validation failed. The corrected branch requires a new
real macOS/YT Music validation pass before merge. Phase 2 has not been
started.

## Phase 1 correction update (2026-08-14)

### Root cause

The first Phase 1 correction correctly rejected generic now-playing evidence,
but it still had no usable per-device authentication flow. The web adapter
used a single player-bar readiness gate, so a connected YT Music home page
with no current track was treated as unavailable. `ytm_play` then fell back to
desktop deep links/keystrokes, which could reopen the installed web app and
could not reliably produce audible, verifiable playback.

### Completed

- Added explicit `DISCONNECTED`, `NEEDS_LOGIN`, `CONNECTING`, `CONNECTED` and
  `ERROR` states with separate page, search, player and playing state.
- Added headed persistent-profile connection flow using the per-device
  `~/.jarvis/ytm_profile` profile; Google authentication remains entirely in
  the visible Google/YT Music page.
- Added `GET /api/ytm/connection`, `POST /api/ytm/connect` and a minimal
  Connections-tab card driven by backend status.
- Separated page/search readiness from player readiness so first play can start
  from an authenticated page with no loaded track.
- Made the dedicated YTM browser the only normal play/transport path and
  removed desktop deep-link/Quartz fallback from those actions.
- Restricted status and verification evidence to the dedicated YTM DOM; the
  generic macOS now-playing stream cannot provide YTM success evidence.
- Selects a watch/video identity from YTM search results and verifies the
  resulting player state, including a second different play request.
- Added clean browser shutdown while retaining the local persistent profile
  for restart.
- Updated the Phase 2 handoff so `MediaService` must consume this connected
  browser adapter rather than recreate authentication.

### Files changed

- `jarvis/media/ytm_web.py`
- `jarvis/agent/tools.py`
- `jarvis/agent/prompts.py`
- `jarvis/app.py`
- `web-ui/src/components/ConnectionsTab.tsx`
- `web-ui/src/lib/ytm-connection.ts`
- `tests/test_ytm_web.py`
- `tests/test_phase0_media_regressions.py`
- `tests/test_ytm_connection_api.py`
- `web-ui/src/lib/ytm-connection.test.ts`
- `README.md`
- `PRODUCTION-READINESS-PLAN.md`

### Tests added/changed

- Added disconnected/login-required/connected-without-player/expired-session
  connection state coverage.
- Added headed launch, first play from an empty player, second different play,
  player-required transport, DOM pause/resume, launch-error and no-desktop-
  fallback coverage, plus playable-result metadata coverage.
- Added API and frontend regressions proving backend confirmation is required
  before the UI shows Connected.
- Reworked the previous desktop-fallback regressions to prove normal YTM
  actions do not call the deep-link or desktop transport path.

### Validation

- `./.venv/bin/pytest tests/test_ytm_web.py tests/test_phase0_media_regressions.py tests/test_ytm_connection_api.py tests/test_nowplaying.py tests/test_loop_tool_events.py -q` -> PASS (`48 passed`)
- `./.venv/bin/pytest -q` -> PASS (`132 passed, 3 xfailed`; the xfails are the known Phase 3 local-model regressions)
- `./.venv/bin/ruff check jarvis/media/ytm_web.py jarvis/agent/tools.py jarvis/app.py jarvis/agent/prompts.py tests/test_ytm_web.py tests/test_phase0_media_regressions.py tests/test_ytm_connection_api.py` -> PASS
- `./.venv/bin/ruff check .` -> FAIL (3 pre-existing findings in `jarvis/audio/focus.py`, `jarvis/log.py` and `tests/test_web_ui_7b.py`)
- `./.venv/bin/ruff format --check .` -> FAIL (6 pre-existing formatting findings; changed YTM/connection test files are formatted)
- `git diff --check` -> PASS
- `cd web-ui && npm run typecheck` -> PASS
- `cd web-ui && npm run test` -> PASS (3 tests)
- `cd web-ui && npm run build` -> PASS
- `curl -sS http://127.0.0.1:7777/api/ytm/connection` -> PASS runtime smoke; current real profile reports `NEEDS_LOGIN`, `page_ready=true`, `search_ready=true`, `player_loaded=false`

### Manual validation still required

No real Google login or audible playback has been claimed. In the open headed
YT Music window, click **Poveži YouTube Music** if needed, log in directly on
Google, then validate: play artist/song A; play different artist/song B while
A is active; pause; resume; next five times; previous three times; play a
specific different song; restart JARVIS and confirm the profile reconnects.
Confirm every successful tool result reports `adapter=ytm_web` and
`verified=true`, and confirm audio is audible from the same browser session.

### New issues discovered

- No additional out-of-scope issue was found. Real browser DOM/audio behavior
  remains the required manual checkpoint.

### Remaining risks

- Authentication/account selectors and YT Music DOM control selectors need
  confirmation against the user's logged-in account.
- Browser autoplay/audio policy and macOS Accessibility permissions may still
  affect real playback or unrelated PTT functionality.
- Full Ruff and format checks remain red on unrelated pre-existing files.
- Phase 2 MediaService has not started.

### Ready for next phase

NO — automated validation passes, but real connection/authentication and
audible YT Music manual validation are still required. Do not merge and do not
start Phase 2.

## Phase 1 connection presentation correction (2026-08-14)

### Root cause

When the server already had a live headed Playwright page in
`~/.jarvis/ytm_profile`, `POST /api/ytm/connect` reused it and navigated to
YT Music but never brought that page to the front. The endpoint returned the
same `NEEDS_LOGIN` state, while the Connections card had no inline action
feedback, so the user could not see where to authenticate. Startup also
treated the existence of the profile directory as proof that the profile had
previously been authenticated, so an incomplete profile was launched again on
every restart.

### Completed

- Explicit `connect()` now calls Playwright `Page.bring_to_front()` on the
  existing dedicated page before navigation and does not create a duplicate
  persistent context.
- Added a local `.connected` marker written only after backend probe evidence
  establishes `CONNECTED`; `warm_up()` and implicit readiness restoration now
  require that marker.
- Added immediate inline Connections-card feedback for opening/login errors
  and login progress, while preserving backend-confirmed `CONNECTED` truth.
- Changed the `NEEDS_LOGIN` button label to **Otvori YouTube Music prijavu**.

### Files changed

- `jarvis/media/ytm_web.py`
- `web-ui/src/components/ConnectionsTab.tsx`
- `web-ui/src/lib/ytm-connection.ts`
- `tests/test_ytm_web.py`
- `web-ui/src/lib/ytm-connection.test.ts`
- `README.md`
- `PRODUCTION-READINESS-PLAN.md`

### Tests added/changed

- Added a regression proving an existing `NEEDS_LOGIN` page is brought to the
  front exactly once and no second persistent context is launched.
- Added coverage proving an unconnected profile directory is not warmed up
  automatically.
- Added frontend pure-state coverage for the explicit login button label.

### Validation

- Targeted connection/YTM backend tests -> PASS (`40 passed`)
- Targeted Ruff for changed Python files -> PASS
- Frontend typecheck -> PASS
- Frontend tests -> PASS (`3 tests`)
- Frontend build -> PASS
- Full backend suite -> PASS (`135 passed, 3 xfailed`; known Phase 3 local-model regressions)
- Changed Python format check -> PASS
- Full Ruff -> FAIL on 3 pre-existing findings in `jarvis/audio/focus.py`,
  `jarvis/log.py` and `tests/test_web_ui_7b.py`
- Full format check -> FAIL on 6 pre-existing files/findings; changed YTM
  files remain formatted
- `git diff --check` -> PASS

### Manual validation still required

After restart, open the Connections tab and click **Otvori YouTube Music
prijavu**. The existing/new dedicated headed browser must become visible. Log
in directly through Google/YT Music, wait for backend `CONNECTED`, then run
the full Phase 1 playback sequence and restart JARVIS to verify persistence.

### New issues discovered

- No additional issue beyond the connection-presentation bug recorded in
  Appendix A.

### Remaining risks

- macOS window-manager behavior may still vary; `bring_to_front()` is scoped
  to the dedicated Playwright page and does not activate unrelated Chrome
  profiles.
- Real Google login, audible playback and YT Music DOM controls remain
  unvalidated until the user repeats the manual sequence.

### Ready for next phase

NO — the correction is ready for a new manual validation attempt, but Phase 1
is not complete and Phase 2 must not start.

## Phase 1 login completion correction (2026-08-14)

### Root cause

The failed real login was performed in the intended dedicated profile and
returned to a live `https://music.youtube.com` page, but the authentication
probe required one narrow avatar/account selector. The real logged-in page
had a usable YT Music app, navigation bar and search surface while that
selector was absent, so every status poll incorrectly returned `NEEDS_LOGIN`.

The adapter also retained only one tracked Playwright page. That made a
Google login flow vulnerable to leaving `_ytm_page` on an accounts page or a
stale/closed tab when the usable YT Music page was opened or replaced
elsewhere in the same persistent context.

The live diagnostic confirmed the profile and DOM evidence without exposing
credentials: `/Users/marko/.jarvis/ytm_profile`, one active YT Music tab,
`page_ready=true`, `search_ready=true`, `has_ytm_app=true`, `has_nav=true`,
`has_search=true`, `has_account=false` and `has_explicit_login=false`.

### Completed

- Added live-page inventory and adoption across `_context.pages`, preferring
  an authenticated, usable `music.youtube.com` page and recovering from a
  stale or closed tracked page.
- Reused the page-adoption path for status polling, connect/reconnect,
  restore and navigation recovery.
- Replaced the avatar-presence authentication decision with evidence-based
  signals: usable YT Music app/search surface plus no explicit login
  evidence; Google login pages and explicit YT Music sign-in controls remain
  `NEEDS_LOGIN`.
- Kept unknown authentication as an explicit error rather than silently
  claiming `CONNECTED` or inventing a login failure.
- Preserved automatic polling and marker creation only after verified
  dedicated-page readiness.
- Prevented a repeated Connect action from navigating away from an active
  Google login page.
- Confirmed the saved profile restored as `CONNECTED` after a JARVIS restart
  without another login.

### Files changed

- `jarvis/media/ytm_web.py`
- `tests/test_ytm_web.py`
- `PRODUCTION-READINESS-PLAN.md`

### Tests added/changed

- Added polling coverage for `NEEDS_LOGIN -> CONNECTED` after DOM/auth state
  changes without another Connect call.
- Added coverage proving a usable YT Music surface does not require an
  avatar selector.
- Added second-tab, stale-YT Music-tab and closed-page adoption tests.
- Added coverage for unknown authentication, marker safety and preserving an
  active Google login page on repeated Connect.

### Validation

- `./.venv/bin/pytest -q tests/test_ytm_web.py tests/test_ytm_connection_api.py` -> PASS (`37 passed`)
- `./.venv/bin/pytest -q tests/test_phase0_media_regressions.py tests/test_ytm_web.py tests/test_ytm_connection_api.py tests/test_nowplaying.py tests/test_loop_tool_events.py` -> PASS (`58 passed`)
- `./.venv/bin/pytest -q` -> PASS (`142 passed, 3 xfailed`; known Phase 3 local-model regressions)
- `./.venv/bin/ruff check jarvis/media/ytm_web.py tests/test_ytm_web.py` -> PASS
- `./.venv/bin/ruff format --check jarvis/media/ytm_web.py tests/test_ytm_web.py` -> PASS
- `./.venv/bin/ruff check .` -> FAIL (3 pre-existing findings in
  `jarvis/audio/focus.py`, `jarvis/log.py` and `tests/test_web_ui_7b.py`)
- `./.venv/bin/ruff format --check .` -> FAIL (6 pre-existing formatting
  findings; changed files remain formatted)
- `git diff --check` -> PASS
- `cd web-ui && npm run typecheck` -> PASS
- `cd web-ui && npm run test` -> PASS (`3 tests`)
- `cd web-ui && npm run build` -> PASS
- Real runtime before the correction: dedicated profile/page was confirmed,
  but old code returned `NEEDS_LOGIN` despite YTM readiness.
- Real runtime after the correction: `POST /api/ytm/connect` returned
  `CONNECTED`; after shutdown/restart, automatic profile restore returned
  `CONNECTED` again. Safe probe logs showed the real usable YTM surface and
  no account selector.

### Manual validation still required

- Repeat the full connection-only sequence in the visible dedicated browser:
  start JARVIS, click Connect, log in if needed, wait for automatic
  `CONNECTED`, stop JARVIS, restart it, and confirm `CONNECTED` persists.
- Only after that connection checkpoint, repeat the Phase 1 playback matrix:
  different artist/song while another track plays, pause, resume, next five
  times and previous three times. No manual playback success is claimed by
  this correction.

### New issues discovered

- The connection correction is resolved, but a separate real-runtime
  playback/search issue was observed and recorded in Appendix A. The
  missing-avatar probe and multi-page login handoff are fixed in this
  correction; the manual playback checkpoint remains outstanding.

### Remaining risks

- YT Music DOM controls and browser autoplay/audio behavior still require the
  user's real manual playback validation.
- Full Ruff/format repository checks may still report unrelated pre-existing
  findings; changed files are clean.
- Phase 2 MediaService/state-ownership work has not started.

### Ready for next phase

NO — the connection correction passed real profile restore smoke validation,
but the required user-led connection and audible playback checkpoint has not
yet been repeated after this correction. Do not merge and do not start Phase 2.

---

## Phase 1 playback DOM correction update (2026-08-14)

### Root cause

The authenticated YT Music page did not use the selector shape assumed by the
old `play_query()` implementation. The live search surface rendered
`ytmusic-responsive-list-item-renderer` and `ytmusic-two-row-item-renderer`
components. A playable row carried `videoId` and a nested `watchEndpoint` in
Polymer component data, while its rendered watch link could be `/watch` or a
watch URL without the old guaranteed `href*="/watch?v="` shape. Artist and
album/navigation rows exposed browse data without a playable `videoId`.

Two additional runtime issues were confirmed during the focused real-browser
check. Setting the search input and pressing Enter changed the URL but could
leave stale result rows during a second request, so the same dedicated page
now navigates directly to the YT Music `/search?q=...` URL. Also, this
Playwright version exposes the `wait_for_function` argument as keyword-only;
passing the verification payload positionally caused the real readiness and
player checks to fail before they could verify playback.

### Completed

- Added component-aware playable-result inspection using nested `videoId` /
  `watchEndpoint` evidence and a real `/watch` anchor or YT Music play-control
  fallback.
- Skipped artist, album and generic navigation rows and ranked playable rows
  by safe query-text relevance before selecting a candidate.
- Replaced the stale SPA input/Enter search path with direct navigation inside
  the same authenticated YT Music browser page and added a bounded render
  settle before clicking.
- Used the current YT Music watch anchor as the preferred click target because
  the live overlay play control could load a selected track without starting
  playback; the result remains verified only from YT Music player state.
- Added structured `ytm_play` diagnostics for connection state, search method,
  stage, candidate identity, delivery and verification. Connected search or
  playback failures remain `connection_state=CONNECTED` and do not imply
  login failure.
- Corrected the real Playwright `wait_for_function(..., arg=...)` calls and
  preserved strict player identity/playing verification for both successive
  play requests.

### Files changed

- `jarvis/media/ytm_web.py`
- `jarvis/agent/tools.py`
- `jarvis/agent/prompts.py`
- `tests/test_ytm_web.py`
- `tests/test_phase0_media_regressions.py`
- `PRODUCTION-READINESS-PLAN.md`

### Tests added/changed

- Added component-shaped fixtures covering playable songs, artist/album
  navigation, no-candidate structured failure, successive different queries,
  connected playback failure and verified player state.
- Added a tool regression proving a connected search failure preserves its
  diagnostic state and does not become a login instruction.

### Validation

- `./.venv/bin/pytest -q tests/test_ytm_web.py tests/test_phase0_media_regressions.py` -> PASS (`49 passed`)
- `./.venv/bin/pytest -q` -> PASS (`147 passed, 3 xfailed`; the xfails are the known Phase 3 local-model regressions)
- `./.venv/bin/ruff check jarvis/media/ytm_web.py jarvis/agent/tools.py jarvis/agent/prompts.py tests/test_ytm_web.py tests/test_phase0_media_regressions.py` -> PASS
- `./.venv/bin/ruff format --check` on changed YTM/Python test files -> PASS
- `git diff --check` -> PASS
- `cd web-ui && npm run typecheck` -> PASS
- `cd web-ui && npm run test` -> PASS (`3 tests`)
- `cd web-ui && npm run build` -> PASS
- Real authenticated adapter check using `/Users/marko/.jarvis/ytm_profile` -> PASS for `Relja Popović` and `Vlado Georgiev`: `CONNECTED`, search submitted through `ytm_search_url`, playable candidate found/clicked, player `playing=true`, actual YT Music title/artist matched, `verified=true` for both successive requests.
- Audible output was not independently measured; only the dedicated YT Music DOM player state was observed.
- Full repository Ruff remains red on three pre-existing unused imports, and full format check remains red on six pre-existing documentation/unrelated files; no changed-file finding was introduced.

### Manual validation still required

Start JARVIS and repeat through chat with the logged-in dedicated browser:
play one specific Relja song, play a different Vlado Georgiev song while it
is active, pause, resume, next five times and previous three times. Confirm
each successful result has `adapter=ytm_web`, `connection_state=CONNECTED`,
`verified=true` and the actual YT Music title/artist. Confirm the audio is
audible from the dedicated browser and repeat the check after a restart.

### New issues discovered

- The search-result selector issue is resolved in code and recorded here; no
  separate out-of-scope issue was found. The known YT Music DOM/autoplay
  variability remains a manual checkpoint.

### Remaining risks

- Audible output and the complete user-led pause/resume/next/previous matrix
  still require manual confirmation on this macOS session.
- YT Music may change its Polymer component data or click behavior again;
  failures now remain explicit and include adapter/stage diagnostics.
- Phase 2 `MediaService` and state-ownership work has not started.

### Ready for next phase

NO — the focused real adapter check passes, but the required user-led manual
Phase 1 validation and audible playback checkpoint remain outstanding. Do not
merge and do not start Phase 2.

---

## Phase 1 final focused runtime pass (2026-08-14)

### Root cause

The live authenticated YT Music player bar does not expose the transport
controls as `#next-button` and `#previous-button`. It renders
`yt-icon-button.next-button` and `yt-icon-button.previous-button` inside
`ytmusic-player-bar`; the actionable inner buttons carry the `Next` and
`Previous` ARIA labels. The old selectors therefore returned `no next button`
or `no previous button` even though the visible controls were usable.

The YT Music volume tools were still using AppleScript `set volume`, which
changed the whole macOS output device rather than the dedicated YT Music
player. The headless Chrome smoke test also reached the music origin but did
not render the YT Music app, search box or player bar, so headless mode was not
adopted as the normal runtime.

### Completed

- Scoped next/previous discovery to the authenticated `ytmusic-player-bar`
  custom controls and clicked exactly one actionable inner button.
- Preserved strict before/after track-identity verification and added bounded
  delayed state reads only; delivered next/previous commands are never resent
  by the verification path.
- Added YT Music-only volume operations using `video.volume` and
  `video.muted`, with ±0.10 clamping, readback and explicit degraded failure
  results when the media element is unavailable.
- Updated tool schemas, prompt guidance and README semantics so `ytm_volume_*`
  is distinct from macOS-wide `system_volume`.
- Kept explicit Connect headed and presented; saved normal restore uses the
  headed browser with `--start-minimized`.
- Real authenticated adapter validation completed `next` ×5 and `previous`
  ×3 with `CONNECTED`, `delivered=true`, `verified=true` and changed title /
  artist state for each transition. The YTM volume up/down/mute/toggle path
  also returned verified media-element readback.

### Files changed

- `jarvis/media/ytm_web.py`
- `jarvis/agent/tools.py`
- `jarvis/agent/prompts.py`
- `tests/test_ytm_web.py`
- `tests/test_phase0_media_regressions.py`
- `README.md`
- `PRODUCTION-READINESS-PLAN.md`

### Tests added/changed

- Added player-bar-shaped fake DOM coverage for next/previous, single command
  delivery, unchanged-track failure and delivered-but-unverifiable degraded
  results with bounded read counts.
- Added YT Music media-element volume clamp, mute, readback and unavailable
  element coverage.
- Added a tool-level regression proving YT Music volume never calls the
  macOS system-volume AppleScript.
- Added lifecycle coverage for minimized saved-profile restore versus
  explicitly presented Connect.

### Validation

- `./.venv/bin/pytest -q tests/test_ytm_web.py tests/test_phase0_media_regressions.py` -> PASS (`60 passed`)
- Real authenticated `ytm_web` adapter using `/Users/marko/.jarvis/ytm_profile` -> PASS: `CONNECTED`; search/play verified; next ×5 and previous ×3 each delivered one player-bar click and verified a changed track identity; volume up/down/mute/toggle verified HTML media-element state.
- Headless authenticated Chrome smoke -> NOT ADOPTED: origin loaded, but the YT Music app, search box and player bar did not render.
- Headed saved-profile restore with `--start-minimized` -> PASS: the adapter restored `CONNECTED`; the read-only macOS window-list probe found no on-screen matching YT Music window. Audible output and focus behavior were not independently measured.

### Manual validation still required

- Start JARVIS with `./scripts/start.sh` and use the connected profile through
  chat: play a specific Relja song, play a different Vlado Georgiev song,
  pause, resume, next ×5 and previous ×3.
- Confirm successful tool results show `adapter=ytm_web`,
  `connection_state=CONNECTED`, `verified=true` and the actual YT Music
  title/artist; confirm the dedicated browser remains backgrounded during
  normal actions and is presented only by Connect/reconnect.
- Test `ytm_volume_up/down/mute` while another macOS audio source is present,
  confirming only YT Music changes, then confirm the audio is audible.
- Repeat the playback and persistence checks after a JARVIS restart.

### New issues discovered

- Headless Chrome did not render the authenticated YT Music surface in the
  real-profile smoke test; this is recorded in Appendix A. Phase 1 uses the
  safer headed-minimized fallback and does not add a new browser architecture.

### Remaining risks

- Audible output, exact focus/window behavior and the user-led chat sequence
  still require manual confirmation on this macOS session.
- YT Music may change its Polymer control classes or labels; failures remain
  explicit and limited to one delivered non-idempotent action.
- Phase 2 `MediaService` and state-ownership work has not started.

### Ready for next phase

NO — the focused runtime correction is implemented and directly validated, but
the required user-led audible/manual Phase 1 checkpoint remains outstanding.
Do not merge and do not start Phase 2.

---

# 5. Phase 2 — Introduce authoritative MediaService

**Priority:** P0/P1  
**Goal:** remove competing media truths.

### Phase 1 handoff constraint

Phase 1 now provides a per-device authenticated, persistent `ytm_web` browser
adapter and connection status. When Phase 2 introduces `MediaService`, its
authoritative YT Music adapter must consume this existing connected browser
session and authentication flow; it must not recreate login, copy cookies or
reintroduce the desktop deep-link path.

## Target files

Suggested new structure:

```text
jarvis/media/models.py
jarvis/media/service.py
jarvis/media/ytm_web.py
jarvis/media/ytm_desktop.py
jarvis/media/nowplaying.py
jarvis/tools/media.py           later/optional in this phase
```

## Tasks

- [ ] Define `PlaybackState`.
- [ ] Define adapter health/state types.
- [ ] Extract desktop-YTM implementation out of `agent/tools.py`.
- [ ] Add `YtmWebAdapter`.
- [ ] Add `YtmDesktopAdapter`.
- [ ] Create `MediaService`.
- [ ] Define primary/fallback selection.
- [ ] Make service own action verification.
- [ ] Make service own playback state normalization.
- [ ] Reduce/remove `_YTM_STATE` as authoritative state.
- [ ] Make `ytm_status` call `MediaService`.
- [ ] Make `context.py` call `MediaService`.
- [ ] Tool functions become thin wrappers.

## Required state distinction

Do not use one boolean called `ready`.

Represent at least:

```text
adapter process/browser healthy
search available
player available
playback state
```

A YTM page can be healthy with no active track.

## YTM web recovery

- [ ] Validate browser/context connection.
- [ ] Validate page is open.
- [ ] Validate expected origin.
- [ ] Validate a small DOM probe.
- [ ] Invalidate stale ready state.
- [ ] Recreate page/context when needed.
- [ ] Prevent concurrent duplicate launches with one lifecycle lock/task.
- [ ] Shutdown must cancel warmup/recovery work and close browser resources.

## Definition of done

The following all read the same service:

```text
world-state
ytm_status
ytm_pause/resume
ytm_next/previous
ytm_play
```

No module is allowed to claim a different authoritative playback state.

## Suggested commits

```text
add media service state model
extract ytm desktop adapter
route ytm tools through media service
use media service in world state
```

## Checkpoint

Run automated tests + manual YTM test.

STOP before local-model refactor.

---

# 6. Phase 3 — Stabilize local model tool calling

**Priority:** P0  
**Goal:** make Ollama behavior conform to the same tool protocol as cloud
models.

## Files

```text
jarvis/llm.py
jarvis/local_models.py
jarvis/agent/loop.py
tests for local models / tool streaming
```

## Tasks

### Shared ToolCallAccumulator

- [ ] Extract cloud tool delta assembly into reusable component.
- [ ] Support:
  - call index,
  - streamed id,
  - streamed function name,
  - streamed JSON arguments,
  - final full-call repetition.
- [ ] Use the same accumulator for cloud provider and Ollama.
- [ ] Validate final arguments as JSON or preserve explicit parse error.
- [ ] Never execute raw per-delta fragments.

### Capability detection

- [ ] Rewrite probe prompt so success requires a tool call.
- [ ] Parse the response.
- [ ] `tools` only if valid structured tool call exists.
- [ ] Distinguish:
  - `tools`,
  - `notools`,
  - `unknown`.
- [ ] Preserve explicit `.env` override precedence.
- [ ] Persist probe result with enough version identity that a changed model
      tag/build can be reprobed later.

### Tool/no-tool history

- [ ] Continue sanitizing tool mechanics for notools models.
- [ ] Add tests proving tool messages do not leak into notools history.
- [ ] Ensure the no-tools system prompt contains no executable-looking tool
      patterns.

## Definition of done

A fake Ollama stream with fragmented call data results in exactly one valid
tool call.

A model that returns HTTP 200 but plain text is **not** labeled tool-capable.

## Suggested commit

```text
unify local tool call streaming
```

## Checkpoint

Test at least:

- one tools-capable local model,
- one no-tools local model,
- cloud model afterward in same session.

STOP before UI lifecycle work.

---

# 7. Phase 4 — Make local model selection state-safe

**Priority:** P1  
**Goal:** eliminate “selected local model but cloud answered” races except for
explicit runtime fallback.

## Backend tasks

- [ ] Formalize runner states:
  - idle,
  - loading,
  - ready,
  - error,
  - unloading.
- [ ] Return state consistently from endpoints.
- [ ] Ensure concurrent load requests are deterministic.
- [ ] Define what happens when selecting model B while A is loading.
- [ ] Define unload behavior during active chats.
- [ ] Emit explicit fallback reason/event.

## Frontend tasks

Relevant:

```text
web-ui/src/lib/actions.ts
web-ui/src/store.ts
web-ui/src/lib/bus.ts
```

- [ ] Add `pendingModel`.
- [ ] Do not set `currentModel` until load success.
- [ ] Surface load error.
- [ ] Disable send or queue chat while explicit model transition is pending.
- [ ] Make fallback to cloud visible but non-destructive to selected preference
      unless policy intentionally changes it.
- [ ] On boot, if persisted local model exists, show loading state until ready.

## Definition of done

There is no time window where UI says:

```text
active model = local X
```

while backend considers X not ready.

## Suggested commit

```text
make local model selection wait for readiness
```

---

# 8. Phase 5 — Refactor the tool system

**Priority:** P1  
**Goal:** make future tools cheap to add and safe to reason about.

## Target structure

Example:

```text
jarvis/tools/
├── __init__.py
├── base.py
├── registry.py
├── executor.py
├── apple/
│   ├── calendar.py
│   └── reminders.py
├── system/
│   ├── apps.py
│   ├── clipboard.py
│   └── volume.py
├── search/
│   └── web.py
├── media.py
└── coding/
    └── kilo.py
```

Do not force exact names if a cleaner current-project fit exists.

## Tasks

- [ ] Create canonical `ToolResult`.
- [ ] Create canonical `ToolError`.
- [ ] Create `ToolExecutor`.
- [ ] Centralize:
  - lookup,
  - permission check,
  - timeout,
  - exception normalization,
  - BUS events.
- [ ] Move domain implementations out of `agent/tools.py`.
- [ ] Keep agent loop dependent on registry/executor, not implementation
      modules.
- [ ] Add per-tool/default timeout configuration.
- [ ] Validate arguments before execution where practical.
- [ ] Preserve OpenAI-compatible schemas.

## Tool error codes

Start with a small vocabulary:

```text
INVALID_ARGUMENTS
PERMISSION_DENIED
DEPENDENCY_MISSING
NOT_AVAILABLE
NOT_READY
TIMEOUT
EXECUTION_FAILED
VERIFICATION_FAILED
CANCELLED
```

## Definition of done

Adding a simple new tool does not require editing `agent/loop.py`.

`agent/tools.py` is removed or reduced to compatibility exports during a
short migration period.

## Suggested PR name

```text
refactor tool execution architecture
```

---

# 9. Phase 6 — Configuration, setup and dependency correctness

**Priority:** P1  
**Goal:** clean installs and existing installs behave predictably.

## Tasks

### `.env`

- [ ] Change `scripts/setup.sh`:
  - create `.env` only when absent,
  - never rewrite existing `.env`.
- [ ] If defaults need migration, create an explicit migration command later.

### dependencies

- [ ] Fix malformed `requirements.txt` line.
- [ ] Ensure default TTS backend dependency is installed.
- [ ] Clearly separate core and optional heavy dependencies.
- [ ] Confirm optional install script imports all modules it uses correctly.
- [ ] Test setup from a clean venv.

### defaults

- [ ] Compare:
  - `config.py`,
  - `.env.example`,
  - README,
  - developer guide.
- [ ] Choose canonical defaults.
- [ ] Add config validation for unsupported enum values/ranges.

### versions

- [ ] Add a single version constant/package source.
- [ ] Use it for:
  - FastAPI app,
  - `/api/status`,
  - WebSocket hello,
  - user agent where appropriate,
  - package metadata if feasible.

## Definition of done

A clean install with no custom config can start using its documented default
TTS backend.

Running setup again does not change an existing `.env`.

---

# 10. Phase 7 — Lifecycle and concurrency hardening

**Priority:** P1  
**Goal:** no leaked long-lived resources and deterministic persistence.

## Lifespan

- [ ] Introduce a central service startup/shutdown path.
- [ ] Close YTM Playwright on shutdown.
- [ ] Close shared HTTP clients.
- [ ] Cancel local model pull tasks.
- [ ] Cancel/finish session workers as policy requires.
- [ ] Stop PTT.
- [ ] Stop/cancel speech playback.
- [ ] Shut down custom executors if appropriate.
- [ ] Final persistence flush.

## Session persistence

- [ ] Add a save lock or single writer.
- [ ] Test two sessions completing nearly simultaneously.
- [ ] Test cancellation during tool execution.
- [ ] Test restart loading after cancelled/incomplete turn.
- [ ] Consider repository abstraction if session storage grows.

## Event bus

- [ ] Keep bounded queues.
- [ ] Add subscriber identity/metrics if debugging multi-tab overflow is hard.
- [ ] Verify cancellation events cannot be lost in a way that leaves UI stuck.

## Definition of done

Starting/stopping the app repeatedly does not leave browser/process resources
behind.

Parallel sessions cannot corrupt `sessions.json`.

---

# 11. Phase 8 — Frontend async/race cleanup

**Priority:** P1/P2  
**Goal:** frontend always represents backend lifecycle honestly.

## Tasks

- [ ] Model transition state, as defined in Phase 4.
- [ ] Browser mic:
  - await or otherwise coordinate audio focus before recording where feasible.
- [ ] Ensure `listen_exit` reflects actual reason set rather than blindly
      clearing if multiple reasons can exist.
- [ ] Audit swallowed fetch errors that materially affect state.
- [ ] Distinguish:
  - disconnected,
  - loading,
  - empty,
  - error.
- [ ] Verify session-scoped event filtering for all session events.
- [ ] Verify switching session mid-stream does not pollute the new transcript.
- [ ] Add frontend tests if test framework is introduced; otherwise isolate
      pure state reducers/functions for unit testing.

## Definition of done

No major async operation is represented as complete before backend confirms
it.

---

# 12. Phase 9 — Observability and diagnostics

**Priority:** P1/P2  
**Goal:** future bugs can be diagnosed from logs instead of guesses.

## Tasks

- [ ] Add `turn_id`.
- [ ] Preserve `tool_call_id` through logs/events.
- [ ] Structured logs for:
  - model request,
  - fallback,
  - tool start/end,
  - media adapter selection,
  - media verification,
  - STT,
  - TTS.
- [ ] Record durations.
- [ ] Add media status to `doctor`.
- [ ] Add local model capability/readiness to `doctor`.
- [ ] Add TTS backend dependency check to `doctor`.
- [ ] Add YTM Playwright availability/profile health diagnostic.
- [ ] Do not print secrets.

## Useful metrics

```text
llm_first_token_ms
llm_total_ms
tool_duration_ms
tool_failure_count
ytm_fallback_count
ytm_verification_failure_count
local_cloud_fallback_count
stt_duration_ms
tts_first_audio_ms
```

## Definition of done

For a failed `ytm_next`, logs can answer:

```text
which adapter?
what was before state?
what command?
what was after state?
why verification failed?
what fallback ran?
```

---

# 13. Phase 10 — CI and quality gates

**Priority:** P1  
**Goal:** regressions do not silently enter `main`.

## GitHub Actions

Backend job:

```bash
pip install ...
pytest
ruff check .
ruff format --check .
```

Frontend job:

```bash
cd web-ui
npm ci
npm run typecheck
npm run build
```

Optional separate macOS/manual workflow for platform-specific smoke tests.

## Test markers

Use markers for dependencies that CI cannot provide:

```text
macos
requires_ollama
requires_ytm_login
integration
```

## Merge gate

Require basic backend + frontend jobs before merge.

## Definition of done

A PR that breaks typecheck/tests cannot merge unnoticed.

---

# 14. Phase 11 — Documentation and release hardening

**Priority:** P2  
**Goal:** code and docs describe the same system.

## Tasks

- [ ] Delete/retire old `plan.md`.
- [ ] Keep `DEVELOPER-GUIDE.md` as architecture source.
- [ ] Keep README user-facing.
- [ ] Update setup docs.
- [ ] Document model capability behavior.
- [ ] Document YTM login/profile behavior.
- [ ] Document optional dependencies.
- [ ] Document manual macOS permissions:
  - Accessibility,
  - microphone.
- [ ] Establish release checklist.
- [ ] Use one version source.
- [ ] Add changelog/release notes when versions start shipping regularly.

## Definition of done

A new developer can understand:

```text
how to install
how to run
how chat works
how local models work
how tools execute
how YTM works
how to add a tool
how to debug
how to validate a PR
```

without reading historical chat logs.

---

# 15. Recommended implementation order

Do not reorder casually.

```text
Phase 0  safety net
Phase 1  immediate YTM correctness
Phase 2  MediaService
Phase 3  local tool streaming/capability
Phase 4  local model readiness/UI
Phase 5  tool architecture
Phase 6  config/setup/dependencies/version
Phase 7  lifecycle/concurrency
Phase 8  remaining frontend races
Phase 9  observability/doctor
Phase 10 CI
Phase 11 docs/release
```

Phases 1–4 address the bugs most visible to the user.

Phases 5–10 create the foundation for growth.

---

# 16. Recommended PR breakdown

Prefer approximately:

```text
PR 1  YTM transport correctness + regression tests
PR 2  MediaService + YTM adapters
PR 3  shared tool-call accumulator + local capability probe
PR 4  local model readiness state + UI transition
PR 5  ToolExecutor + typed results + split registry
PR 6  setup/config/dependency/version cleanup
PR 7  lifecycle + session persistence hardening
PR 8  frontend async cleanup
PR 9  observability + doctor
PR 10 CI + documentation synchronization
```

Do not require exactly ten PRs, but avoid collapsing all into two giant PRs.

---

# 17. Manual validation matrix

Before declaring stabilization complete:

## Cloud model

- [ ] normal chat
- [ ] reasoning stream
- [ ] one tool
- [ ] multiple sequential tools
- [ ] permission ask/allow
- [ ] permission deny
- [ ] cancel during LLM stream
- [ ] cancel during tool work

## Local tools-capable model

- [ ] load
- [ ] chat
- [ ] one tool
- [ ] fragmented streamed tool call
- [ ] multiple tool calls
- [ ] unload
- [ ] restart and reload

## Local no-tools model

- [ ] labeled no-tools
- [ ] receives no schemas
- [ ] does not imitate old tool history
- [ ] clearly refuses side-effect request

## YT Music

- [ ] clean app start
- [ ] YTM web adapter warmup
- [ ] logged-out behavior
- [ ] play query
- [ ] pause
- [ ] resume
- [ ] next repeatedly
- [ ] previous repeatedly
- [ ] status
- [ ] kill/close Playwright page -> recovery
- [ ] desktop fallback
- [ ] server restart
- [ ] no unwanted foreground focus

## Audio

- [ ] browser mic
- [ ] PTT
- [ ] barge-in cancels TTS
- [ ] system volume restores
- [ ] repeated quick PTT
- [ ] TTS default after clean setup

## Sessions

- [ ] rapid FIFO messages
- [ ] two active sessions
- [ ] reset
- [ ] delete
- [ ] restart persistence
- [ ] cancelled tool call persistence

---

# 18. Production-readiness non-goals

This roadmap does **not** require:

- containerizing macOS desktop automation,
- multi-user SaaS architecture,
- Kubernetes,
- distributed queues,
- a database migration before it is needed,
- replacing FastAPI,
- replacing React,
- replacing Ollama,
- rewriting the project in another language.

The goal is a strong single-user desktop assistant architecture that can grow.

---

# 19. Architecture decisions to preserve

Unless evidence changes:

- Keep async Python backend.
- Keep FastAPI.
- Keep React/Vite UI.
- Keep server-driven TTS scheduling.
- Keep session FIFO semantics.
- Keep permission gate outside tool implementations.
- Keep Ollama as a local provider abstraction, not embedded model runtime.
- Prefer adapter/service boundaries over adding more fallback logic directly
  inside tool functions.
- Prefer real state verification over mirrored intent.

---

# 20. Final Definition of Done

The stabilization program is complete when:

### Reliability

- [ ] media actions are verified,
- [ ] stale media state cannot silently report success,
- [ ] local tool calls are canonical,
- [ ] model readiness is deterministic,
- [ ] background resources recover or fail cleanly.

### Architecture

- [ ] MediaService exists,
- [ ] ToolExecutor exists,
- [ ] tool implementations are domain-separated,
- [ ] provider quirks are inside adapters,
- [ ] application lifecycle owns long-lived resources.

### Developer experience

- [ ] setup is non-destructive,
- [ ] defaults are consistent,
- [ ] doctor reports meaningful dependency health,
- [ ] guide explains extension patterns.

### Quality

- [ ] regression tests cover confirmed bugs,
- [ ] CI runs backend and frontend gates,
- [ ] manual macOS validation matrix passes.

### Growth readiness

A developer can add a new tool or model provider without modifying unrelated
core orchestration code.

---

# Appendix A — Newly discovered issues

AI agents should append findings here instead of silently expanding scope.

Template:

```markdown
## [P1] Short title

**Found in phase:** 3  
**Files:** `...`

**Symptom:**  
...

**Root cause:**  
...

**Recommended phase:**  
...

**Blocks current phase:** yes/no
```

## [P1] Delivered generic next/previous command could be repeated

**Found in phase:** 1
**Files:** `jarvis/media/nowplaying.py`

**Symptom:**
If `nowplaying-cli` reported a successful `next` or `previous` delivery but
track identity could not be read, the fallback chain could send the same
non-idempotent action again and potentially skip multiple tracks.

**Root cause:**
The generic transport treated failed verification uniformly and retried or
fell back after delivery, without distinguishing a delivered command from a
transport that failed before delivery.

**Recommended phase:**
Phase 1 — fixed in the Phase 1 correction.

**Blocks current phase:** yes — resolved.

## [P1] Generic macOS now-playing state contaminated YT Music verification

**Found in phase:** 1
**Files:** `jarvis/agent/tools.py`

**Symptom:**
When the dedicated YT Music web adapter was unavailable, desktop `ytm_play`
and transport actions accepted `nowplaying-cli` state that could describe
JARVIS TTS or another audio application. An already-playing unrelated track
could therefore be reported as a newly requested YT Music track.

**Root cause:**
`_ytm_read_transport_state()` used generic macOS now-playing as a fallback,
and desktop `ytm_play` treated `playing=True` as sufficient without matching
the requested YT Music video identity.

**Recommended phase:**
Phase 1 — fixed by rejecting generic state and returning explicit degraded
failures when YT Music-specific state is unavailable.

**Blocks current phase:** yes — resolved in code; manual retest required.

## [P2] `tool_done` event conflated execution with operation success

**Found in phase:** 1
**Files:** `jarvis/agent/loop.py`

**Symptom:**
The debug event reported `tool_done.ok=True` whenever a tool returned without
raising, even when the tool returned structured JSON with `{"ok": false}`.

**Root cause:**
`_execute_tool()` did not inspect the structured tool result before publishing
the completion event.

**Recommended phase:**
Phase 1 — fixed with a minimal result-status projection; the Phase 5
ToolExecutor refactor has not started.

**Blocks current phase:** no — resolved.

## [P2] Frontend dependency audit reports vulnerabilities

**Found in phase:** 0  
**Files:** `web-ui/package.json`, `web-ui/package-lock.json`

**Symptom:**  
The clean `npm ci` validation reports 5 dependency vulnerabilities: 3
moderate, 1 high and 1 critical. The advisory endpoint was unavailable in the
restricted network environment, so the individual packages and runtime impact
were not confirmed.

**Root cause:**  
The frontend dependency graph, including the new test tooling, contains
transitive packages reported by npm's audit metadata.

**Recommended phase:**  
Phase 10 — CI and quality gates, with dependency remediation as a separate
focused change.

**Blocks current phase:** no

## [P1] Explicit YT Music connect did not present the existing login page

**Found in phase:** 1
**Files:** `jarvis/media/ytm_web.py`, `web-ui/src/components/ConnectionsTab.tsx`

**Symptom:**
With a live dedicated headed YT Music page in `NEEDS_LOGIN`, clicking the
connection button returned `NEEDS_LOGIN` but did not visibly present the page
or provide inline feedback explaining where to log in.

**Root cause:**
`connect()` reused the live runtime and navigated it without calling
`Page.bring_to_front()`. The frontend displayed the same status after the
request and only surfaced request exceptions through the global tool log.
Profile-directory existence also caused incomplete profiles to warm up on
every restart.

**Recommended phase:**
Phase 1 — fixed in the connection presentation correction.

**Blocks current phase:** yes — resolved in code; manual retest required.

## [P1] Logged-in YT Music page remained in NEEDS_LOGIN after Google login

**Found in phase:** 1
**Files:** `jarvis/media/ytm_web.py`, `tests/test_ytm_web.py`

**Symptom:**
After the user completed Google/YT Music login in the dedicated
`~/.jarvis/ytm_profile` browser, status polling remained `NEEDS_LOGIN` and
`ytm_play` returned a connection failure.

**Root cause:**
The probe treated a missing narrow avatar/account selector as proof of logout,
even though the real page had a usable YT Music app, navigation bar and search
surface. The adapter also tracked only one page and could miss a usable
`music.youtube.com` page created or replacing the login tab.

**Recommended phase:**
Phase 1 — fixed with multi-page adoption and evidence-based login detection;
manual connection and playback retest required.

**Blocks current phase:** yes — resolved in code; manual retest required.

## [P1] Connected YT Music search did not produce a playable result

**Found in phase:** 1
**Files:** `jarvis/media/ytm_web.py`

**Symptom:**
After the dedicated profile reached `CONNECTED`, real `ytm_play` requests for
`Relja Popović` and `Vlado Georgiev` used the `ytm_web` adapter but timed out
after 12 seconds waiting for the YT Music search-result selector. The logged
results were explicit failures (`ok=false`, `delivered=false`,
`verified=false`), so no false playback success was reported.

**Root cause:**
The selector assumed every playable result had an anchor whose href contained
`/watch?v=...`. The real page used Polymer result components with nested
`videoId`/`watchEndpoint` data and watch anchors whose href shape varied. A
second search could also leave stale rows after the input/Enter SPA update.
The focused correction initially passed Playwright `wait_for_function`
payloads positionally, but the installed Playwright API accepts that argument
only by keyword; the real readiness and player checks therefore failed until
corrected.

**Recommended phase:**
Phase 1 — fixed in the Phase 1 playback DOM correction update. The correction
uses the same authenticated browser page, direct YTM search navigation,
component-aware selection and strict DOM verification; it does not add a
desktop or normal-YouTube fallback.

**Blocks current phase:** yes — code and direct adapter validation are fixed,
but user-led manual playback/audible validation remains required.

## [P2] Headless Chrome did not render the authenticated YT Music surface

**Found in phase:** 1
**Files:** `jarvis/media/ytm_web.py`

**Symptom:**
The real authenticated profile could launch in headless Chrome and reach
`https://music.youtube.com`, but the YT Music app, search box and player bar
were not rendered in the bounded smoke test.

**Root cause:**
The exact headless rendering/autoplay incompatibility was not isolated. The
same profile works in headed Chrome, so Phase 1 does not assume that an origin
load in headless mode means that YT Music is usable.

**Recommended phase:**
Phase 1 — resolved operationally by retaining a headed dedicated browser and
starting saved normal-runtime sessions minimized. Revisit only if a reliable
background audio mode is needed later.

**Blocks current phase:** no — headed minimized restore is the selected
fallback; manual focus and audible behavior remain required.

---

# Appendix B — Phase report template

At the end of each phase, the implementing developer/AI should produce:

```markdown
## Phase N report

### Completed
- ...

### Files changed
- ...

### Root causes fixed
- ...

### Tests
- command -> result

### Manual validation
- ...

### Remaining risks
- ...

### New issues discovered
- ...

### Ready for next phase
YES / NO
```

---

# Appendix C — Prompt to give a coding AI

You can give an AI coding agent this instruction together with the repository:

```text
Read DEVELOPER-GUIDE.md and PRODUCTION-READINESS-PLAN.md completely before
editing code.

Execute only the next incomplete phase of PRODUCTION-READINESS-PLAN.md.

First inspect the current implementation and tests and confirm the described
issues still exist. Add regression coverage, implement only the scoped phase,
run all validation required by that phase, and report exact files changed,
root cause, tests and remaining risks.

Do not jump to later phases, do not perform a broad rewrite, and do not
silently expand scope. If you discover a new issue, record it in Appendix A
with severity and continue only if it blocks the current phase.

Do not mark an action successful merely because a command was sent; preserve
the project's rule that side effects should be verified whenever possible.
```

That prompt plus these two documents is the recommended AI handoff package.
