# JARVIS — Codex / AI Agent Instructions

> **Scope:** this repository only  
> **Project:** JARVIS  
> **Primary environment:** macOS, Apple Silicon  
> **Purpose:** give coding agents durable project rules so work remains safe,
> reviewable and aligned with the stabilization roadmap.
>
> This file is intentionally strict. JARVIS controls real local applications,
> audio, browser automation and OS-level tools; correctness and verifiability
> matter more than making broad changes quickly.

---

# 1. Read this before editing

Before changing code, read these files completely:

1. `AGENTS.md`
2. `DEVELOPER-GUIDE.md`
3. `PRODUCTION-READINESS-PLAN.md`
4. `README.md` when setup/runtime behavior is relevant

`DEVELOPER-GUIDE.md` describes the long-lived target architecture and
engineering standards.

`PRODUCTION-READINESS-PLAN.md` is the executable stabilization roadmap.

If those documents disagree with the current implementation, inspect the code
and tests and report the mismatch. Do not blindly rewrite working behavior
just because a document is stale.

---

# 2. Stabilization mode

Until `PRODUCTION-READINESS-PLAN.md` is complete, the repository is in
**stabilization mode**.

When asked to execute a roadmap phase:

- Work on **one phase only** unless the user explicitly authorizes more.
- Confirm the described issue still exists before fixing it.
- Add regression coverage before or together with the fix.
- Do not jump ahead to later phases.
- Do not perform opportunistic rewrites.
- Do not “clean up” unrelated files.
- Do not silently expand the task.
- At the end, produce the phase report required by the plan.
- Stop after the phase checkpoint.

If a new bug is discovered, add it to Appendix A of
`PRODUCTION-READINESS-PLAN.md` when appropriate instead of silently pulling it
into the current scope.

Only fix a newly discovered issue immediately when:

1. it blocks the active phase, or
2. it is a genuine P0 correctness/data-loss/security issue.

---

# 3. Core engineering principles

## 3.1 Verify side effects

Never treat:

```text
command was sent
```

as equivalent to:

```text
requested effect happened
```

This is especially important for:

- YouTube Music,
- AppleScript,
- Quartz keyboard events,
- subprocess-based tools,
- local model loading,
- browser automation,
- audio focus.

When practical, use:

```text
state before
-> command
-> state after
-> verification
```

If verification is impossible, return an explicit best-effort/degraded result.

Do not return `ok=True` merely because an API call, key event or subprocess
completed without raising an exception.

---

## 3.2 One source of truth per domain

Avoid competing state authorities.

Target ownership:

```text
sessions       -> session service/repository
models         -> local runner/model router
tools          -> tool registry/executor
media          -> MediaService
audio          -> audio services
configuration  -> config layer
permissions    -> PermissionStore
```

Do not introduce new mirrored booleans or global state unless the value is
explicitly documented as a non-authoritative cache.

---

## 3.3 Keep provider quirks inside adapters

The agent loop should consume a canonical internal protocol.

Cloud and local LLM providers may stream data differently, but those
differences belong inside provider/adaptation code.

The agent loop should not contain MiniMax-specific, Ollama-specific or future
provider-specific parsing rules.

Canonical model stream:

```text
delta
reasoning
finish
done
```

Canonical final assistant message:

```python
{
    "role": "assistant",
    "content": "...",
    "tool_calls": [...],
    "finish_reason": "...",
}
```

---

## 3.4 Domain services own implementation details

LLM tools should express intent, not OS mechanics.

Preferred:

```text
ytm_next tool
    ->
MediaService.next()
    ->
adapter
    ->
verify
```

Avoid:

```text
ytm_next tool
    ->
PID lookup
    ->
Quartz
    ->
AppleScript
    ->
DOM
    ->
manual state mutation
```

inside a central registry module.

---

## 3.5 Prefer small reviewable changes

A correct 5-file PR is preferable to a difficult-to-debug 35-file rewrite.

