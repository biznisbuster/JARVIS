# Jarvis — napomene za Kilo agenta

## Stack

- **Backend**: Python 3.9, FastAPI, uvicorn, httpx (async).
- **Audio**: `faster-whisper` (STT, CPU/CUDA) i `mlx-whisper` (Apple Silicon
  Metal GPU, `JARVIS_STT_BACKEND=mlx_whisper`). TTS: `piper-tts` (default, fast
  ONNX) i `Coqui XTTSv2` (`pip install TTS`, `JARVIS_TTS_BACKEND=xtts` —
  prirodan glas, voice cloning, MPS/CUDA). `sounddevice` za mic (treba
  `portaudio` iz brew-a), macOS `say` Piper fallback.
- **LLM**: OpenAI-compatible streaming klijent ka
  `https://api.minimax.io/v1/chat/completions` (MiniMax Token Plan, model
  `MiniMax-M3`, `thinking: disabled`). Ključ (`sk-cp-…`) u `.env`
  (`JARVIS_MINIMAX_API_KEY`). Fallback provider: `bailian` (QwenCloud, ključ iz
  `~/.config/kilo/kilo.jsonc`), prebaci sa `JARVIS_PROVIDER=bailian`.
- **Tools**: Python funkcije registrovane u `jarvis/agent/tools.py`,
  OpenAI-compatible JSON schema.
- **Permissions**: `jarvis/permissions.py` — `allow / ask / deny` po toolu,
  trajno u `config/permissions.json`, hot-reload.
- **Kilo CLI**: integracija preko `kilo run --auto` sa strožim profilom
  (`config/kilo-jarvis.jsonc`) koji je allowlista.
- **Frontend**: vanilla HTML/CSS/JS u `web/`, nema build stepe, služi ga
  FastAPI kao statiku.

## Konvencije

- **Ne dodaj komentare u kodu** osim ako već postoje (docstring na modulima
  je OK).
- **Async** je pravilo za IO: `httpx`, `asyncio.create_subprocess_exec`,
  `await tool.execute(args)`. `subprocess.run` koristi samo u sync helperima
  za AppleScript / clipboard / `say` (kratki pozivi).
- **Event bus** (`jarvis/bus.py`) je jedini kanal između backend-a i UI-ja.
  Ne piši direktno u WebSocket — publikuj event, WS ga prosleđuje.
- **Permission gate**: svaki tool koji nije čist read prolazi kroz
  `PermissionStore.check()` pre izvršenja. Ako dodaješ novi tool, dodaj i
  podrazumevanu politiku u `config/permissions.json`.
- **Konfiguracija**: nikad ne hardcoduj API ključeve. Sve ide kroz env /
  `~/.config/kilo/kilo.jsonc`.

## Ključne datoteke

- `jarvis/llm.py:1` — streaming OpenAI-compat klijent; akumulira delte
  (`content` + `tool_calls`). MiniMax šalje tool_calls inkrementalno, pa u
  zadnjem chunku ceo call ponovo — `_absorb_tool_delta` koristi suffix-dedup.
  `stream_clean` skida `thinking` blok; `_collapse_double` spaja duple odgovore.
- `jarvis/agent/loop.py:38` — `run_turn`: drži se `max_iterations=8`; ako
  dodaješ nove grane, zadrži `assistant_msg` obavezan čak i za prazan
  content (LLM ponekad vrati samo tool_calls).
- `jarvis/agent/tools.py:1` — registar alata; svaki tool ima opis, JSON
  schema i async `execute(args) -> str` (string = JSON ili čitljiv tekst).
- `jarvis/permissions.py:78` — `PermissionStore._prompt_user`: baci
  `asyncio.TimeoutError` posle 5 min i automatski odbij.
- `jarvis/app.py:1` — FastAPI. Permission store je singleton modula
  (`app.permission_store`) jer ga `tools.py` lazy-importuje.

## Pokretanje

- Setup: `./scripts/setup.sh` (pravi venv, instalira deps + Kilo CLI).
- Start: `./scripts/start.sh` (aktivira venv, pokreće `python -m jarvis serve`).
- Port: `JARVIS_PORT` (default 7777).
- Menu bar: `./scripts/menubar.sh` (opciono, koristi `rumps`).

## Zabranjeno

- Ne menjaj `config/kilo-jarvis.jsonc` da bi bio labaviji — to je **bezbednosni
  profil**. Ako treba nova komanda, dodaj je sa `ask` politikom.
- Ne hardcoduj API ključ iz `kilo.jsonc` — čitaj runtime.
- Ne pravi sync IO u FastAPI handlerima osim za kratke AppleScript pozive.
- Ne pravi build pipeline za `web/` — UI je vanilla, bez bundlera.