# Jarvis — lični AI asistent

Desktop-orijentisan lični asistent koji razgovara na srpskom, ima lokalni audio
pipeline (Whisper STT + Piper TTS) i koristi **Minimax token plan** (Bailian,
OpenAI-compatible) kao LLM. Za kod i terminal poslove delegira **Kilo Code
CLI** sa strožim profilom dozvola. Sve se kontroliše iz lokalnog kontrolnog
panela.

## Status i bitna napomena o modelu

Koristiš **MiniMax Token Plan** (subscription key `sk-cp-…`) ka
`https://api.minimax.io/v1` sa modelom `MiniMax-M3` (OpenAI-compatible).
To je **international** MiniMax endpoint — kineski je `api.minimaxi.com` (druga
stvar, 2049 invalid api key). Podrazumevani model je `MiniMax-M3`, a `thinking`
je `disabled` radi čistih, brzih odgovora (MiniMax-M3 sa `adaptive` povremeno
ponovi ceo odgovor; client ih automatski spaja).

Alternativa: `bailian` (QwenCloud, tvoj postojeći nalog u `kilo.jsonc`) — prebaci
sa `JARVIS_PROVIDER=bailian` u `.env`.

## Glas

Tri TTS backenda:

| Backend | Kvalitet | Latencija | Offline | Instalacija |
|---|---|---|---|---|
| `edge` | ★★★★★ (prirodan, MS neural) | niska | ❌ (internet) | `pip install edge-tts` |
| `piper` | ★★ (robotski) | niska | ✓ | uključena u `requirements.txt` |
| `xtts` (Coqui XTTSv2) | ★★★★★ (engleski) | srednja | ✓ | `pip install TTS` |

`edge` je default — najbolji srpski glas (`sr-RS-NicholasNeural`,
`sr-RS-SophieNeural`), besplatan, treba internet. `piper` stoji kao fallback
(i potpuno offline opcija). Upozorenje: **XTTSv2 ne podržava srpski** (samo 17
jezika) — koristi ga samo za engleski. Backend se bira sa `JARVIS_TTS_BACKEND`.

## Whisper (STT) na GPU

`JARVIS_STT_BACKEND=mlx_whisper` (default) koristi Apple MLX + Metal — mnogo
brže na M-serija Mac-ovima. Zahteva `pip install mlx-whisper` i Python 3.10+.
Failsafe: `faster_whisper` (CPU/CUDA) — `JARVIS_STT_BACKEND=faster_whisper`.

## Browser / YouTube

Alati `open_url(url, browser)` i `play_youtube(query)` koriste Playwright
(`pip install playwright && python -m playwright install chromium`) da
otvore Chrome, ukucaju u YouTube pretragu i puste prvi rezultat. Pitanje
za `ask` dozvolu pre izvršenja.

## YouTube Music

YT Music koristi namensku, vidljivu persistent browser sesiju u
`~/.jarvis/ytm_profile`. Na novom uređaju otvori tab *Konekcije* i klikni
*Poveži YouTube Music*; prijava se obavlja direktno na Google/YT Music stranici.
JARVIS ne prima, ne upisuje i ne čuva Google lozinku. Ista browser sesija se
zatim koristi za pretragu, reprodukciju i DOM verifikaciju pause/resume/next/
previous akcija. Ako sesija istekne, status prelazi u *Potrebna prijava*.

## Šta imaš odmah

- 🧠 **LLM** — `MiniMax-M3` (MiniMax Token Plan, `https://api.minimax.io/v1`).
  Ključ `sk-cp-…` u `.env` (`JARVIS_MINIMAX_API_KEY`); opcioni fallback je
  `bailian` (QwenCloud, iz `~/.config/kilo/kilo.jsonc`).
- 🎙 **STT** — `faster-whisper` lokalno (srpski), default model
  `large-v3-turbo` (na Apple Silicon int8 radi realtime).