Keep commits focused.

Suggested commit style:

```text
fix ytm transport verification
unify local tool call assembly
make local model selection wait for readiness
split media tool implementation
```

Avoid meaningless commit messages such as:

```text
updates
fix stuff
refactor everything
```

---

# 4. Git safety

Before editing:

```bash
git status -sb
git branch --show-current
```

Rules:

- Never discard user changes.
- Never reset, checkout over or delete unrelated uncommitted work.
- Do not use destructive git commands unless the user explicitly requests
  them and the consequences are understood.
- If the working tree contains unrelated changes, keep your edits isolated.
- Do not stage unrelated files.
- Prefer a dedicated branch for each roadmap phase or tightly related group of
  tasks.
- Do not force-push unless explicitly requested.
- Do not modify `main` directly when the workflow is using PR branches.

If multiple Codex agents/threads are used on the same repository:

- each writing agent should use its own worktree/branch,
- do not let two agents concurrently edit the same central files,
- use additional agents primarily for read-only review/audit unless the tasks
  are truly independent.

For this stabilization effort, prefer **one writer per phase**.

---

# 5. Required validation

Run targeted tests while developing.

Before declaring a phase complete, run the validation requested by the phase
and, when available, the following baseline.

Backend:

```bash
pytest
ruff check .
ruff format --check .
```

Frontend:

```bash
cd web-ui
npm run typecheck
npm run build
```

If dependencies need a clean install for the task:

```bash
cd web-ui
npm ci
```

Use the project's environment/venv when one exists.

Do not claim a command passed if it was not executed.

If a command cannot run because a dependency or platform service is missing:

- state the exact blocker,
- run all remaining checks that are possible,
- distinguish `not run` from `failed`.

---

# 6. Test rules

Every confirmed regression should receive a regression test where practical.

Tests should validate behavior rather than implementation trivia.

Good regression:

```text
next command does not send an unconditional play/pause toggle
```

Weak regression:

```text
private helper was called exactly once
```

Prefer fakes/adapters for external systems.

Tests should not require:

- a real Google login,
- real YouTube Music playback,
- an installed Ollama model,
- live API credentials,

unless explicitly marked as platform/integration tests.

Use markers for environment-dependent tests when appropriate:

```text
macos
integration
requires_ytm
requires_ollama
```

Do not weaken existing tests just to make a refactor pass.

---

# 7. Python backend standards

- Use type hints on public APIs.
- Prefer dataclasses or Pydantic models for cross-module domain data.
- Keep blocking I/O off the asyncio event loop.
- External calls require timeouts.
- Do not introduce unbounded background tasks.
- Long-lived resources need explicit lifecycle ownership.
- Avoid new module-level mutable global state.
- Avoid broad `except Exception: pass` in critical code.
- Log recoverable failures instead of swallowing them.
- Never log secrets.
- Keep functions small enough that ownership is obvious.
- Use dependency injection/service boundaries where it materially improves
  testability; do not introduce a framework purely for abstraction.

---

# 8. Frontend standards

Frontend root:

```text
web-ui/
```

Active UI is React + TypeScript + Vite.

Do not treat historical “vanilla JS frontend” documentation as current
architecture.

Rules:

- Distinguish selected UI state from confirmed backend state.
- Do not optimistically mark infrastructure `ready`.
- Model loading should have explicit pending/loading/error state.
- Keep WebSocket events session-scoped where required.
- Do not let events from an old session pollute the active session.
- Do not silently swallow errors that materially change user-visible state.
- Preserve TypeScript correctness.
- Prefer small pure helpers/state transitions when logic needs tests.

---

# 9. Agent/session invariants

Relevant core:

```text
jarvis/agent/loop.py
```

Preserve these invariants:

- one executing turn per session,
- FIFO queued user turns,
- barge-in/cancel does not corrupt history,
- an assistant tool-call message must not remain without corresponding tool
  results,
