# JARVIS — Developer Guide

> **Status:** Living developer documentation  
> **Project:** `biznisbuster/JARVIS`  
> **Platform:** macOS / Apple Silicon oriented  
> **Last major audit:** 2026-08-13  
>
> This document explains how JARVIS is structured, how its runtime flows work,
> how to extend it safely, and which engineering rules should be preserved as
> the project grows.
>
> This is **not** the bug-fixing roadmap. For the current stabilization work,
> use `PRODUCTION-READINESS-PLAN.md`.

---

## 1. Product goal

JARVIS is a local-first personal assistant for macOS with four major responsibilities:

1. Hold conversational sessions through cloud or local LLMs.
2. Execute real actions through tools with a permission gate.
3. Support low-latency voice input/output.
4. Control local desktop/media workflows, especially YouTube Music.

The project should optimize for:

- **Correctness before confidence** — never report an action as successful unless its effect is known or explicitly marked as best-effort.
- **One source of truth per domain** — playback state, model state, session state, configuration and permissions should not each be mirrored in several unrelated modules.
- **Provider independence** — MiniMax, Ollama and future providers should adapt to one canonical internal protocol.
- **Recoverability** — external dependencies will fail. Browser tabs die, Ollama restarts, subprocesses time out, APIs return malformed data. Failures must degrade cleanly.
- **Incremental extensibility** — adding a tool must not require editing the agent core.

---

# 2. High-level architecture

Current logical flow:

```text
React/Vite UI
    |
    | REST + WebSocket
    v
FastAPI application
    |
    +----------------------+----------------------+----------------------+
    |                      |                      |                      |
Session/Agent Loop      Audio layer           Local models          Tool layer
    |                      |                      |                      |
    v                      v                      v                      v
LLM adapters           STT / TTS / PTT         Ollama            OS / Apps / Web
    |
    +--> cloud providers
    +--> local adapter
```

Target architecture after stabilization:

```text
                         AppServices
                             |
          +------------------+------------------+
          |                  |                  |
      AgentService       MediaService       AudioService
          |                  |                  |
   ModelRouter         Media adapters       STT/TTS/PTT
          |
    +-----+------+
    |            |
 CloudAdapter  OllamaAdapter
    |
 canonical streaming events
 canonical ToolCall[]
          |
     ToolExecutor
          |
  Registry -> Validator -> PermissionGate -> ToolHandler -> Domain Service
```

The important distinction is:

> The LLM chooses **what** action is needed. Domain services decide **how**
> that action is reliably executed.

---

# 3. Repository map

Important backend modules currently include:

```text
jarvis/
├── app.py                 FastAPI app, REST endpoints, WebSocket, lifespan
├── config.py              Runtime settings / .env parsing
├── context.py             Per-turn world state
├── llm.py                 Cloud streaming + local adapter routing
├── local_models.py        Ollama discovery/load/unload/streaming
├── permissions.py         allow / ask / deny tool policies
├── bus.py                 in-process event bus
├── state.py               runtime singletons
├── state_store.py         small persistent key/value state
├── __main__.py            CLI: serve/ui/stop/doctor
│
├── agent/
│   ├── loop.py            session queue + turn orchestration
│   ├── tools.py           current tool registry + many implementations
│   ├── prompts.py         system prompts
│   └── kilo_bridge.py     Kilo CLI bridge
│
├── media/
│   ├── nowplaying.py      generic macOS media state/control
│   └── ytm_web.py         persistent Playwright YT Music controller
│
└── audio/
    ├── focus.py           listen-mode system audio focus
    ├── player.py          server-side audio playback
    ├── speech.py          sentence streaming TTS scheduler
    ├── stt.py             Whisper backends
    └── tts.py             TTS backends
```

Frontend:

```text
web-ui/
├── src/
│   ├── App.tsx
│   ├── store.ts
│   ├── components/
│   └── lib/
│       ├── actions.ts
│       ├── api.ts
│       ├── bus.ts
│       ├── speech.ts
│       ├── tools.ts
│       ├── text.ts
│       └── types.ts
├── package.json
└── vite.config.ts
```

Do not use the old architectural descriptions that refer to the frontend as
“vanilla JS” as the source of truth. The active frontend is React/Vite.

---

# 4. Runtime: one chat turn

A normal text turn should conceptually work like this:

```text
POST /api/chat
   |
Session FIFO queue
   |
run_turn()
   |
build world state once
   |
select model backend
   |
stream model output
   |
   +--> assistant deltas -> BUS -> UI
   +--> assistant deltas -> SPEECH -> TTS
   |
model may return canonical tool calls
   |
ToolExecutor
   |
permission check
   |
execute tool
   |
append structured tool result
   |
next LLM iteration
   |
final assistant answer
```

### Session invariants

For each session:

- Only one active turn may execute at a time.
- Queued user messages are FIFO.
- Cancellation must leave valid chat history.
- An assistant `tool_calls` message must never be left without matching tool
  results in persisted history.
- History trimming must cut only at safe turn boundaries.

---

# 5. Model architecture

## 5.1 Canonical provider contract

Every model backend should eventually expose the same protocol:

```text
delta
reasoning
finish
done
```

`done` must contain one normalized assistant object:

```python
{
    "role": "assistant",
    "content": "...",
    "tool_calls": [...],
    "finish_reason": "...",
}
```

Provider-specific streaming quirks must be handled **inside the provider
adapter**, not inside the agent loop.

## 5.2 Tool-call assembly

Streaming providers often split one tool call across multiple chunks.

Never execute raw deltas.

Required flow:

```text
provider deltas
     |
ToolCallAccumulator
     |
validated canonical ToolCall[]
     |
agent loop
```

Canonical tool call:

```python
{
    "id": "call_x",
    "type": "function",
    "function": {
        "name": "ytm_play",
        "arguments": "{\"query\":\"...\"}"
    }
}
```

Cloud and Ollama must use the same assembly behavior.

## 5.3 Local model lifecycle

A local model should have explicit states:

```text
UNAVAILABLE
IDLE
LOADING
READY
ERROR
UNLOADING
```

A model must not become the active chat model until state is `READY`.

UI selection and backend readiness are not the same concept.

## 5.4 Local model capabilities

Capabilities should be explicit data, not assumptions:

```text
chat
tools
reasoning
vision
context_window
```

For tool support, HTTP 200 is **not enough**.

A capability probe only succeeds if the model returns a valid structured tool
call to a prompt explicitly requiring one.

Models without tools should receive:

- no tool schema,
- a sanitized history,
- a no-tools system prompt,
- clear UI labeling.

---

# 6. Tool system

## 6.1 Desired responsibilities

The agent should not know implementation details of OS actions.

Target layering:

```text
Tool schema
   |
ToolRegistry
   |
ToolExecutor
   +--> argument validation
   +--> permission check
   +--> timeout
   +--> execution
   +--> normalized result
   |
Domain service
   |
Adapter / OS integration
```

## 6.2 Tool result contract

Do not return arbitrary success strings.

Target structure:

```python
class ToolError:
    code: str
    message: str
    retryable: bool

class ToolResult:
    ok: bool
    data: dict
    error: ToolError | None
    meta: dict
```

Example:

```json
{
  "ok": false,
  "data": {},
  "error": {
    "code": "MEDIA_NOT_READY",
    "message": "YouTube Music is not ready",
    "retryable": true
  },
  "meta": {
    "adapter": "ytm_web"
  }
}
```

The LLM may receive serialized JSON, but internal Python should work with
typed objects until the message boundary.

## 6.3 Timeouts

Every external tool must have a bounded timeout.

The tool executor should own timeout policy rather than every tool inventing
its own behavior.

Examples:

```text
clipboard:       short
osascript:       short
web search:      medium
browser action:  medium
Kilo:            long
```

No tool may block a chat turn forever.

## 6.4 Permission gate

Permission rules remain:

```text
allow
ask
deny
```

The permission layer should operate before side effects occur.

Do not put permission logic inside individual tool implementations.

---

# 7. Media / YouTube Music

Media is a domain service, not a collection of unrelated tools.

## 7.1 Target model

```text
MediaService
   |
   +--> YtmWebAdapter       authoritative YT Music adapter
```

The service owns:

- the authoritative YT Music adapter,
- health,
- playback state,
- verification,
- mutation serialization,
- explicit failure when YT Music is unavailable.

Generic macOS now-playing remains a separate system-media capability. It is
not an automatic YT Music fallback and cannot verify YT Music actions.

## 7.2 Playback state

Use one normalized object:

```python
PlaybackState(
    ok=True,
    health=AdapterHealth(...),
    player_available=True,
    playing=True,
    title="...",
    artist="...",
    current_time=12.3,
    duration=201.0,
    source="ytm_web",
    track_id="...",
)
```

Avoid authoritative mirrored booleans such as `_YTM_STATE` when a real state
reader is available.

A mirrored state may exist only as explicitly marked best-effort metadata.

## 7.3 Action verification

Rules:

### Pause

```text
send pause
read state
success only if playing == false
```

### Resume

```text
send play
read state
success only if playing == true
```

### Next / previous

```text
read BEFORE
send command
read AFTER
success if track identity changed
```

Do not send a generic play/pause toggle after next/previous unless real state
proves playback is paused and the intended policy explicitly requires resume.

## 7.4 Web adapter lifecycle

The Playwright adapter should distinguish:

```text
browser ready
search UI ready
player available
track loaded
```

A healthy YT Music home page with no track is still usable for search.

Health checks should verify:

- browser/context connected,
- page not closed,
- expected hostname,
- DOM responsive.

A stale `_ready=True` flag is not sufficient.

## 7.5 World state

`context.py` must use the authoritative media service.

Do not let:

```text
system prompt -> generic nowplaying
ytm_status    -> YTM DOM
transport     -> mirrored state
```

represent three competing realities.

---

# 8. Audio architecture

## 8.1 Speech output

`speech.py` is responsible for:

- buffering streaming assistant text,
- sentence segmentation,
- ahead-of-time synthesis,
- ordered playback events,
- cancellation,
- suppression during audio tools.

Keep this server-driven.

## 8.2 Listen focus

When the user begins speaking:

1. Cancel assistant speech.
2. Take audio focus.
3. Start capture only after focus is acquired where practical.
4. Record.
5. Release focus.
6. Transcribe.
7. Send or populate draft according to configuration.

Browser and PTT flows should use the same conceptual lifecycle.

## 8.3 STT

STT model load and transcription should stay outside the default executor.

Long-running ML work must not starve short OS tool operations.

---

# 9. Frontend state rules

The frontend is a client of backend truth.

Do not optimistically mark asynchronous infrastructure as ready.

Examples:

Bad:

```text
set currentModel=local
start loading local model
```

Good:

```text
pendingModel=local
load
success -> currentModel=local
failure -> keep previous model
```

Recommended model UI state:

```text
selected
loading
ready
error
```

Disable or queue chat send while an explicitly selected model is still
loading.

WebSocket event handling must remain session-scoped where applicable.

---

# 10. Configuration

## 10.1 Source of truth

Runtime defaults should live in one place.

Recommended rule:

```text
config.py        = runtime defaults and validation
.env             = user overrides
.env.example     = documented example
README / guide   = explanation, not separate defaults
```

If a default changes, update all user-facing documentation in the same PR.

## 10.2 Existing `.env`

Setup scripts must never silently reset an existing user `.env`.

Rule:

```text
.env missing  -> copy .env.example
.env exists   -> leave untouched
```

Configuration migrations, if ever needed, should be explicit.

## 10.3 Secrets

Never log:

- provider API keys,
- auth headers,
- browser cookies,
- Google session data,
- Kilo credentials.

---

# 11. Application lifecycle

Every long-lived resource needs `start` and `stop`.

Candidate `AppServices` resources:

```text
shared HTTP client
YT Music browser
speech scheduler
PTT listener
local pull tasks
session workers
ML executors
```

FastAPI lifespan should:

```text
START
  attach bus
  load persistence
  start/warm optional services

STOP
  cancel background work
  stop PTT
  shutdown YTM browser
  close HTTP clients
  flush persistence
```

Best-effort startup is acceptable for optional services.

Silent resource leaks at shutdown are not.

---

# 12. Persistence

Small persistent state uses atomic tmp+replace writes, which is a good base.

As concurrency grows, use a single writer or an async lock around each
persistent store.

Persisted user history is application data. Treat corruption defensively:

- invalid file must not prevent startup,
- log the problem,
- preserve recoverable data where possible.

---

# 13. Error handling rules

Avoid broad silent failures in core paths.

Use:

```text
DEBUG   low-level adapter diagnostics
INFO    lifecycle and successful domain actions
WARNING recoverable degraded behavior / fallback
ERROR   user-visible operation failure
```

When catching an exception:

- include operation name,
- include adapter/provider,
- preserve safe context,
- never include secrets.

Do not convert every failure into `ok=True`.

---

# 14. Observability

Every turn should eventually have:

```text
turn_id
session_id
model
provider
iteration
tool_call_id
```

Useful timings:

```text
LLM first-token latency
LLM total latency
tool latency
YT Music command latency
YT Music fallback count
local -> cloud fallback count
STT latency
TTS first-audio latency
```

Structured events should make it possible to answer:

> What failed, where, how long did it take, and which fallback was used?

without reproducing the bug manually.

---

# 15. Testing strategy

## Unit tests

Use for:

- tool-call accumulation,
- playback verification logic,
- local capability parsing,
- history repair/sanitization,
- state machines,
- config parsing.

## Integration tests

Use fake external adapters for:

```text
AgentLoop -> tool call -> permission -> tool -> result -> next iteration
```

## Platform tests

macOS-specific tests should be marked separately.

Examples:

```text
@pytest.mark.macos
@pytest.mark.requires_ytm
@pytest.mark.requires_ollama
```

CI should not fail merely because CI does not have a logged-in YTM account.

## Regression rule

Every confirmed production bug should get a regression test before or with
the fix.

---

# 16. Coding standards

Backend:

- Python type hints for public APIs.
- Prefer dataclasses/Pydantic models for cross-module data.
- No new module-level mutable global unless there is a strong lifecycle reason.
- No blocking subprocess/network call directly on the event loop.
- Avoid `except Exception: pass` in critical paths.
- External calls require timeouts.
- Domain logic should be testable without macOS/Chrome/Ollama.

Frontend:

- TypeScript strictness should remain enabled.
- Network state and selected UI state must be distinct.
- Avoid duplicated backend state.
- Event handlers should be deterministic and session-scoped.
- Do not swallow failures that change user-visible behavior.

---

# 17. Adding a new tool

Checklist:

1. Define domain responsibility.
2. Add or reuse a domain service.
3. Define a small JSON schema.
4. Define a typed result.
5. Register the tool.
6. Assign permission policy.
7. Add executor timeout.
8. Unit-test validation/result behavior.
9. Integration-test one successful agent call.
10. Document it in the system prompt only if the model genuinely needs the
   capability described there.

Do not add a new 150-line function to a central registry module.

---

# 18. Adding a new LLM provider

Provider adapter must implement:

- streaming text,
- reasoning if available,
- finish reason,
- canonical tool call assembly,
- error normalization,
- cancellation,
- timeout.

The agent loop should not be changed for normal provider-specific quirks.

---

# 19. Adding a new media adapter

Adapter contract should expose approximately:

```python
async def health() -> Health
async def get_state() -> PlaybackState
async def play_query(query: str) -> PlaybackState
async def pause() -> PlaybackState
async def resume() -> PlaybackState
async def next() -> PlaybackState
async def previous() -> PlaybackState
async def close() -> None
```

The domain service owns the canonical result and verification boundary. The
current YT Music service deliberately has no automatic desktop or normal
YouTube fallback.

---

# 20. Development workflow

Before coding:

```bash
git status
git pull --ff-only
```

Work on a feature/fix branch.

Recommended validation:

```bash
pytest
ruff check .
ruff format --check .
```

Frontend:

```bash
cd web-ui
npm ci
npm run typecheck
npm run build
```

Run targeted tests during implementation, then the full suite before merge.

---

# 21. Commit discipline

Prefer small, reviewable commits.

Good:

```text
fix media next verification
unify local tool call assembly
add local model readiness state
split media tools into service
```

Avoid:

```text
fix everything
major refactor
updates
```

Each stabilization phase in `PRODUCTION-READINESS-PLAN.md` should ideally be
one PR or a small set of tightly related PRs.

---

# 22. Release checklist

Before calling a build stable:

- Python tests pass.
- Ruff passes.
- TypeScript typecheck passes.
- Frontend production build succeeds.
- `python -m jarvis doctor` gives expected output.
- Clean setup was tested.
- Existing `.env` survives setup unchanged.
- Cloud chat tested.
- One tool call tested.
- Local model tested if available.
- YT Music play/pause/next/previous manually tested on macOS.
- PTT manually tested.
- Server shutdown leaves no orphan Playwright/process resources.
- Version is consistent across backend/UI/status endpoints.

---

# 23. Debugging playbook

## “Local model does not use tools”

Check:

1. Is model actually `READY`?
2. What is its stored capability?
3. Did capability probe observe a real tool call?
4. Did the provider stream fragmented tool calls?
5. Was the tool schema actually sent?
6. Was history sanitized if `notools`?
7. Did backend silently fall back to cloud?

## “YT Music says success but action did not happen”

Check:

1. Which adapter owned the action?
2. State before command.
3. Command sent.
4. State after command.
5. Verification result.
6. Was a toggle command incorrectly used?
7. Did fallback occur?
8. Is world-state reading the same media service?

## “Selected local model answered with cloud”

Check:

1. Did UI set current model before load completed?
2. `RUNNER.is_ready(model_id)`.
3. `model_fallback` BUS event.
4. Ollama `/api/ps`.
5. Load error in runner state.

## “Clean install has broken TTS”

Check:

1. Effective configured backend.
2. Dependency installed for that backend.
3. `python -m jarvis doctor`.
4. `.env.example` vs Python defaults.

---

# 24. Documentation ownership

Use these documents for different purposes:

### `README.md`

User-facing installation and product overview.

### `DEVELOPER-GUIDE.md`

Long-lived engineering architecture and development standards.

### `PRODUCTION-READINESS-PLAN.md`

Temporary executable stabilization roadmap.

When the stabilization plan is complete, archive or remove it rather than
turning it into another historical architecture document.

---

# 25. Core engineering rule

When choosing between:

```text
more fallback code
```

and:

```text
clear ownership + verified state
```

prefer clear ownership and verified state.

JARVIS will become easier to extend when every domain has one authority:

```text
sessions       -> Session service/repository
models         -> Model router/runner
tools          -> Tool executor
media          -> Media service
audio          -> Audio services
configuration  -> Config layer
permissions    -> Permission store
```

That is the foundation for sustainable future growth.
