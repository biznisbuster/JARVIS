# JARVIS Production Readiness Plan

This document is the phased stabilization roadmap for JARVIS. Execute one phase at a time. For each phase, first confirm the described issue still exists, add regression coverage, make only the changes required by that phase, run the relevant validation, summarize the result, and stop before moving to the next phase unless continuation is explicitly requested.

## Phase 0 — Safety net

Record the current backend and frontend validation baseline. Add regression coverage for confirmed media, local-model and readiness defects. Tests should not require real Google or Ollama credentials unless explicitly marked as platform/integration tests.

## Phase 1 — YouTube Music correctness

Fix the desktop fallback that sends next or previous and then an unconditional Space toggle. Verify pause/resume from post-action playback state. Verify next/previous from track change when possible. Delivery of a key event alone is not success.

## Phase 2 — MediaService

Introduce one normalized playback state and one MediaService. Extract YT Music desktop behavior from the central tools module. Treat web and desktop control as adapters behind the service. Route world-state, status and YTM actions through the same service. Add health checks and recovery for a dead or stale Playwright page.

## Phase 3 — Local model tool calls

Use one shared tool-call accumulator for cloud and Ollama streaming. Assemble fragmented calls before execution. Rewrite local tool capability detection so support is confirmed only when a valid structured tool call is actually returned. Preserve a clear tools/notools/unknown state.

## Phase 4 — Local model readiness

Formalize local model lifecycle states and make the frontend wait for backend READY before making a local model active. Represent loading and errors explicitly. Avoid the race where the UI shows a local model while the backend falls back to cloud because loading has not completed.

## Phase 5 — Tool architecture

Introduce a central ToolRegistry and ToolExecutor. Centralize dispatch, validation, authorization policy, timeout behavior, error normalization and events. Split Apple, system, search, media and coding implementations into domain modules. Adding a simple tool should no longer require changes to the agent loop.

## Phase 6 — Setup, configuration and dependency consistency

Make repeated setup non-destructive to user configuration. Fix the default TTS dependency mismatch. Separate core and optional heavy dependencies clearly. Align runtime defaults and examples. Use one application version source across status surfaces.

## Phase 7 — Lifecycle and persistence hardening

Create a central startup/shutdown path for long-lived resources, including the YTM browser, shared clients, voice services, background model operations and session workers. Make session persistence deterministic under concurrent completion and cancellation.

## Phase 8 — Frontend asynchronous state cleanup

Audit model transitions, microphone/audio-focus timing, session switching during streams, connection/error states and other optimistic state updates. The UI should not display asynchronous infrastructure as ready before backend confirmation.

## Phase 9 — Observability and diagnostics

Add consistent turn and tool-call identifiers and structured timing around model requests, tools, media verification, speech recognition and speech synthesis. Extend diagnostics so future failures can be traced to the adapter, state transition and fallback path involved.

## Phase 10 — Continuous integration

Add backend test/lint checks and frontend typecheck/build checks to every change. Keep platform-specific YTM, macOS and Ollama validation separated from deterministic generic checks.

## Phase 11 — Documentation and release hardening

Use `DEVELOPER-GUIDE.md` as the long-lived engineering source. Keep this file as the temporary implementation roadmap. Synchronize setup, model, media and manual macOS validation documentation when stabilization is complete.

## Confirmed or high-confidence issues driving the plan

- YTM next/previous fallback can accidentally toggle playback off.
- YTM state is represented through several competing paths.
- Ollama streaming can treat fragments of one tool call as separate calls.
- Local tool capability detection can report support without observing a real tool call.
- The frontend can mark a local model active before it is ready.
- Generic world-state media information can disagree with dedicated YTM state.
- `agent/tools.py` currently owns too many unrelated domains.
- Setup and documented defaults can drift from actual runtime behavior.
- Long-lived service shutdown is not yet centralized.

## Final definition of done

The stabilization program is complete when media actions are verified, media has one source of truth, local tool calls are canonical, local model readiness is deterministic, the tool layer has clear execution boundaries, setup is predictable, configuration and versions agree, background resources are cleaned up, persistence is safe under concurrency, important regressions are tested, quality checks run automatically, and failures can be diagnosed from structured runtime information.

## AI agent handoff rule

An AI coding agent should read this file and `DEVELOPER-GUIDE.md` before editing. It should execute only the next incomplete phase, verify the current implementation first, keep changes scoped, add regression coverage, validate the result, and report files changed, root cause, tests and remaining risks. New issues should be recorded separately unless they block the active phase. Avoid broad rewrites and preserve the rule that real side effects should be verified whenever possible.