- history trimming must not split tool-call groups,
- cancellation must not leave UI permanently busy,
- persisted sessions should remain readable after a restart.

Do not change session orchestration casually as part of unrelated work.

---

# 10. LLM / local model rules

Relevant files include:

```text
jarvis/llm.py
jarvis/local_models.py
jarvis/agent/loop.py
jarvis/agent/prompts.py
```

## Tool-call streaming

Never execute raw streaming tool fragments.

Use one canonical accumulator capable of combining:

- streamed call id,
- tool-call index,
- partial function name,
- partial JSON arguments,
- providers that repeat a final full call.

Cloud and Ollama should converge into the same canonical `ToolCall[]`.

## Capability detection

Tool capability must not be inferred from HTTP 200 alone.

A tools probe succeeds only when the response includes a valid structured
tool call to a prompt requiring one.

Represent at least:

```text
tools
notools
unknown
```

Explicit configuration overrides may take precedence when intentionally
defined.

## Local readiness

A local model should not be active for chat before it is `READY`.

Preferred lifecycle:

```text
IDLE
LOADING
READY
ERROR
UNLOADING
```

If the local model fails and cloud fallback is used:

- emit an explicit event,
- keep logs clear,
- do not make the fallback invisible to the user.

## No-tools models

No-tools models should receive:

- no tool schema,
- sanitized history without tool mechanics,
- a no-tools system prompt,
- clear user-visible labeling.

Do not teach a no-tools model to imitate fake tool calls in plain text.

---

# 11. YouTube Music / media rules

This is a high-risk regression area.

Relevant current files include:

```text
jarvis/media/ytm_web.py
jarvis/media/nowplaying.py
jarvis/agent/tools.py
jarvis/context.py
```

Target architecture is documented in `DEVELOPER-GUIDE.md` and the production
roadmap.

## Critical rule: no blind toggle after next/previous

Do not implement:

```text
next
sleep
Space
```

or:

```text
previous
sleep
Space
```

because Space is a playback toggle and may pause a successfully started track.

Correct behavior is:

```text
read before
send next/previous
read after
verify track changed
```

Only send play/resume if real state proves it is required by the requested
policy.

## Media state

The long-term authority should be `MediaService`.

Do not let these independently claim truth:

```text
generic nowplaying
YTM DOM
mirrored local bool
window title
```

Adapters may collect evidence, but one service should normalize it.

## Web adapter readiness

Do not treat a stale `_ready=True` cache as proof of health.

Health should consider:

- browser/context still connected,
- page not closed,
- expected origin,
- DOM responsiveness.

Also distinguish:

```text
browser ready
search ready
player available
track loaded
```

A healthy YTM home page with no loaded song can still be ready for search.

## Verification

Pause:

```text
after.playing == False
```

Resume:

```text
after.playing == True
```

Next/previous:

```text
track identity changed
```

Preferred identity order:

```text
track id
-> title + artist
-> documented degraded fallback
```

---

# 12. Tool architecture rules

The current central `jarvis/agent/tools.py` is a migration target, not a
pattern for new features.

Do not add large new domain implementations there.

Target layering:

```text
ToolRegistry
    ->
ToolExecutor
    + validation
    + permission
    + timeout
    + error normalization
    ->
ToolHandler
    ->
DomainService
    ->
Adapter
```

Target result shape:

```python
ToolResult(
    ok=True/False,
    data={...},
    error=...,
    meta={...},
)
```

Useful normalized error codes include:

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

Do not create dozens of error classes before they are needed.

---

# 13. Permission rules

Permission policy lives outside individual tool implementations.

Current conceptual policies:

```text
allow
ask
deny
```

No side effect should occur before permission resolution when a tool requires
approval.

Do not bypass the permission system to make tests or UX easier.

Tests may use a fake/allowing permission store.

---

# 14. Audio rules

Relevant:

```text
jarvis/audio/speech.py
jarvis/audio/focus.py
jarvis/audio/stt.py
jarvis/audio/tts.py
jarvis/hotkey.py
```

Preserve server-driven sentence-streaming TTS.

When the user starts speaking, the intended lifecycle is:

```text
cancel/suppress TTS
take audio focus
capture audio
release focus
transcribe
send/populate
```

Browser microphone and PTT should converge conceptually rather than invent
separate behavior.

Do not allow long ML work to starve short OS tool execution.

---

# 15. Configuration rules

Relevant:

```text
jarvis/config.py
.env
.env.example
scripts/setup.sh
requirements.txt
```

## Existing `.env`

Never silently rewrite an existing user `.env`.

Required setup behavior:

```text
.env does not exist -> create from example
.env exists         -> leave unchanged
```

Do not “refresh defaults” by replacing non-secret user settings.

## Defaults

There should ultimately be one runtime source of truth.

If changing a default, update:

- runtime config,
- `.env.example`,
- README or developer documentation where relevant,

in the same change.

## Secrets

Never print or commit:

- API keys,
- auth headers,
- browser cookies,
- Google profile/session data,
- private tokens.

---

# 16. Dependencies

Core/default runtime behavior must not depend on a package that is only
present in a commented example.

When touching dependencies:

- verify clean installation,
- separate optional heavyweight ML/browser dependencies clearly,
- do not move necessary default dependencies into optional extras accidentally,
- run the relevant import/smoke check.

Do not upgrade unrelated dependency versions during a bug-fix phase unless
required.

---

# 17. Application lifecycle

Relevant:

```text
jarvis/app.py
```

Every long-lived resource needs clear startup/shutdown ownership.

Examples:

```text
shared http client
Playwright YTM browser/context
PTT listener
speech playback
local model pull tasks
session workers
custom executors
```

FastAPI lifespan should eventually centralize this.

When adding a long-lived service, define how it stops before considering the
feature complete.

Avoid orphan subprocesses/browser resources after server shutdown.

---

# 18. Persistence and concurrency

Relevant:

```text
jarvis/state_store.py
jarvis/agent/loop.py
data/*
```

Atomic tmp+replace is a good baseline, but concurrent writers still need
coordination.

Do not introduce a database unless the problem genuinely requires one.

For current scale, prefer:

- async lock,
- single writer,
- small repository abstraction,

before adopting larger infrastructure.

Persisted corruption must not prevent the whole app from starting when a
best-effort recovery is possible.

---

# 19. Event bus / observability

Relevant:

```text
jarvis/bus.py
web-ui/src/lib/bus.ts
```

Preserve bounded subscriber queues.

Important operations should eventually carry:

```text
session_id
turn_id
tool_call_id
provider/model
adapter
duration
```

Do not log private content unnecessarily.

For media failures, logs should answer:

```text
adapter
before state
command
after state
verification result
fallback
```

For local model fallback:

```text
requested model
runner state
failure reason
fallback model/provider
```

---

# 20. README vs developer documents

Use documentation by responsibility.

`README.md`:
- user-facing overview,
- install,
- run,
- basic usage.

`DEVELOPER-GUIDE.md`:
- architecture,
- extension patterns,
- engineering standards,
- debugging guidance.

`PRODUCTION-READINESS-PLAN.md`:
- temporary execution roadmap,
- phases,
- stabilization acceptance criteria.

`AGENTS.md`:
- durable instructions for Codex/AI coding agents.

Do not merge all four purposes into one huge README.

---

# 21. How to add a new tool

Before implementing:

1. Identify the domain.
2. Reuse/create the domain service.
3. Define a small schema.
4. Define normalized result/error behavior.
5. Register tool.
6. Assign permission policy.
7. Define timeout.
8. Add tests.
9. Add system-prompt guidance only when necessary.

Do not put 100+ lines of new domain logic in a registry file.

---