- 🔊 **TTS** — Piper (srpski glas `sr_RS-serbski-medium`), fallback na macOS
  `say`.
- 🛠 **Alati** — vreme, Apple Reminders (kreiranje + lista), Apple Calendar
  (danas), `open -a`, web pretraga (DuckDuckGo), clipboard, system volume,
  Kilo CLI za kod/terminal.
- 🔐 **Dozvole** — svaki tool ima `allow / ask / deny` (UI u tabu *Dozvole*),
  hot-reload, pamćenje po toolu, "zapamti" checkbox u approval modalu.
- 🖥 **Kontrolni panel** — single-page UI na `http://127.0.0.1:7777/`, živi
  WebSocket event stream, push-to-talk preko mikrofona ili globalnog
  hotkey-a (`⌘⌥ Space`).
- 📋 **Menu bar** — `scripts/menubar.sh` (opciono, zahteva `rumps`).

## Brzi start

```bash
cd /Users/marko/Documents/1-Projects/Jarvis

# 1) bootstrap (Python 3.11+ venv, deps, Kilo CLI)
./scripts/setup.sh

# 2) start server + otvori UI
./scripts/start.sh
# → otvara se http://127.0.0.1:7777/

# 3) (opciono) teški ML dodaci (XTTSv2, mlx-whisper, playwright)
.venv/bin/python scripts/ensure_optional.py

# 4) (opciono) menu bar
./scripts/menubar.sh
```

Prvi model koji se skida (preko HF mirror) zavisi od backend-a:
`jarvis_tts_backend=piper` → srpski Piper glas (~60 MB);
`jarvis_stt_backend=mlx_whisper` → large-v3-turbo (~1.5 GB);
`jarvis_tts_backend=xtts` → XTTSv2 (~2 GB). Sve kešira u `~/.cache/`.

## Struktura

```
Jarvis/
├── bin/                      # CLI wrapper + menubar skripta
├── config/
│   ├── permissions.json      # per-tool politika (živo iz UI-ja)
│   └── kilo-jarvis.jsonc     # strožiji profil za kilo CLI (allowlist)
├── jarvis/
│   ├── app.py                # FastAPI + WebSocket + REST
│   ├── agent/
│   │   ├── loop.py           # tool-calling petlja
│   │   ├── tools.py          # definicije + implementacije alata
│   │   ├── kilo_bridge.py    # poziv `kilo run --auto`
│   │   └── prompts.py        # sistemski prompt (srpski)
│   ├── audio/
│   │   ├── stt.py            # faster-whisper
│   │   ├── tts.py            # piper + say fallback
│   │   └── recorder.py       # mikrofon + VAD
│   ├── llm.py                # OpenAI-compat streaming + tool calls
│   ├── permissions.py        # permission engine
│   ├── bus.py                # in-process event bus (→ WebSocket)
│   ├── config.py             # loader (kilo.jsonc + .env)
│   └── __main__.py           # `python -m jarvis serve|ui|doctor`
├── scripts/
│   ├── setup.sh start.sh restart.sh doctor.sh menubar.sh
├── web/
│   ├── index.html  app.js  styles.css
├── .env.example
├── AGENTS.md                  # napomene za Kilo kad radi na projektu
└── requirements.txt
```

## Podešavanje

Sve ide kroz `.env` (prepiši iz `.env.example`) ili kroz kontrolni panel
tab *Konekcije* (prikazuje šta je učitano). Najbitnije:

| Var | Svrha |
|---|---|
| `JARVIS_PROVIDER` | `minimax` (default) ili `bailian`. |
| `JARVIS_MINIMAX_API_KEY` | MiniMax Token Plan subscription key (`sk-cp-…`). |
| `JARVIS_MINIMAX_MODEL` | `MiniMax-M3`, `MiniMax-M2.7`, `MiniMax-M2.7-highspeed`… |
| `JARVIS_MINIMAX_THINKING` | `disabled` (čisto/brzo) ili `adaptive` (razmišlja pa odgovori). |
| `JARVIS_WHISPER_MODEL` | `large-v3-turbo`, `large-v3`, `medium`, `small`. |
| `JARVIS_PIPER_VOICE` | Ime Piper ONNX glasa (srpski: `sr_RS-serbski_institut-medium`). |
| `JARVIS_KILO_BIN` | Putanja do `kilo` binarija. |
| `JARVIS_KILO_AUTO` | `true` = koristi `kilo run --auto` (allowlist). |
| `JARVIS_TTS_OUTPUT` | `ui` = browser reprodukuje audio; `say` = server igra afplay. |
| `JARVIS_DEFAULT_POLICY` | `allow` / `ask` / `deny` za nove alate. |

Napomena: env varijable `JARVIS_MINIMAX_BASE_URL` i `JARVIS_MINIMAX_API_KEY`
prepisuju ono što se čita iz `kilo.jsonc`. Ostavi prazno da koristiš svoj
postojeći setup.

## Dozvole i bezbednost

- Svaki tool se izvršava tek posle `PermissionStore.check()`.
- Ako je politika `ask`, UI prikazuje modal sa argumentima i dugmadima
  *Dozvoli / Odbij* + checkbox *Zapamti*.
- Kilo CLI se uvek zove sa `kilo run --auto` i `KILO_CONFIG=./config/kilo-jarvis.jsonc`.
  Taj profil je allowlista: `rm`, `sudo`, `shutdown`, `defaults` su `deny`,
  `git status*` / `ls *` su `allow`, ostalo je `ask`. Edituj u UI-ju (tab
  Dozvole → alati iz Kilo-a se tretiraju kao `kilo_run`) ili ručno.
- Permission fajl: `config/permissions.json`. UI ga edituje u hodu.

## API

| Endpoint | Svrha |
|---|---|
| `POST /api/chat` | `{text, session_id?, model?}` → `{session_id}` |
| `GET  /api/sessions` | Lista sesija |
| `GET  /api/sessions/{id}` | Puna istorija (messages) |
| `POST /api/sessions/{id}/reset` | Briše istoriju |
| `GET  /api/permissions` | Snapshot dozvola |
| `PUT  /api/permissions` | Izmena default/tools |
| `GET  /api/permissions/pending` | Trenutno otvoreni zahtevi |
| `POST /api/permissions/resolve` | `{request_id, action, remember}` |
| `GET  /api/connections` | Status svih konekcija |
| `GET  /api/ytm/connection` | Bezbedan status YT Music browser konekcije |
| `POST /api/ytm/connect` | Otvori namenski headed YT Music browser za ručnu prijavu |
| `POST /api/audio/stt` | multipart `audio` → `{text}` |
| `POST /api/audio/tts` | `{text}` → WAV fajl (TTS) |
| `WS   /ws` | Živi event stream |

## Pokretanje bez UI-ja (CLI)

```bash
./scripts/start.sh           # server + UI
./scripts/doctor.sh          # status konekcija
./scripts/menubar.sh         # macOS tray app
```

CLI sub-komande:

```bash
python -m jarvis serve [--reload] [--no-browser]
python -m jarvis ui
python -m jarvis doctor
```

## Dalje (TODO)

- 🎙 Kroz UI: snimanje dugmad → prepoznavanje govora → automatski chat.
- 🤖 ACP klijent prema `kilo acp` za žive sesije sa approval promptima u UI-ju.
- 🔌 MCP konekcije (Google Calendar, GitHub, Notion…) direktno u Jarvis.
- 📦 Whisper model cache pre-build skripta (da se prvi start ne blokira).
- 🧠 Dnevni digest: ujutru pročita kalendar + podsetnike + weather.
- 🧪 Pytest za permission engine + LLM delta parser.

Vidi i `AGENTS.md` za napomene namenjene Kilo agentu kad bude radio na ovom
projektu.
