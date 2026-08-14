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
- [x] Do not set mirrored state to playing merely because a key event was
      delivered.
- [x] Return `ok=False` when no channel can verify the intended effect.
- [x] Add adapter/method metadata to diagnostic result.
- [x] Preserve current working play/pause behavior while strengthening
      verification.

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
- Converted the Phase 0 strict YT Music xfails into passing regressions and
  added web/generic transport verification coverage.

### Files changed

- `jarvis/agent/tools.py`
- `jarvis/media/nowplaying.py`
- `jarvis/media/ytm_web.py`
- `tests/test_phase0_media_regressions.py`
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

### Validation

- `./.venv/bin/pytest tests/test_phase0_media_regressions.py tests/test_ytm_web.py tests/test_nowplaying.py -q` -> PASS (`21 passed`)
- `./.venv/bin/pytest -q` -> PASS (`105 passed, 3 xfailed`; the xfails are the known Phase 3 local-model regressions)
- `./.venv/bin/ruff check .` -> FAIL (4 pre-existing findings in `jarvis/audio/focus.py`, `jarvis/log.py`, `jarvis/media/ytm_web.py` and `tests/test_web_ui_7b.py`)
- `./.venv/bin/ruff format --check .` -> FAIL (8 pre-existing formatted files; no added Phase 1 formatting issue)
- `git diff --check` -> PASS
- `cd web-ui && npm run typecheck` -> PASS
- `cd web-ui && npm run test` -> PASS (1 test)
- `cd web-ui && npm run build` -> PASS

### Manual validation still required

- Real macOS/YT Music validation was not run by the agent. On macOS, start
  JARVIS with `./scripts/start.sh`, ensure YT Music is installed and logged
  in, then issue `play`, `pause`, `resume`, `next` five times and `previous`
  three times through the UI. Confirm every transition changes the track and
  that no `next`/`previous` action pauses playback unexpectedly. Repeat with
  the browser adapter unavailable if desktop fallback coverage is required.

### New issues discovered

- None. Existing Phase 3 xfails, the Phase 0 frontend dependency advisory and
  the deferred MediaService world-state test remain outside this phase.

### Remaining risks

- The desktop fallback still relies on generic macOS now-playing evidence when
  the dedicated YTM web state is unavailable; YTM-specific manual validation
  is required to confirm what MediaRemote exposes on this machine.
- Full Ruff validation remains red on pre-existing repository findings.
- Phase 2 MediaService/state-ownership work has intentionally not started.

### Ready for next phase

YES — Phase 1 is complete and ready for review/merge. Stop here; Phase 2 has
not been started.

---

# 5. Phase 2 — Introduce authoritative MediaService

**Priority:** P0/P1  
**Goal:** remove competing media truths.

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