# 22. How to add a model provider

A provider adapter is responsible for:

- request formatting,
- authentication,
- streaming parsing,
- reasoning extraction,
- canonical tool-call assembly,
- timeout/network error normalization,
- cancellation semantics.

The agent loop should not need provider-specific special cases for ordinary
protocol differences.

---

# 23. How to add a media adapter

Expected conceptual interface:

```python
async def health()
async def get_state()
async def play_query(query)
async def pause()
async def resume()
async def next()
async def previous()
async def close()
```

Adapter returns evidence/state.

MediaService decides:

- which adapter is primary,
- when fallback is allowed,
- how verification works,
- which result becomes authoritative.

---

# 24. Scope control for AI agents

Before modifying a file, be able to explain why it belongs to the active
task.

If a requested phase is “local model tool streaming”, unrelated files such as
TTS voices, CSS and calendar tools should normally remain untouched.

If a broad refactor appears necessary:

1. stop,
2. explain why,
3. identify the minimal boundary change,
4. proceed only when it is genuinely required by the phase.

Do not create abstractions “for future flexibility” without an immediate
project need.

---

# 25. Multi-agent / parallel-work rules

Codex may be used with multiple threads/worktrees, but this project should not
default to many simultaneous writers.

Recommended pattern during stabilization:

```text
Writer agent
    -> implements current phase

Reviewer agent (optional, read-only)
    -> audits diff / tests

Specialist audit agent (optional, read-only)
    -> investigates YTM/local-model area

Writer agent
    -> addresses validated findings
```

If two agents both write:

- tasks must be genuinely independent,
- each uses its own branch/worktree,
- central files must not overlap,
- integration happens only after both changes are reviewed.

Do not ask agents to race on alternate implementations unless the user
explicitly wants a comparison experiment.

---

# 26. Phase completion report

At the end of a roadmap phase, report:

```markdown
## Phase N report

### Root cause
...

### Completed
- ...

### Files changed
- ...

### Tests added/changed
- ...

### Validation
- `command` -> PASS / FAIL / NOT RUN

### Manual validation still required
- ...

### New issues discovered
- ...

### Remaining risks
- ...

### Ready for next phase
YES / NO
```

If `NO`, explain the blocker.

Do not automatically continue to the next phase.

---

# 27. Before claiming a task complete

Check:

- Did I actually solve the stated issue?
- Did I add regression coverage?
- Did I preserve unrelated behavior?
- Did I run the required checks?
- Did I distinguish automated validation from manual validation?
- Did I avoid exposing secrets?
- Did I leave git state understandable?
- Did I accidentally start a later roadmap phase?
- Did I update plan checkboxes/report if requested?

Only then declare completion.

---

# 28. Current recommended stabilization sequence

Follow `PRODUCTION-READINESS-PLAN.md` as the authority.

Expected order:

```text
Phase 0  safety net
Phase 1  immediate YTM correctness
Phase 2  MediaService
Phase 3  local tool streaming/capability
Phase 4  local model readiness/UI
Phase 5  tool architecture
Phase 6  config/setup/dependencies/version
Phase 7  lifecycle/concurrency
Phase 8  frontend async cleanup
Phase 9  observability/doctor
Phase 10 CI
Phase 11 docs/release
```

Do not reorder simply for convenience.

---

# 29. Default instruction when a task is ambiguous

When working from the production roadmap and the user says something like:

```text
continue
```

interpret it as:

```text
continue with the next incomplete roadmap phase only
```

Read the latest plan state first.

If the user explicitly requests a separate feature or bug fix, follow that
request instead and state whether it falls inside or outside the roadmap.

---

# 30. Final project rule

JARVIS is a real-action assistant.

The standard is not:

> “the code probably sent the right thing.”

The standard is:

> “the system has clear ownership, bounded failure modes, observable behavior
> and verifies real effects whenever technically possible.”

Build toward that standard phase by phase.
