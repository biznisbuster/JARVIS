# JARVIS Developer Guide

JARVIS is a macOS personal assistant with a FastAPI backend, React/Vite frontend, cloud and Ollama model support, voice input/output, and application tools.

## Architecture principles

Keep the agent loop focused on conversation orchestration. Provider adapters normalize model streams. A central tool executor handles tool dispatch. Domain services own business behavior, and platform adapters own browser or macOS implementation details.

Media should have one authoritative service with separate YouTube Music web and desktop adapters. Local model readiness should be explicit, and local and cloud model streams should produce the same internal tool-call format.

## Main areas

- `jarvis/app.py`: HTTP and WebSocket application boundary.
- `jarvis/agent/loop.py`: sessions and turns.
- `jarvis/llm.py`: model streaming boundary.
- `jarvis/local_models.py`: Ollama lifecycle.
- `jarvis/agent/tools.py`: current tool implementations and registry; this should be split by domain during stabilization.
- `jarvis/media/`: media integration.
- `jarvis/audio/`: speech, transcription and audio focus.
- `web-ui/`: React/Vite user interface.

## Development rules

Prefer one source of truth for each domain. Verify side effects when possible. Keep provider-specific behavior out of the agent loop. Do not execute incomplete streamed tool-call fragments. Treat model selection and model readiness as separate states. Add regression coverage for confirmed bugs. Keep external integrations bounded and recoverable. Keep documentation synchronized with runtime behavior.

## Validation

Backend changes should pass the Python test and lint suite. Frontend changes should pass TypeScript checking and the production build. Platform-specific media and voice behavior should also be manually verified on macOS.

Use `PRODUCTION-READINESS-PLAN.md` for the current phased stabilization program. The downloadable full edition of this guide contains the expanded runtime flows, service contracts, extension checklists, debugging playbook and release checklist.
