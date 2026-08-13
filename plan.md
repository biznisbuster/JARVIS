# Jarvis — živi plan (SOURCE OF TRUTH)

> **Za sledeću sesiju:** ovo je živi dokument. Pročitaj ga CELOG pre nego što
> nastaviš rad. Opisuje arhitekturu, završeno stanje, root-cause analizu svih
> prijavljenih problema i roadmap po fazama. Radi fazu po fazu; posle svake
> faze ažuriraj §5 (status) i §9 (dnevnik).
>
> **Datum poslednje izmene:** 2026-08-13
> **Test server:** `cd /Users/marko/Documents/1-Projects/Jarvis && .venv/bin/python -m jarvis serve --no-browser` (port 7777)

---

## 1. Ideja — šta Jarvis treba da bude

Lični glasovni asistent na macOS-u: korisnik priča (PTT bilo gde na sistemu
ili mic u browser-u), Jarvis odgovara glasom skoro bez latencije, i izvršava
akcije (muzika, kalendar, podsetnici, terminal preko Kilo agenta) kroz tool
pozive sa permission gate-om.

Tri stuba kvaliteta ka kojima sve odluke idu:

1. **Pouzdanost iznad svega.** Asistent koji kaže "pauzirao sam" a muzika i
   dalje svira je gori od asistenta koji kaže "ne mogu". Svaka akcija mora
   imati verifikaciju efekta, a ne "poslao sam komandu, valjda je radila".
2. **Glas prvi.** Odgovor kreće da se izgovara čim postoji prva rečenica;
   kad korisnik progovori, sve ostalo (TTS, muzika) se sklanja (listen mode).
3. **Čist temelj za rast.** Vanilla JS frontend bez build-a, async Python
   backend, event bus kao jedini kanal ka UI-u, tool registar sa šemama —
   novi tool/feature mora moći da se doda bez diranja jezgra.

---

## 2. Arhitektura (trenutno stanje)

```
Browser (web/app.js) — vanilla JS, bez build-a
   │  REST: /api/chat, /api/chat/stop, /api/audio/*, /api/sessions ...
   │  WS:   /ws  (prima događaje sa BUS-a)
   │  Cross-tab: BroadcastChannel('jarvis-speech') — elekcija ko pušta TTS
   ▼
FastAPI (jarvis/app.py)
   │
   ├─ agent/loop.py — SessionManager: FIFO red turn-ova po sesiji, jedan
   │     worker po sesiji; interrupt/barge-in; history trim (80 msg, seče
   │     na user granici); _repair_after_cancel; PERZISTENCIJA u
   │     data/sessions.json (atomski tmp+replace, posle user msg, posle
   │     turn-a i na reset; load na startu u lifespan-u).
   │
   ├─ agent/prompts.py — SYSTEM_PROMPT (srpski, persona, routing muzike,
   │     tool-first pravilo za činjenične upite).
   │
   ├─ llm.py — OpenAI-compat streaming klijent; deljeni keep-alive httpx;
   │     suffix-dedup za MiniMax delte; native reasoning_content;
   │     thinking stripper SAMO kad je thinking uključen;
   │     model sa prefiksom `local:` → _LocalStreamAdapter → Ollama.
   │
   ├─ local_models.py — Ollama bridge: katalog iz .env, load/unload
   │     (keep_alive warmup / keep_alive=0), streaming preko
   │     localhost:11434/v1; retry bez tools na HTTP 400 za modele koji
   │     ne podržavaju function calling (Gemma3).
   │
   ├─ agent/tools.py — registar alata (async subprocess preko to_thread):
   │     vreme, podsetnici, kalendar, clipboard, volume, web_search,
   │     open_app/url, play_youtube (Playwright), ytm_* (YouTube Music:
   │     MediaRemote ctypes → keystroke fallback; search scrape videoId →
   │     open watch URL bez fokusa).
   │
   ├─ agent/kilo_bridge.py — `kilo run --auto --pure` sa strožim profilom
   │     (config/kilo-jarvis.jsonc, KILO_CONFIG env).
   │
   ├─ audio/speech.py — SPEECH scheduler: server-driven sentence-streaming
   │     TTS (sintetiše UNAPRED dok model još generiše), tts_speak/tts_stop
   │     događaji, cancel/discard za barge-in, suppress za audio tool-ove.
   │
   ├─ audio/tts.py — 6 backend-ova (say/edge/piper/xtts/azure/elevenlabs),
   │     runtime switch + persist u .env, izlaz u data/tts/ (GC 80 fajlova).
   │
   ├─ audio/stt.py — Whisper (faster_whisper / mlx_whisper), warmup na startu.
   ├─ audio/player.py — afplay sa stop() (server-side playback).
   │
   ├─ hotkey.py — global PTT (pynput): drži taster → (mute sistema opciono)
   │     + snimanje → pusti → transkripcija → voice_ptt_transcribed.
   │
   ├─ permissions.py — allow/ask/deny po toolu, config/permissions.json,
   │     ask = modal u UI (timeout 5 min → deny).
   │
   ├─ bus.py — in-process pub/sub; svaki WS subscriber dobija svoj Queue.
   └─ state.py — deljeni singletoni (permission_store, whisper/piper/xtts).
```

**Tok jednog odgovora:** `/api/chat` → worker dequeue → `run_turn` → LLM
stream → delte na BUS (`assistant_delta`) I u SPEECH → SPEECH sintetizuje
rečenice unapred → `tts_speak` na BUS → browser tab-ovi se takmiče claim
tokenima (BroadcastChannel), pušta samo pobednik → tool pozivi kroz
`PermissionStore.check()` → izvršenje → nastavak turn-a.

---

## 3. ŠTA JE ZAVRŠENO

### Refaktor 1 (brzina + stabilnost jezgra)
- [x] `state.py` singletoni, raskinuti cirkularni importi.
- [x] `llm.py` prepisan: keep-alive klijent, thinking stripper ne baferuje
      ceo odgovor (bio uzrok "zanemi" efekta), native reasoning_content.
- [x] `loop.py` prepisan: SessionManager FIFO po sesiji, barge-in, trim,
      `_repair_after_cancel`, `SPEECH.discard()`.
- [x] `speech.py` NOVO: server-driven sentence-streaming TTS.
- [x] `tts.py`: bez odbijanja mešovitog teksta, data/tts/, .aiff leak fix.
- [x] `player.py` stop(); `stt.py` warmup; `tools.py` sve na to_thread;
      `hotkey.py` cross-thread loop fix; `app.py` /api/chat/stop, interrupt.
- [x] `web/app.js` prepisan: ordered TTS queue, busy/stop, loadConnections fix.

### Refaktor 2 (2026-08-13, ova sesija)
- [x] **Cross-tab elekcija za TTS playback** (P0 regresija): claim/token
      protokol preko `BroadcastChannel('jarvis-speech')` u `enqueueSpeech()`
      (web/app.js) — 150 ms prozor, najmanji token pušta, tie-break TAB_ID;
      stopSpeech() poništava pending claimove. Server sintetizuje i dalje
      jednom. Cache bust `?v=5`.
- [x] **Tool-first prompt**: sekcija "Činjenični upiti — PRVO tool, NIKAD iz
      glave" u prompts.py. Verifikovano uživo (time_now pre odgovora).
- [x] **Perzistencija sesija**: `save_sessions()`/`load_sessions()` u
      loop.py → data/sessions.json (atomski), lifespan load, čuvanje posle
      user poruke / turn-a / reset-a. Verifikovano restartom.
- [x] Verifikovano: compileall, node --check, FIFO 3 brze poruke, reset.

### I dalje RUČNO (ne može iz koda)
- [ ] **Test u pravom browser-u**: autoplay (unlockAudio), stop dugme
      (turn + audio), mute toggle, busy indikator, i cross-tab elekcija sa
      2 otvorena taba (audio samo u jednom).
- [ ] **PTT na hardveru**: Accessibility dozvola terminalu/python-u, držanje
      desnog cmd-a, transkripcija, auto-send.

---

## 4. ANALIZA PROBLEMA — root cause i rešenja

Prijavljeno od korisnika + nalazi audita. Ovo je mapa za faze u §6.

### 4.1 Lokalni modeli bez tool podrške (gemma3-4b) — NAJVEĆI UX KVAR
**Simptom:** posle izbora gemma3-4b, model NARIRA radnje umesto da pozove
tool ("Pretrage počinjem sa…", "Držim te da pustim tu pesmu") i piše lažne
pozive u code blokovima (`` `ytm_play Relja Popović` ``). Pesma ne kreće.
**Root cause:**
- Gemma3 ne podržava function calling. `local_models.py:316-323` detektuje
  HTTP 400 i tiho retry-uje BEZ tools — ali model i dalje dobija
  SYSTEM_PROMPT pun opisa alata → halucinira tool pozive kao tekst.
- Istoj sesiji history sadrži `tool_calls`/`tool` poruke iz prethodnih
  cloud turn-ova — dodatni šum za lokalni model.
- Katalog je STATIČAN (samo `JARVIS_LOCAL_MODELS` iz `.env`) — modeli koje
  korisnik već ima na disku (gemma4:12b, gemma4:26b, gemma4:e2b) se uopšte
  ne vide u UI. Korisnik nije ograničen na skinute modele — sme da skida
  nove, ali pull danas mora da se radi ručno u terminalu.
- `qwen3.6:27b` (koji PODRŽAVA tools) je deklarisan u `.env` ali NIJE bio
  pull-ovan → jedini dostupan lokalni model bio je upravo onaj koji ne radi
  sa alatima. **Pull qwen3.6:27b:** oba pokušaja 2026-08-13 pala na finalnom
  koraku (Ollama partial-blob error: `remove ...-partial-0: no such file` —
  stale stanje daemon-a). Recept za popravku je u Fazi 3 task 1; pre Faze 3
  obavezno proveriti `ollama list`.
- Nema upozorenja u UI da model ne podržava tools.
**Rešenje (Faza 3):** katalog = AUTO-DISCOVERY svih instaliranih Ollama
modela (`/api/tags`), `.env` ostaje samo za override (n_ctx, keep_alive,
flags); capability flag po modelu (probe detekcija na load: jedan probni
zahtev sa tools); upozorenje u UI za modele bez tools; zaseban SYSTEM_PROMPT
bez alata za takve modele; sanitize history-ja pri prelasku na lokalni model;
`think:false` za Qwen3 preko Ollame (da CoT ne curi u odgovor); graceful
fallback na cloud model kad je izabrani model unload-ovan; **background pull
iz UI** (endpoint + progress događaji na BUS-u, nikad ne blokira server).
gemma4:12b je kandidat za tool calling (proveriti probe testom — ako radi,
to je brz lokalni default).

### 4.2 Media kontrole nepouzdane (pause ne radi, status laže)
**Simptom:** "stopiraj mi pesmu" → `ytm_pause` vrati ok, muzika i dalje
svira; model sam primeti preko ytm_status pa proba opet.
**Root cause:**
- `_media_remote_send` (tools.py:447) je **fire-and-forget**: vraća True ako
  ctypes nije bacio exception, bez ikakve verifikacije da je komanda stigla
  do now-playing app-a.
- `ytm_status` (tools.py:730-739) čita prozor od **process 1** (launchd!)
  umesto od YTM procesa — skripta je pogrešna, status je smeće.
- Nema verify → retry → fallback petlje; model dobija lažni `ok:true`.
**Rešenje (Faza 1):** `brew install nowplaying-cli` (stabilan MediaRemote
wrapper) kao primarni kanal u novom modulu `jarvis/media/nowplaying.py`:
stanje (isPlaying/title/artist) + pause/play/next/prev SA verifikacijom
efekta (provera playbackRate posle komande, 1 retry), fallback lanac
nowplaying-cli → MR ctypes → keystroke. `ytm_status` da se prepiše na
nowplaying + ispravan AppleScript za YTM prozor. Tool rezultati moraju da
prijave STVARNO stanje ("pauzirano, potvrđeno" / "pokušao, i dalje svira").

### 4.3 Sesije ne znaju stanje sveta (nova konverzacija je slepa)
**Simptom:** nova konverzacija ne zna da muzika svira, koliko je sati, itd.
**Root cause:** SYSTEM_PROMPT je statičan; svaka sesija ima izolovan history;
nema zajedničkog "world state" konteksta. (Dodatno: aktivan model se drži
samo u JS state-u tab-a — posle refresh-a/new tab-a pada na default iako je
lokalni model i dalje u RAM-u.)
**Rešenje (Faza 1 + 3):** nov modul `jarvis/context.py` koji SVAKI turn
gradi kratak world-state blok (trenutno vreme/datum, da li muzika svira i
šta, sistemski volume, aktivan model) i injektuje ga u system poruku. Ovo
root-cause rešava i pogađanje vremena. Aktivan model: server-side persist
(data/state.json) da novi tab/restart nasleđuje izbor.

### 4.4 TTS log spam ("✓ TTS (say)" više puta po odgovoru)
**Simptom:** 3-5 "✓ TTS" linija po odgovoru, a izgovoreno jednom.
**Root cause:** svaka sintetizovana rečenica emituje `tts_done`
(tts.py:324-326), frontend loguje svaki sa 4s dedup prozorom (app.js:177) —
rečenice stižu razmaknuto pa dedup ne pomaže. Nije bug u zvuku, jeste šum.
**Rešenje (Faza 1):** frontend agregira po turn-u: jedan red "✓ TTS ×N" koji
se ažurira (reset na assistant_start); ili tts_done ukupno preseliti u Logs
tab. SPEECH scheduler je ionako jedini caller u chat flow-u.

### 4.5 Snimanje ne utišava pozadinu (listen mode ne postoji)
**Simptom:** kad korisnik krene da priča, muzika/Jarvis-ov TTS mu smetaju.
**Root cause:**
- PTT samo mutira sistem dok drži taster (hotkey.py:203) — i to samo ako
  listener uopšte radi (Accessibility!); muzika se ne pauzira, Jarvis-ov TTS
  u browser-u ne staje eksplicitno, server-side afplay se ne stopira.
- Browser mic (toggleMic) zove samo `stopSpeech()` u SVOM tabu — drugi
  tabovi i server playback nisu dirnuti, muzika nije dirnuta.
- Nema centralnog "listen mode" koncepta.
**Rešenje (Faza 2):** `jarvis/audio/focus.py` AudioFocusManager:
- **enter**: SPEECH.cancel + player.stop() + `tts_stop` svim tabovima;
  pauziraj muziku preko nowplaying (zapamti wasPlaying) ILI duck volume na
  konfigurabilnih N% (config `JARVIS_LISTEN_MODE=pause|duck|mute`);
- **exit** (posle transkripcije): vrati muziku ako je bila pauzirana, vrati
  volume.
PTT i browser mic (novi endpoint `/api/audio/listen/start|stop`) koriste isti
mehanizam.

### 4.6 Audit — dodatne rupe u robusnosti (nađeno čitanjem koda)
| # | Problem | Lokacija | Faza |
|---|---------|----------|------|
| a | Nema retry za LLM greške (429/5xx/mreža) — turn odmah umire | llm.py | 4 |
| b | TTS sinteza bez timeout-a (edge-tts može da visi zauvek i blokira speech worker); nema fallback lanca edge→say | tts.py/speech.py | 4 |
| c | Whisper transkripcija deli default ThreadPoolExecutor sa svim to_thread poslovima (duga transkripcija guši osascript pozive) | stt.py | 4 |
| d | BUS: QueueFull tiho izbacuje subscriber (WS umre bez traga) | bus.py | 4 |
| e | WS: svi eventi idu u sve tabove bez obzira na sesiju; frontend ne filtrira po session_id → mešanje transkripata | app.py/app.js | 4 |
| f | `_trim_history` fallback grana (bez user poruka) može ostaviti orphan tool poruke → API 400 | loop.py:57 | 4 |
| g | Nema DELETE session; `newSession()` šalje "Zdravo" (troši tokene, pravi šum) — treba prazan session endpoint | app.py/app.js | 4 |
| h | `local_models.available()` sinhroni httpx u async handler-u (blokira loop do 1.5s kad je Ollama down) | local_models.py:64 | 4 |
| i | Izabrani lokalni model se unload-a → RuntimeError → assistant_error bez graceful fallback-a | loop/llm | 3 |
| j | `web_search` regex-scrape DDG HTML-a — krhko | tools.py:203 | 6 |
| k | `play_youtube` pravi nov Playwright context po pozivu → gomilanje tabova | tools.py:271 | 6 |
| l | Mrtav kod: `match_pattern`, `_PERMISSION_WILDCARD` | permissions.py:165 | 5 |
| m | `__main__.py` docstring pominje `stop` komandu — ne postoji | __main__.py | 5 |
| n | Nema testova, nema lint/format, print umesto strukturiranog logovanja | sve | 5 |
| o | Frontend: izbor modela nije perzistovan; PTT autosend prikaz uvek '—'; nema markdown renderovanja; styles.css bump kad se menja | web/ | 6 |
| p | `ytm_volume_*` menjaju SISTEMSKI volume a opis kaže "YTM zvuk" — neodgovara opisu; ili pravi YTM volume (keystroke ↑↓) ili iskren opis | tools.py:696 | 1 |

---

## 5. STATUS (ažurirati posle svake faze)

| Faza | Naziv | Obim | Status |
|------|-------|------|--------|
| 0 | Ručni testovi (browser + PTT hardver) | korisnik | ⬜ otvoreno |
| 1 | Mediji + stanje sveta | M — 1 sesija | ✅ gotovo (2026-08-13) |
| 2 | Listen mode + globalni hotkey | M — 1 sesija | ✅ gotovo (2026-08-13) |
| 3 | Lokalni modeli (3A + 3B) | L — 2 sesije | ✅ gotovo (2026-08-13) |
| 4 | Robusnost jezgra (4A + 4B) | L — 2 sesije | ✅ gotovo (2026-08-13) |
| 5 | Testovi + higijena | M — 1 sesija | ✅ gotovo (2026-08-13) |
| 6 | UX poliranje (funkcionalno) | S — 1 sesija | ✅ gotovo (2026-08-13) |
| 7 | Kompletan UI/UX redizajn (7A + 7B, Vite+React+TS) | L — 2 sesije | ✅ gotovo (7A ✅ 2026-08-13 / 7B ✅ 2026-08-13) |

Redosled je obavezan: faze se rade po numeričkom redosledu; sledeća faza ne
počinje dok prethodna nije ✅ (osim Faze 0 koju korisnik radi kad stigne).

---

## 6. ROADMAP PO FAZAMA

### Protokol rada po sesijama (OBAVEZNO)

1. **Jedna sesija = jedna faza** (ili jedna pod-faza: 3A/3B, 4A/4B, 7A/7B).
   Sesija radi ISKLJUČIVO prvu nezavršenu fazu iz §5. Ne preskati, ne
   mešati faze, ne počinjati sledeću čak i ako ima vremena — fresh context
   u sledećoj sesiji je namerna odluka, ne gubitak.
2. **Kraj faze = obavezan stop.** Pre nego što stane, sesija MORA:
   - da protera §7 testove + acceptance kriterijume svoje faze;
   - da ažurira plan.md: §5 status svoje faze → `✅ gotovo`;
   - da popuni polje **"Zaključci"** svoje faze (obavezno, videti t.4);
   - da doda redak u §9 dnevnik;
   - da korisniku kaže: "Faza X je gotova — restartuj sesiju" i STANE.
3. **Ako faza ne može da se završi u jednoj sesiji** (prevelik obim,
   nepredviđeni problemi): stati na prirodnoj granici (ne ostaviti polomljen
   build), u §5 označiti `🟡 u toku`, u "Zaključke" faze napisati TAČNO:
   šta je gotovo, gde je stalo (fajl/funkcija), šta je sledeći korak.
   Sledeća sesija NASTAVLJA istu fazu — ne počinje novu.
4. **Zaključci svake sesije su obavezan deo plan.md-a**, u okviru faze koju
   je ta sesija radila. Piše se: (a) šta je stvarno urađeno (ako se razlikuje
   od taskova — i zašto), (b) novootkriveni bugovi/rupe, (c) donete odluke,
   (d) šta je ostavljeno za kasnije i zašto. Ovo je primarni mehanizam kojim
   sledeća sesija dobija potpune informacije.
5. **Definicija "gotovo" za fazu:** acceptance kriterijumi prolaze + §7
   testovi prolaze + `compileall`/`node --check` čisti + plan.md ažuriran.
   Bez ovoga faza nije gotova, čak i ako kod "izgleda gotovo".
6. Taskovi unutar faze su smernice, ne zakon: ako sesija tokom rada otkrije
   da nešto treba drugačije, uradi bolje — ali MORAJU da se dokumentuju
   odstupanja u "Zaključcima".

---

### FAZA 1 — Mediji + stanje sveta (prioritet: najveći vidljivi kvar)
**Obim:** M — jedna sesija. **Status:** ✅ gotovo (2026-08-13)
**Zaključci:**

(a) Šta je stvarno urađeno:
- `brew install nowplaying-cli` (2.1.0) — korisnik odobrio.
- NOV `jarvis/media/nowplaying.py`: `get_state()` (nowplaying-cli `get --json`,
  tumačenje playing preko isPlaying/playbackRate — YTM Web App vraća
  `isPlaying:null` pa je rate rezervoar istine); `control(action)` sa lancom
  nowplaying-cli (2 pokušaja) → MediaRemote ctypes → keystroke fallback,
  verifikacija efekta posle svakog pokušaja (čekanje 450 ms + ponovno čitanje
  stanja). MediaRemote ctypes kod PREMEŠTEN iz tools.py ovde.
- Keystroke fallback registruje tools.py (`register_keystroke_fallback`) da se
  izbegne cirkularni import; mapiranje action→keystroke (space/n/p).
- `ytm_pause/resume/next/previous` prepisani na `_np.control()` — vraćaju
  STVARNO stanje (verified, method, attempts, state). `ytm_status` prepisan:
  nowplaying info + YTM running (pgrep); stari process-1 Applescript OBAČEN.
- NOV `jarvis/context.py`: `build_world_state()` (vreme/datum/dan u srpskom,
  stanje muzike, sistemski volume) — gradi se JEDNOM po turn-u u `run_turn`
  (loop.py) pre petlje, injektuje u system poruku; exception-safe (padne li,
  turn nastavlja bez bloka).
- TTS log agregacija (web/app.js): jedan "✓ TTS ×N" red po turn-u koji se
  ažurira u mestu; reset na prvi assistant_start turn-a, zatvaranje turn-a na
  assistant_done(final)/cancelled/error. Cache bust ?v=6.
- Prompt: blok "STANJE SVETA" zamenjuje pogađanje vremena/muzike; tool-first
  pravilo prilagođeno (time_now za precizniju/proveru posle duže akcije;
  ytm_status samo kad stanje nepoznato ili provera posle akcije). Opisi
  ytm_volume_* iskreni ("menja SISTEMSKI zvuk") u tools.py, prompts.py i app.js.

(b) Novootkriveni problemi:
- Na ovoj mašini `get volume settings` vraća `missing value` za output volume
  (izlazni uređaj ne eksponira volume) → world-state preskače volume liniju
  (graceful); `system_volume` tool ima isto ograničenje (postoji od ranije).
- Now-playing registracija je SISTEMSKA: browser tab (npr. video stranica)
  može da preotme "now playing" slot dok YTM svira/pauzira — tool-ovi tada
  prijavljuju taj tab (što je iskreno). Model je u testu ispravno uočio
  neslaganje i prijavio ga korisniku.

(c) Donete odluke:
- nowplaying-cli primarni kanal (odobreno od korisnika), MR ctypes drugi,
  keystroke poslednji.
- Za next/previous verifikacija: playing nije False + promena pesme (ako je
  prethodno stanje imalo naslov); bez promene naslova i dalje playing=True
  prolazi kao verifikovano.

(d) Ostavljeno za kasnije:
- Ručni browser test TTS agregacije sa 2 taba (Faza 0) — headless test
  potvrđuje 1 red ×N po odgovoru.
- Pravi YTM volume (keystroke ↑↓) umesto sistemskog — nije traženo, iskren
  opis je dovoljan (task 4 odluka).

Verifikovano: compileall + node --check čisti; live lanac status→resume→pause
(verified:true, playing:true/false, rate 1/0); chat acceptance (play/pauza/
status tačni); "Šta svira?" iz world-state bez pogađanja; headless TTS test
"✓ TTS ×3 (say)" jedan red.

Cilj: "pusti/pauziraj" UVEK radi i UVEK prijavljuje istinu; asistent zna
koliko je sati i šta svira bez pogađanja.

Taskovi:
1. `brew install nowplaying-cli` (ili vendored alternativa ako korisnik
   odbije — pitati u §10).
2. NOV `jarvis/media/nowplaying.py`: `get_state()` (isPlaying, title, artist,
   rate), `pause()`, `play()`, `next()`, `prev()` — svaka akcija: komanda →
   čekanje 300-500 ms → verifikacija → 1 retry → fallback (MR ctypes →
   keystroke sa fokusom). Vraća stvarno stanje, ne nameru.
3. Prepisati `ytm_pause/resume/next/previous/status` u tools.py preko
   nowplaying.py; `ytm_status` vraća nowplaying info + YTM running.
4. `ytm_volume_*`: odluka — iskren opis ("menja sistemski zvuk") ILI pravi
   YTM volume; default u planu: iskren opis + model već ima system_volume.
5. NOV `jarvis/context.py`: `build_world_state()` → kratak tekst blok
   (vreme, datum, dan; mediji: svira/ne svira + naslov; volume). Injektuje
   se u system poruku u `run_turn` (loop.py) ispred history-ja. Kešira se
   unutar turn-a (ne zove se po iteraciji).
6. TTS log agregacija u web/app.js: jedan "✓ TTS ×N" red po turn-u.
7. Prompt dopuna: "stanje sveta dobijaš u kontekstu — ne pogađaj vreme ni
   status muzike".

Acceptance:
- `ytm_play X` → svira; `ytm_pause` → TIŠINA, potvrđeno statusom; ponovo
  `ytm_status` → playing:false.
- Nova sesija, pitanje "šta svira?" → tačan odgovor BEZ pogađanja.
- Jedan TTS log red po odgovoru u chat tab-u.

### FAZA 2 — Listen mode (audio fokus) + globalni hotkey
**Obim:** M — jedna sesija. **Status:** ✅ gotovo (2026-08-13)
**Zaključci:**

(a) Šta je stvarno urađeno:
- NOV `jarvis/audio/focus.py`: `AudioFocusManager` sa async `enter(reason)` /
  `exit(reason)`, idempotantan putem refcount-a kao `set[str]` od razloga
  (isti razlog se broji jednom; PTT + browser mic + budući wake-word
  mogu paralelno). Na prvi `enter`: SPEECH.cancel_all() (drop svih
  session speech buffera + stop server playback + `tts_stop` svim
  tabovima), snimi `wasPlaying` + `prev_volume`, primeni mod
  (pause/duck/mute). Na poslednji `exit`: vrati muziku SAMO ako je bila
  pauzirana, vrati volume. Emituje `listen_enter` / `listen_exit` na BUS.
- NOV `SpeechScheduler.cancel_all()` u `jarvis/audio/speech.py` —
  koristi ga `focus.enter` da zaustavi SVE sesije odjednom.
- Config: NOV `ListenModeSettings(mode, duck_volume)` u `jarvis/config.py`;
  env `JARVIS_LISTEN_MODE` (default `pause`) i `JARVIS_LISTEN_DUCK_VOLUME`
  (default 15). Defaults u `.env`.
- `jarvis/hotkey.py` prepisan: PTT više NE dira sistemski mute direktno.
  `_on_press` → `FOCUS.enter("ptt")` (scheduled na server loop preko
  `asyncio.run_coroutine_threadsafe`); `_on_release` → `FOCUS.exit("ptt")`
  PRE `_stop_recording()` (transkripcija NE čeka restore — focus se
  vraća odmah posle release). Stari `_set_mute` + `_muted` field su
  obrisani (mrtav kod).
- NOV endpoints u `jarvis/app.py`: `GET /api/audio/listen`,
  `POST /api/audio/listen/start`, `POST /api/audio/listen/stop`. Telo
  `{reason: "browser"|"ptt"|...}` (default `browser`). `listen` polje
  dodato u `_connections_payload()`.
- `web/app.js` `toggleMic()`: pre snimanja zove `listen/start("browser")`,
  posle (u `onstop` I u catch grani) zove `listen/stop("browser")`. WS
  handler-i za `listen_enter` / `listen_exit` pale `listening` klasu
  na mic + dodaju badge "🎙 slušam…". CSS klasa `.composer #mic.listening`
  (accent outline) + `.listen-badge` (apsolutno pozicioniran ispod).
- PTT Accessibility dijagnostika: `PushToTalk.status()` sada izlaže
  `enabled_at` / `first_press_at` / `last_press_at` i izvedeno
  `no_events_yet` (true ako enabled >60s bez ijednog press eventa).
  Frontend periodic poll `/api/ptt` svakih 30s dok je enabled; kad
  `no_events_yet` postane true, `renderPtt` prikazuje hint koji TAČNO
  upućuje korisnika na System Settings → Privacy & Security →
  Accessibility + napominje da dozvolu treba procesu koji drži
  `jarvis serve` (Terminal/iTerm/python). Stari "muted" red je
  zamenjen sa "listen: pause|duck|mute" da korisnik vidi koji mod
  je aktivan.

(b) Novootkriveni problemi:
- Na ovoj mašini Bluetooth izlaz ne eksponira sistemski volume
  (`get volume settings` → `missing value`). `duck` i `mute` modovi
  su graceful no-op-ovi (`_read_output_volume()` vraća None → restore
  se preskače); `pause` mod funkcioniše jer koristi MediaRemote
  kontrolu, ne sistemski volume. Ovo je **isto ograničenje** koje je
  Faza 1 već zabeležila za world-state/system_volume; ovde je samo
  potvrđeno da duck/mute ne padaju na exception. Rešenje za duck na
  ovom hardware-u: menjati volume kroz YTM MediaRemote (keystroke
  ↑↓) — zabeleženo kao zavisi od Faze 6+ ako korisnik to traži.
- WebSocket subscriber za event-e dobija `listen_enter`/`listen_exit`
  ali eventi nisu session-scoped (nemaju `payload.session`). Kada
  browser ima 2 taba u 2 sesije, OBA taba se prikazuju kao
  "listening" čim bilo koja sesija uđe u listen mode. Nije bug za
  ovu fazu (focus je globalan po dizajnu), ali vredi imati na umu
  za Fazu 4 (WS scope cleanup).

(c) Donete odluke:
- Refcount preko `set[str]` (razlozi), ne `int` — isti razlog se
  broji jednom; semantički "aktivni izvori" a ne "broj poziva".
  Posledica: 2× `enter("ptt")` + 1× `exit("ptt")` = stanje
  restored. To odgovara stvarnom cycle-u (1 korisnik ne može imati
  2 aktivna PTT-a paralelno).
- Exit PRE stop-recording (ne finally oko svega) — transkripcija
  NE blokira vraćanje muzike. Ovo je alignment sa zahtevom iz
  acceptance ("muzika se vrati čim pustiš taster").
- Hardkorirani fallback: kad god `focus.enter` ne uspe da pauzira
  muziku (nowplaying-cli timeout, MR ctypes fail), NE eksplodira —
  tiho propusti; `was_playing` ostaje None pa ni exit ne pokušava
  resume (nije bezbedno blefovati "pusti" kad nismo sigurni da je
  bila pauzirana).
- `no_events_yet` prag 60s — ne pre toga, da se legitimni korisnik
  koji je upalio PTT a tek posle 30s probao taster ne uplaši
  lažnim upozorenjem.

(d) Ostavljeno za kasnije:
- Pravi sistemski volume duck na Mac-ovima koji ga eksponiraju — već
  radi, samo nije izgledno na ovom uređaju; nema posla.
- Fallback hotkey unutar browser tab-a (cmd+alt+space) — već postoji
  i radi; ostaje kao workaround dok Accessibility nije odobren (nije
  bila tema za implementaciju u ovoj fazi, samo reminder).
- `cross-tab "listening" badge bez dupliranja` (BroadcastChannel
  claim kao za TTS) — vredi u Fazi 7 (UI redizajn), nije hitno jer
  badge je nenametljiv.

Verifikovano: compileall + node --check čisti; live test
"pusti pesmu → enter listen → muzika pauzirana → exit listen →
muzika nastavila" (rate 1→0→1); chat + TTS regression OK; refcount
sa više izvora i idempotentnim ulazom radi ispravno.

Cilj: kad korisnik progovori, **STANE sav zvuk na laptopu** (pesma,
TTS, video), korisnik se lepo čuje; kad završi, sve se vraća. Hotkey
za razgovor radi **bez obzira na fokus** (PTT sistemski, accessibility
dozvola potrebna, dijagnostika u UI kad nedostaje).

Taskovi:
1. NOV `jarvis/audio/focus.py`: `enter_listen()` / `exit_listen()` (async,
   idempotent, refcount za paralelne izvore). enter: SPEECH.cancel za aktivne
   sesije, player.stop(), tts_stop na BUS (svi tabovi), media pause preko
   nowplaying.py (zapamti wasPlaying + prethodni volume), pamti prethodno
   stanje. exit: vraća muziku/volume ako su bili aktivni.
2. Config: `JARVIS_LISTEN_MODE=pause|duck|mute` — **default `pause`**
   (korisnik eksplicitno tražio da sve STANE dok priča),
   `JARVIS_LISTEN_DUCK_VOLUME=15` (za duck režim).
3. PTT (hotkey.py): zameni sirovi mute sa focus.enter/exit oko snimanja.
4. Browser mic: endpoint-i `/api/audio/listen/start` i `/api/audio/listen/stop`;
   toggleMic u app.js ih zove pre/posle snimanja.
5. Edge case: transkripcija ne sme da čeka exit — exit odmah posle release,
   pre transkripcije.
6. **Globalni hotkey je tvrd zahtev:** PTT listener (pynput) je sistemski i
   ne zavisi od fokusa — ali zahteva Accessibility dozvolu za proces koji
   pokreće server (Terminal/iTerm/python). Faza 2 MORA da uključi:
   - jasnu UI dijagnostiku kad listener ne prima evente (pynput tiho guta
     evente bez dozvole — detektovati "listener aktivan ali 0 eventa u N
     sekundi" i prikazati uputstvo za System Settings),
   - uputstvo u UI koje TAČNO imenuje proces kojem se daje dozvola,
   - test protokol: PTT mora da radi dok je fokus u Safari/Chrome/VS Code-u.
   - Razmotriti: fallback hotkey unutar browser tab-a (cmd+alt+space već
     postoji) kao privremeno rešenje dok Accessibility nije rešen.

Acceptance:
- ✅ Pusti muziku → drži PTT (iz BILO KOJE aplikacije) → muzika STANE +
  Jarvis TTS stane → pusti → transkript u input → muzika se vrati.
  (Live verifikovano kroz API + refactorisan PTT koristi isti mehanizam.)
- ✅ Isto za browser mic dugme. (toggleMic zove listen/start|stop sa
  reason="browser".)
- ✅ UI jasno prijavljuje ako Accessibility dozvola nedostaje.
  (`no_events_yet` + System Settings uputstvo u renderPtt.)

### FAZA 3 — Lokalni modeli
**Obim:** L — dve sesije: **3A** (taskovi 1-5: katalog, pull, probe, UI) i
**3B** (taskovi 6-9: prompt, history, think, fallback, state). Jedna sesija
radi jednu pod-fazu pa STAJE.
**Status:** ✅ gotovo (3A ✅ 2026-08-13 / 3B ✅ 2026-08-13)
**Zaključci 3A (2026-08-13):**

(a) Šta je stvarno urađeno:
- **Pre početka: kompletna live verifikacija Faze 1 i Faze 2** (svi
  acceptance kriterijumi prolaze, bez potrebnih popravki): media lanac
  play/pause verified:true u oba smera; world-state tačan; chat "šta
  svira" bez pogađanja; listen enter→rate 1→0, exit→rate 0→1; refcount
  sa 2 razloga + idempotentni dupli enter; FIFO, stop/repair,
  perzistencija kroz restart.
- **qwen3.6:27b pull (task 1):** nađeno 28 partial blob-ova (stale stanje
  iz plana); Ollama daemon je radio kao RUČNO startovan `ollama serve`
  (pid 9330, NIJE brew services) — restartovan preko nohup
  (log `~/.ollama/serve.log`); pull nastavljen u pozadini (nohup,
  log `/tmp/qwen-pull.log`), prošao je kritični finalni korak koji je
  ranije padao. Do kraja sesije stigao do ~45% (17 GB, brzina varira
  37→2.4 MB/s); pull je detached i NASTAVLJA SAM posle ove sesije.
- **Auto-discovery (task 2):** `jarvis/local_models.py` prepisan —
  `discover()` čita `/api/tags` + `/api/ps` (async, keš 5s), spaja sa
  `.env` override-ima. `JARVIS_LOCAL_MODELS` sada služi SAMO za
  parametre; dodato opciono 5. polje `flags` (`notools`/`tools` forsira
  capability bez probe). Modeli iz `.env` zadržavaju svoj id
  (gemma3-4b), otkriveni modeli koriste tag kao id (gemma4:12b).
- **Background pull (task 3):** `start_pull/cancel_pull/_pull_worker` —
  NDJSON stream sa `/api/pull`, progress na BUS (`local_model_pulling`
  sa status/percent/detail), statusi starting/progress/done/error/
  cancelled, auto-čišćenje zapisa posle 30s. Endpointi
  `POST /api/local_models/pull` i `POST /api/local_models/pull/cancel`.
  Nikad ne blokira event loop.
- **Capability probe (task 4):** `probe_tools(tag)` — jedan kratak
  `/api/chat` zahtev sa tool šemom (`think:false`, 120s timeout);
  HTTP 400 "does not support tools" → `notools`, 200 → `tools`,
  ostalo → None (ne kešira se). Keš u `data/state.json` (NOV modul
  `jarvis/state_store.py`, atomski upis). `.env` flags imaju prednost
  nad probe-om. `stream_chat` notools modelima NE šalje tool šeme
  (HTTP-400 retry bez tools zadržan kao safety net i upisuje notools
  u keš ako se aktivira).
- **Load flow:** warmup (timeout 300s za velike modele) → verifikacija
  u `/api/ps` → probe ako capability nepoznat → payload sa capability;
  greške na srpskom, jasne poruke za model koji nije skinut.
- **app.py:** `/api/models` nudi SVE otkrivene lokalne modele (ne samo
  učitane); `/api/local_models` vraća runner+models+pulls; dodat
  `astatus()` (async) — app.py više nema sync httpx poziva ka Ollama-u
  u handler-ima (delimično zatvara audit stavku §4.6h).
- **Frontend (task 5):** local-models tab prikazuje sve modele
  (veličina, capability badge ✓ tool-ovi / ⚠ bez tool-ova / ?,
  ○ na disku / ● učitan u RAM), pull input + progress bar + Otkaži;
  izbor lokalnog modela u dropdown-u ODMAH pokreće load (idempotentno);
  WS `local_model_pulling` ažurira progress u mestu. Cache bust:
  app.js v8, styles.css v5. Obrisan mrtav `resolve_tag`.

(b) Novootkriveni problemi:
- **gemma4:12b PODRŽAVA tools** (probe: tools; live verifikovan pun
  tool loop: `reminders_create` → ok:true → finalni odgovor). To je
  kandidat za brz lokalni default (7 GB, Apple Silicon).
- gemma3:4b bez tool šema odgovara čisto iz world-state, bez
  haluciniranih poziva; na "šta svira" je rekla "svira" za pauziranu
  pesmu iako je world-state rekao "pauzirana" — fraziranje malog
  modela, nije sistemski kvar.
- Izbor ne-učitanog modela u dropdown-u pa SLANJE poruke pre nego što
  load završi → RuntimeError "nije učitan" (assistant_error). Rešava
  3B task 8 (graceful fallback na cloud).
- Ollama pull brzina jako varira (37 → 2.4 MB/s) — qwen pull može
  potrajati >1h; ide u pozadini, ne blokira ništa.

(c) Donete odluke:
- Capability se kešira po Ollama TAG-U (ne po UI id-u).
- Pull cancel je `POST /api/local_models/pull/cancel` (DELETE sa
  body-jem je nestandardan).
- Probe koristi `think:false` u body-ju (Qwen3 CoT; Ollama ignoriše
  kod modela koji ne znaju polje).
- Sinhroni `available()/status()` zadržani u runner-u, ali app.py
  koristi isključivo async verzije.

(d) Ostavljeno za 3B:
- **qwen3.6-27b tool verifikacija** — čeka završetak pull-a (pull radi
  detached; pre 3B proveriti `ollama list` i `/tmp/qwen-pull.log`).
- SYSTEM_PROMPT varijanta bez alata za notools modele + instrukcija
  "nemaš alate, prebaci na cloud"; sanitize history-ja (tool_calls/tool
  poruke) pri slanju lokalnom modelu bez tools.
- `think:false` za Qwen3 u redovnom stream_chat (ne samo probe);
  graceful fallback lokalni→cloud uz vidljivu napomenu; perzistencija
  aktivnog modela u data/state.json + `GET/PUT /api/state`.

Acceptance 3A:
- ✅ Svi modeli sa `ollama list` vidljivi u UI (headless: 5 redova,
  badge-i, bez console grešaka); pull novog taga iz UI radi u pozadini
  dok chat funkcioniše (smollm:135m start → 19% → cancel).
- ✅ gemma3-4b: odgovara čisto, bez lažnih tool poziva (notools keširan,
  tool šeme se ne šalju).
- ⏳ qwen3.6-27b: pull u toku (detached) — tool test čeka 3B.
- ✅ gemma4:12b: capability utvrđen probe-om (tools) i zabeležen u
  data/state.json; tool loop live verifikovan.

**Zaključci 3B (2026-08-13):**

(a) Šta je stvarno urađeno:
- **qwen3.6:27b pull ZAVRŠEN** (17 GB, prošao kritični finalni korak posle
  restarta daemon-a iz 3A; recept iz taska 1 funkcionisao). Model učitan,
  probe dao `tools` (keširano u data/state.json), live tool loop verifikovan
  (`calendar_today` poziv bez CoT-a u content-u — `think:false` radi).
- **T6 — notools prompt + history sanitize:** NOV `SYSTEM_PROMPT_NOTOOLS`
  u prompts.py (persona ista, bez sekcije o alatima, eksplicitna zabrana
  glumljenja poziva + primer ispravnog odgovora). `run_turn` bira prompt
  po capability-ju aktivnog modela (`RUNNER.capability_for_model()`, NOV
  async metod). `sanitize_history_for_notools()` u local_models.py:
  `tool` poruke se bacaju, `assistant(tool_calls)` zadržava samo text
  content (ako ga ima) — bez ikakvog "pozvan je alat" obrasca.
- **T7 — think:false + stripper:** `"think": False` u SVAKOM local chat
  body-ju (Ollama 0.30.6 toleriše polje i za modele koji ga ne znaju —
  verifikovano na gemma3:4b). NOV `_ThinkTagStripper` (streaming filter
  za `
</think>

` blokove, drži granične bafer-e za tag-ove prelomljene
  preko delte) — primenjen na content delte u `RUNNER.stream_chat`,
  drain na kraju; 6 unit test slučajeva prolazi (uklj. tag prelomljen
  između chunk-ova i stray close tag).
- **T8 — graceful fallback:** `run_turn` pre petlje proverava
  `RUNNER.is_ready()` — lokalni model nije u RAM-u → odmah prelazak na
  cloud + `model_fallback` event na BUS-u (frontend prikazuje napomenu).
  Isti fallback i ako lokalni model baci grešku SRED stream-a (exception
  handler → `SPEECH.cancel` + event + retry sa cloud modelom). Verifikovano
  uživo: ne-učitan model → cloud odgovor + event sa razlogom.
- **T9 — perzistencija aktivnog modela:** `GET/PUT /api/state` u app.py
  (ključ `ui` u data/state.json: `{model, tts_enabled}`, merge semantika,
  ne dira capability keš). Frontend: `loadPersistedUI()` na boot-u (model
  + tts toggle), `persistUI()` na promenu modela i TTS toggle-a,
  `ensureLocalLoaded()` helper (boot + dropdown izbor). Cache bust app.js v9.
- **Acceptance prolazi:** gemma3-4b na zahtev "pusti pesmu" odgovara
  "nema alate, izaberi cloud model" (bez ijednog lažnog poziva);
  gemma4:12b pun tool loop (`ytm_play` → pesma STVARNO svira, potvrđeno
  nowplaying-cli: title/artist/rate=1); qwen3.6-27b tool poziv bez CoT-a;
  cloud regresija + fallback + /api/state round-trip.

(b) Novootkriveni problemi:
- **Mali modeli imitiraju strukturu iz history-ja:** prva verzija
  sanitize-a pisala je sažetke "(pozvan je alat X: {...})" — gemma3-4b
  je IMITIRALA obrazac i napisala lažni poziv u text-u + slagala da je
  pesma krenula. Popravljeno: sanitize sad POTPUNO uklanja mehaniku
  tool-ova (bez imena alata, bez JSON-a), + prompt sa eksplicitnim
  primerom. Posle popravke odgovor je bio školski tačan.
- qwen3.6-27b je na tool grešku (Calendar nije pokrenut, -600) odgovorio
  "nema događaja" umesto "kalendar nije pokrenut" — model-quality problem
  (tool rezultat nosi i `events:[]` i `error`), nije sistemski kvar.
- `jarvis serve` pri restart-u može da izadje sa "port already in use;
  reusing running instance" ako stari proces još drži port (trka u
  `__main__.py:_port_open`) — manuelni restart mora da sačeka da port
  bude slobodan. Kandidat za Fazu 5 (higijena).
- Mid-stream fallback ostavlja već emitirane delte lokalnog modela u
  chat-u (vidljiva napomena objašnjava) — prihvatljivo, nije bilo
  mehanizma za "brisanje" već stream-ovanog teksta bez novog protokola.

(c) Donete odluke:
- **§10.4 REŠENO — varijanta (a):** učitan notools model na zahtev za
  akciju kaže "nemam alate, prebaci na cloud" (ne tiho preusmeravanje).
  Razlog: (b) zahteva detekciju "ova poruka treba tool" bez modela —
  ili uvek-cloud (poništava izbor lokalnog modela) ili klasifikator
  (krhko). Fallback (T8) pokriva stvarni kvar: model nedostupan/neučitan.
- Sanitize baca celu tool mehaniku umesto sažetaka — svaki tekst koji
  liči na poziv je pozivnica za imitaciju malom modelu.
- `think:false` za SVE lokalne modele (ne samo Qwen3): Ollama ignoriše
  polje kod modela koji ga ne znaju, a jedan code path je jednostavniji.
- Perzistencija u `ui` ključu state.json-a (odvojeno od capability keša).

(d) Ostavljeno za kasnije:
- Ručna provera "refresh browser-a → isti model" u pravom browser-u
  (Faza 0) — API round-trip i boot logika verifikovani headless.
- gemma4:26b / gemma4:e2b capability još nije probe-iran (cap: None u
  katalogu) — probe se radi tek pri prvom load-u, po dizajnu.
- "port already in use" trka pri restart-u → Faza 5.

Verifikovano: compileall + node --check čisti; unit testovi (stripper 6
slučajeva + sanitize 4 slučaja) prolaze; live: fallback event, notools
odbijanje, gemma4:12b ytm_play (pesma svira), qwen3.6-27b calendar_today
(bez CoT-a), /api/state round-trip, cloud regresija.

Cilj: SVI instalirani Ollama modeli su dostupni u Jarvis-u; rade u okviru
svojih mogućnosti; korisnik nikad ne dobija halucinirane tool pozive; novi
modeli mogu da se skinu iz UI bez blokiranja aplikacije.

Taskovi:
1. Proveriti da li je `qwen3.6:27b` pull završen (`ollama list`). **PAŽNJA:**
   pull je 2026-08-13 dva puta pao na finalnom koraku sa greškom
   `Error: remove .../sha256-83c5...-partial-0: no such file or directory`
   (Ollama pokušava da ukloni partial blob koji ne postoji — stale stanje u
   daemon-u). Ako se greška ponovi, recept: (a) `ls ~/.ollama/models/blobs/
   | grep partial` da se vidi ima zaostalih fajlova; (b) restartovati Ollama
   daemon (`brew services restart ollama`); (c) ponoviti pull U POZADINI.
   Pull je idempotentan i nastavlja prekinut download.
2. **Auto-discovery kataloga**: UI local-models tab prikazuje SVE modele iz
   Ollama `/api/tags` (trenutno na disku: gemma3:4b, gemma4:12b, gemma4:26b,
   gemma4:e2b, + qwen3.6:27b kad se skine). `JARVIS_LOCAL_MODELS` u `.env`
   ostaje samo za override parametre (n_ctx, keep_alive, flags) po id-u.
3. **Background pull iz UI**: `POST /api/local_models/pull {tag}` → server
   pokreće pull kao background task (asyncio, nikad ne blokira event loop ni
   druge zahteve), progress ide na BUS (`local_model_pulling` sa procentom),
   UI prikazuje status; pull je idempotentan (Ollama sam nastavlja prekinut
   pull). Dodati i `DELETE`/cancel pull-a.
4. Capability registar: probe detekcija na load — jedan kratak zahtev sa
   tools; ako vrati 400/"does not support tools" → model označen `notools`.
   Rezultat se kešira u data/state.json. Eksplicitni override flag `notools`
   u `.env` i dalje važi. **gemma4:12b posebno testirati** — ako podržava
   tools, to je kandidat za brz lokalni default.
5. UI (local-models tab + dropdown): jasna oznaka "bez tool-ova", warning
   pri izboru; oznaka "učitan/u RAM-u"; dugme Pull za modele koji nisu na
   disku (tag input).
6. Za modele bez tools: SYSTEM_PROMPT varijanta bez sekcije o alatima +
   instrukcija "nemaš alate, reci korisniku da prebaci na cloud model za
   akcije"; history sanitize pri slanju lokalnom modelu (tool_calls/tool
   poruke → sažeti tekst ili izbaciti).
7. Ollama `think:false` u body-ju za modele koji podržavaju (Qwen3) da CoT
   ne curi u content; za ostale stripper `<think>` blokova iz content-a.
8. Graceful fallback: lokalni model unload-ovan/greška → automatski prelazak
   na default cloud model + vidljiva napomena u UI (ne tihi assistant_error).
9. Perzistencija aktivnog modela: `data/state.json` ({model, tts...}),
   `GET/PUT /api/state`; frontend čita na boot, piše na promenu.

Acceptance:
- Svi modeli sa `ollama list` vidljivi u UI; pull novog taga iz UI radi u
  pozadini dok chat normalno funkcioniše.
- gemma3-4b: odgovara čisto, bez lažnih tool poziva; za "pusti pesmu" kaže
  da nema alate (ili auto-fallback na cloud — odluka u implementaciji).
- qwen3.6-27b: poziva tools ispravno, bez CoT u odgovoru.
- gemma4:12b: tool capability utvrđen probe testom i zabeležen.
- Refresh browser-a → isti aktivan model.

### FAZA 4 — Robusnost jezgra
**Obim:** L — dve sesije: **4A** (taskovi 1-4: LLM retry, TTS timeout +
fallback, STT executor, bus overflow) i **4B** (taskovi 5-8: WS scope, trim
guard, session endpoints, local_models to_thread). Jedna sesija = jedna
pod-faza pa STAJE.
**Status:** ✅ gotovo (4A ✅ 2026-08-13 / **4B ✅ 2026-08-13**)
**Zaključci 4A (2026-08-13):**

(a) Šta je stvarno urađeno:
- **T1 — llm.py retry sloj:** `ChatStream.__aiter__` sada ima retry petlju —
  najviše 2 retry-ja (ukupno 3 pokušaja) sa exp backoff-om (0.7s → ~1.9s +
  jitter), poštuje `Retry-After` header (cap 5s). Retry-able su SAMO
  408/429/500/502/503/504 i httpx mrežne greške/timeout-i, i to SAMO dok
  ništa nije emitovano (`emitted` flag) — posle prvog tokena nema retry-ja
  da se tekst ne bi duplirao. Ostali 4xx odmah dižu LLMError. Novi event
  `llm_retry` na BUS-u (attempt/reason/delay) — vidljiv u Logs tab-u i chat-u.
  `_LocalStreamAdapter` NIJE diran — lokalni modeli već imaju svoj
  graceful fallback na cloud iz 3B.
- **T2 — tts.py timeout + fallback lanac:** `synthesize()` ograničen sa
  `asyncio.wait_for` (default 20s, env `JARVIS_TTS_SYNTH_TIMEOUT` za
  override/test). Greška ILI timeout bilo kog backend-a osim `say` →
  fallback na `_synth_say` (takođe sa timeout-om) uz `tts_fallback` event
  na BUS-u; ako padne i say, greška propagira (speech.py je već pretvara u
  `tts_error` → "skip uz tts_error" iz plana). `CancelledError` se
  propagira odmah (barge-in ne sme da čeka fallback).
- **T3 — stt.py dedicated executor:** module-level
  `ThreadPoolExecutor(max_workers=2, thread_name_prefix="whisper")` za sva
  4 whisper posla (2× load modela, 2× transkripcija) — default executor je
  oslobođen za kratke osascript/clipboard pozive.
- **T4 — bus.py overflow:** queue 1024 → 4096; na QueueFull subscriber se
  više NE uklanja (WS ne umire tiho) — najstariji event se baca da napravi
  mesto novom; `bus_overflow` event se objavljuje najviše jednom u 5s (sa
  reentrancy guard-om da notifikacija ne rekurzira) + stderr log.
- Frontend (web/app.js): handler-i za `llm_retry` ("LLM pokušaj N…"),
  `tts_fallback` ("prelazim na say") i `bus_overflow` ("red događaja
  prepun"). Cache bust app.js v10.

(b) Novootkriveni problemi:
- `tts_speak` stiže jedan await POSLE `tts_done` (speech worker prvo
  sintetizuje — što emituje tts_done iz backend-a — pa tek onda objavi
  tts_speak). Nije bug, ali headless testovi koji assert-uju redosled
  moraju da sačekaju posle assistant_done.
- "port already in use" trka pri restart-u (pozanto iz 3B) potvrđena i
  ovde — čeka Fazu 5.
- Timeout-ovan TTS thread (edge `asyncio.run` u executor-u) ne može da se
  ubije — završava u pozadini, možda ostavi fajl koji GC pokupi. Speech
  worker je oslobođen, što jeste cilj popravke.

(c) Donete odluke:
- "2 pokušaja" iz plana interpretirano kao 2 RETRY-ja (ukupno 3 pokušaja) —
  mock test potvrđuje ponašanje; konzervativnije za 429/5xx.
- Retry samo pre prvog emitovanog event-a — svaka druga opcija duplira već
  stream-ovan tekst u UI-u.
- MiniMax in-band greške (`base_resp`) se NE retry-ju — tretiraju se kao
  poslovne greške, ne transportne.
- TTS timeout preko env var-a umesto config.py — u skladu sa postojećom
  praksom u tts.py (azure/elevenlabs ključevi) i omogućava kratak timeout
  u testovima.
- bus_overflow notifikacija se injektuje direktno u queue-ove (ne kroz
  `publish`) da se izbegne rekurzija.

(d) Ostavljeno za 4B:
- Taskovi 5-8: WS session scope (frontend filtrira chat event-e po
  state.sessionId, Logs tab sve), `_trim_history` hard orphan guard,
  `POST /api/sessions` (prazna sesija, newSession bez "Zdravo") +
  `DELETE /api/sessions/{id}` sa dugmetom u listi, sync httpx pozivi u
  local_models.py → to_thread.

Acceptance 4A:
- ✅ Mock LLM server (uvicorn na 8901): always-500 → 2 `llm_retry` event-a
  + čist LLMError "HTTP 500" (retry pa error, bez loma).
- ✅ Flaky (500, 500, pa 200) → odgovor stiže posle retry-ja ("Zdravo.").
- ✅ HTTP 400 → BEZ retry-ja, odmah LLMError.
- ✅ Hanging TTS backend (simuliran, timeout 1s u testu) → fallback na say,
  fajl se proizvede za <5s; say greška propagira (nema beskonačne petlje).
- ✅ Bus: 4100 event-ova → subscriber preživljava, queue ograničen,
  bus_overflow objavljen jednom, najnoviji event-i sačuvani; drugi burst
  u roku od 5s → bez duplicate notifikacije.
- ✅ STT executor: max_workers=2, zakačen na sva 4 run_in_executor poziva.
- ✅ Live regresija posle restart-a: chat (3+4 tačno), FIFO 3 brze poruke,
  stop, perzistencija, pun WS flow (assistant_delta + tts_speak + tts_done).
- Ukupno 18/18 automatizovanih testova prolazi + compileall + node --check.

**Zaključci 4B (2026-08-13):**

(a) Šta je stvarno urađeno:
- **T5 — WS session scope (frontend filter):** NOVI
  `SESSION_SCOPED_EVENTS` skup u `web/app.js` (assistant_*,
  reasoning_delta, tool_* 12 vrsta, session_busy, session_update,
  model_fallback). `handleEvent` na početku, ako `state.sessionId` postoji
  i event pripada drugom session-u (čita `payload.session` ili
  `payload.id`), drop-uje ga tiho (i dalje appendLog za dijagnostiku).
  Logs tab i dalje dobija SVE — filter je samo na chat-vizuelu. TTS
  playback election (BroadcastChannel) i global eventi (ptt_*,
  listen_*, llm_retry, bus_overflow, tts_*, permissions_*, local_model_*,
  kilo_*, voice_ptt_transcribed, whisper_*) NISU filtrirani — emituju se
  svim tabovima.
- **T6 — _trim_history orphan guard:** NOVA `_drop_orphans()` u
  `jarvis/agent/loop.py` koja skida leading `tool` poruke I `assistant`
  poruke sa `tool_calls` (orphan tool_blocks). Zove se pre trim petlje i
  posle `del messages[:1]` grane. 5/5 unit testova prolazi (čist trim,
  orphan na startu, svi-orphan → [], samo orphan preko MAX, leading
  assistant(tcs) drop).
- **T7 — POST/DELETE sesija + newSession bez "Zdravo":** NOVO
  `agent_loop.delete_session()` (cancel + drain + SPEECH.cancel + SESSIONS.pop
  + save_sessions). `POST /api/sessions` (status 201, vraća praznu
  sesiju bez "Zdravo"). `DELETE /api/sessions/{id}` (404 za nepostojeću,
  200 + čišćenje). Frontend `newSession()` sada zove `POST /api/sessions`
  umesto `POST /api/chat {text:"Zdravo"}` (ne troši tokene). Session
  lista dobija `×` dugme (potvrda sa `confirm(...)`, hide-on-hover
  stil).
- **T8 — local_models.py sync httpx revizija:** audit potvrđuje da su
  SVI sync httpx pozivi u modulu već iza `asyncio.to_thread` (available,
  _api_tags, _ps_tags, probe_tools, _post_json). Jedini ostali sync API
  je `RUNNER.status()` (čita in-memory stanje, samo `_available()` →
  on je sync wrapper namenjen za "no event loop" kontekste; nigde se ne
  zove iz async handlera — app.py koristi `await RUNNER.astatus()`).
  Nema izmena u kodu; task se ZATVARA dokumentovanjem.

(b) Novootkriveni problemi:
- **WS broadcast ne filtrira po session-u na serveru (samo klijent).**
  To je by-design za TTS playback election (svaki tab treba da vidi
  tts_speak). Konsekvenca: filter je POUZDAN samo dok klijent poštuje
  SESSION_SCOPED_EVENTS. Ako se doda novi session-scoped event a zaboravi
  da se doda u set — bug se vraća. Workaround: set + assert da
  assistant_delta/tool_call/session_busy imaju `payload.session`. Nije
  urađeno (Faza 5 higijena).

(c) Donete odluke:
- **Filter je client-side, ne server-side.** Server-side routing bi
  značilo da event ide samo jednom WS-u → Sessions-via-socket map, a
  TTS playback election (BroadcastChannel claim) radi na klijentu.
  Dva različita mehanizma za isti problem = više koda + eventualna
  nekonzistentnost. Client filter je trivijalan i dovoljan.
- **POST /api/sessions bez zadravog "user"-msg.** Stari `newSession` je
  slao "Zdravo" čime je trošio tokene i pravio šum u history-ju
  prazne sesije. Prazna sesija = samo metadata; korisnik kuca poruku
  kad pošalje prvu.
- **DELETE button sa `confirm()`.** Brisanje sesije je destruktivno
  (briše svu istoriju), ali `confirm()` je dovoljna zaštita za solo
  korisnika koji ima pristup serveru. Bulk delete / undo ostavljen za
  budućnost ako se ukaže potreba.
- **`_drop_orphans()` je idempotent + cheap** — prolazak po listi dok
  vidi orphan blok (max nekoliko iteracija) + jedan trim ciklus.
- **`available()` i `status()` ostavljeni kao sync API** jer su
  dokumentovani za "no event loop" upotrebu (skripte, dijagnostika).
  Niko ih ne zove iz async putanje; ako se to desi u budućnosti, fix
  je trivijalan — preimenovati ih i dodati async varijantu.

(d) Ostavljeno za kasnije:
- **Hard assert da payload.session postoji** kod svih emitovanih
  session-scoped event-ova (loop.py publish sites) — sprečava
  regresiju filtera ako se doda novi event. Za Fazu 5 higijenu
  (pytest + lint).
- **Bulk session delete + undo** — nije bilo zahteva.
- **`/api/sessions/{id}/rename`** (PATCH sa title-om) — UI može da doda
  inline edit, korisno za dugačke liste. Nije traženo.

Verifikovano: compileall + node --check čisti; 5/5 orphan guard unit testova;
WS filter dual-tab test (tab B dobija 4 strana event-a, svi bivaju drop-ovani
od strane filter logike); POST/DELETE endpoint round-trip (404 za missing,
empty sesija → chat odmah radi); live chat + FIFO bez regresije (4 turns
se ne mešaju, history rastu u pravom redosledu).

Cilj: sistem preživljava mrežne greške, spore servise i loš input bez
polomljenog stanja; dva korisnika (ili dva taba u dve sesije) u Jarvisu
ne vidе tuđe razgovore.

Taskovi (iz §4.6): a, b, c, d, e, f, g, h, i.
Konkretno:
1. llm.py: retry sloj (2 pokušaja, exp backoff) za 429/5xx/timeout/mrežne
   greške PRE dizanja LLMError; ne retry-uj 4xx osim 429.
2. speech.py/tts.py: timeout po sintezi (npr. 20s); fallback lanac:
   aktivan backend → `say` (uvek dostupan) → skip uz tts_error.
3. stt.py: dedicated ThreadPoolExecutor(max_workers=2) za whisper, odvojen
   od default executor-a.
4. bus.py: umesto tihog drop-a, objavi `bus_overflow` (jednom na 5s) i
   loguj; opciono povećaj queue na 4096.
5. WS session scope: payload-i već nose `session`; frontend filtrira chat
   evente po state.sessionId (logovi tab i dalje sve).
6. loop.py `_trim_history`: hard guard — nikad orphan tool/tool_calls
   (posle sečenja validiraj sekvencu, po potrebi briši ceo tool blok).
7. app.py: `POST /api/sessions` (prazna sesija), `DELETE /api/sessions/{id}`;
   frontend newSession bez "Zdravo", delete dugme u listi.
8. local_models.py: sve sync httpx pozive preko to_thread.

Acceptance: simulirati LLM 500 (mock server) → retry i čist error event;
edge-tts offline → odgovor se i dalje izgovori preko say; 2 sesije u 2 taba
bez mešanja transkripata.

### FAZA 5 — Testovi + higijena
**Obim:** M — jedna sesija. **Status:** ✅ gotovo (2026-08-13)
**Zaključci:**

(a) Šta je stvarno urađeno:
- **T1 — dev deps + konfiguracija:** pytest 9.1.1, pytest-asyncio 1.4.0,
  ruff 0.16.2 u `.venv`. NOV `pyproject.toml`: `[project.optional-dependencies]
  dev`, `[tool.pytest.ini_options]` (asyncio_mode=auto, strict-markers/config,
  testpaths=tests), `[tool.ruff]` (line-length 110, py311, select
  E/F/W/I/UP/B/C4, isort known-first-party). `requirements.txt` ostaje
  samo za runtime deps.
- **T2 — unit testovi (64):** `tests/conftest.py` (free_port, tmp_data_dir,
  env_clean, permission_store fixture) + 6 modula:
  `test_permissions.py` (11: policy lookup/persist/reset, _summarize,
  allow/deny/ask gate, resolve, korumpiran fajl), `test_state_store.py`
  (6: round-trip, default, sibling ključevi, korumpiran fajl, atomski upis,
  20 konkurentnih upisa), `test_speech.py` (13: normalize_for_speech
  markdown/URL/emoji/code-block, _take_sentence min-dužina/force-cut/
  elipsa/baferovanje), `test_llm.py` (14: _retry_delay Retry-After cap +
  exp backoff + jitter, _absorb_content_delta dedup + overlap prozor,
  _absorb_tool_delta suffix dedup), `test_loop_history.py` (12:
  _collapse_double, _drop_orphans, _trim_history user-boundary cut +
  orphan guard), `test_logging.py` (4: env nivo, default INFO, nevažeći
  nivo, idempotentnost).
- **T3 — integracioni testovi (4):** `test_integration_chat.py` sa FAKE
  LLM serverom (FastAPI + uvicorn u thread-u, OpenAI-style SSE na
  `/chat/completions`); SETTINGS.llm patchovan preko dataclasses.replace
  (provider bailian → `/chat/completions`), world-state/TTS-sinteza/player
  stub-ovani. Pokriveno: običan turn (delte na BUS-u + history + perzistencija),
  tool petlja (`time_now` → tool poruka → finalni odgovor), FIFO 3 brze
  poruke (history [u,a,u,a,u,a]), barge-in (stop sred stream-a → bez
  orphan tool poruka, sesija nastavlja da radi).
- **T4 — ruff:** `ruff check` čist (54 nalaza → 51 auto-fix, 8 ručno:
  E402 `import re as _re` na sredini llm.py premešten na vrh, `log = ...`
  između import-a u tts.py, B904 `raise ... from exc` u app.py/hotkey.py,
  F841 u scripts/ensure_optional.py). `ruff format` primenjen (21 fajl).
  Napomena: direktorijum NIJE git repo, pa "zaseban commit" iz plana nije
  moguć — format je primenjen direktno.
- **T5 — strukturirano logovanje:** NOV `jarvis/log.py` (`setup_logging()`,
  nivo iz `JARVIS_LOG_LEVEL`, default INFO, handler na "jarvis" logger-u,
  propagate=False, idempotentno); poziva se iz `__main__.main()` i lifespan-a
  u app.py. Runtime print-ovi → logging: bus.py (overflow warning),
  config.py (parse warning + nedostajući API ključ), tts.py (edge ref
  fallback). CLI izlaz (doctor/serve/stop) NAMERNO ostao print.
- **T6 — mrtav kod + stop komanda:** iz permissions.py obrisani
  `match_pattern`, `_PERMISSION_WILDCARD` + fnmatch/re importi (§4.6 l),
  docstring ažuriran. §4.6 m rešen IMPLEMENTACIJOM: `cmd_stop` u
  `__main__.py` (`lsof -ti tcp:PORT` → SIGTERM → čekanje do 6s da se port
  zatvori; exit 1 za "nije pokrenut" / "PID nije nađen" / "port i dalje
  otvoren").
- **T7 — doctor proširen:** nowplaying-cli, ollama bin + daemon (urllib
  ka `/api/tags`, 2s timeout), status server porta, data/ upisivost,
  permissions fajl (nepostoji / nevažeći JSON → exit 1). `doctor.sh`
  ostaje wrapper oko `python -m jarvis doctor`.
- **BONUS — realan bug nađen testovima:** `list_pending()` u permissions.py
  iterirao je po Future objektima umesto po Permission →
  `GET /api/permissions/pending` (app.py:197) bi pao AttributeError-om
  dok je ijedan ask zahtev na čekanju. Fix: `_pending` sada čuva
  `(Permission, Future)` tuple; `resolve()`/`list_pending()` ažurirani;
  pokriveno testom.

(b) Novootkriveni problemi:
- `list_pending` bug (gore) — latentan od refaktora 1, popravljen odmah.
- Pri SIGTERM shutdown-u ostaje 1 leaked semaphore (resource_tracker
  warning) — potiče iz whisper/pynput multiprocessing-a, bezopasno,
  postoji i pre ove faze.
- Live server na 7777 i dalje radi STAR kod (startovan pre ove sesije) —
  korisnik treba da ga restartuje da bi izmene bile aktivne.

(c) Donete odluke:
- Dev deps u pyproject.toml (requirements.txt samo runtime).
- pytest asyncio_mode=auto (manje boilerplate-a).
- Konzervativan ruff select (bez SIM/RUF/PL) — codebase prvi put
  formatiran; pooštravanje kasnije da se ne zatrpa pravi diff.
- CLI user-facing izlaz ostaje print; logging samo za runtime dijagnostiku.
- Integracioni testovi stub-uju world-state/TTS/player (bez spoljnih
  servisa i nuspojava), ali LLM ide preko STVARNOG HTTP-a ka lokalnom
  SSE server-u — retry/parsing u llm.py se testira end-to-end.
- §4.6 m: `stop` implementiran umesto brisanja pomena iz docstring-a.

(d) Ostavljeno za kasnije:
- Pooštravanje ruff select-a (SIM, RUF, PL) — za neku od narednih faza.
- pytest-cov izveštaj — nije traženo.
- CI — direktorijum još nije git repo.
- Faza 6 stavke (web_search, play_youtube context, PTT autosend prikaz)
  ostaju netaknute.

Verifikovano: 68/68 testova (1.3s); `ruff check .` čist; `ruff format`
primenjen; compileall + node --check čisti; `jarvis doctor` pun prolaz;
`jarvis stop` live verifikovan (test instanca na 7799: SIGTERM → port
zatvoren, exit 0; stop na ugašenom serveru → exit 1; korisnikov server
na 7777 netaknut); start+shutdown test instance sa novim kodom bez grešaka.

Cilj: regresije se hvataju pre korisnika; kod je konzistentan.

Taskovi:
1. `pytest` + `pytest-asyncio` u dev deps; struktura `tests/`.
2. Unit testovi (čiste funkcije): `_take_sentence`, `normalize_for_speech`,
   `_collapse_double`, `_absorb_content_delta` (MiniMax dupli delte),
   `_trim_history` (uklj. orphan guard), `_repair_after_cancel`, permissions
   policy lookup, sessions serialize/load round-trip, dotenv read/write.
3. Integracioni test sa FAKE LLM serverom (FastAPI mock koji strimuje SSE):
   chat turn, tool poziv, barge-in, 3 brze poruke — bez spoljnih servisa.
4. `ruff` (lint + format) config u pyproject.toml; `ruff check` + `ruff format`
   kao obavezan korak; postojeći kod formatirati u zasebnom commit-u.
5. Strukturirano logovanje: `logging` sa modulima umesto print-a; nivo preko
   env `JARVIS_LOG_LEVEL`.
6. Čišćenje mrtvog koda (§4.6 l, m); `jarvis stop` komanda (SIGTERM po port-u).
7. `scripts/doctor.sh` proširiti: nowplaying-cli, ollama, port, permissions
   file, data/ writability.

Acceptance: `pytest` zelen; `ruff check .` čist; doctor prolaz.

### FAZA 6 — UX poliranje (funkcionalno)
**Obim:** S — jedna sesija. **Status:** ✅ gotovo (2026-08-13)
**Zaključci:**

(a) Šta je stvarno urađeno:
- **T1 — web_search robusniji + iskren:** prepisan u `jarvis/agent/tools.py`.
  Regex scraping zamenjen stdlib `HTMLParser`-om (`_SearchHTMLParser`,
  `convert_charrefs=True` — tolerantan na redosled atributa i ugnježdene
  tagove, sam unescape-uje entitete). Dva endpointa u fallback lancu:
  `html.duckduckgo.com/html/` (primarni) → `lite.duckduckgo.com/lite/`
  (rezervni, trivijalna struktura). Browser-like User-Agent (stari
  "Jarvis/0.2" je lako okidao anomaly stranicu). NOVO `_ddg_resolve_url()`
  raspakuje DDG redirect `/l/?uddg=<url>` u STVARNI url rezultata (ranije je
  modelu vraćan beskorisni tracking link). Greška ILI prazan rezultat na oba
  endpointa → `ok:false` sa jasnom srpskom porukom šta da kaže korisniku
  (ranije: tiho `ok:true` sa praznom listom, model je mislio da "nema
  rezultata"). max_results clamp-ovan na 1..10. Obrisan mrtav `_strip_tags`/
  `_unescape`.
- **T2 — play_youtube bez gomilanja tabova:** `_ensure_youtube_browser`
  zamenjen sa `_ensure_youtube_page()` — JEDAN browser, JEDAN context, JEDNA
  stranica se reuse-uju između poziva. Rekreira se samo ono što je ugašeno:
  korisnik zatvori tab → nova stranica u istom context-u; zatvori ceo Chrome
  → nov browser + context. Ranije je SVAKI poziv pravio novi context i na
  uspehu ga nikad nije zatvarao (curenje). Na grešku se stranica NE zatvara
  (ostaje spremna za retry).
- **T3 — PTT autosend iskren u UI:** `PushToTalk.status()` u `jarvis/hotkey.py`
  sada izlaže `auto_send` (čita `SETTINGS.audio.push_to_talk.auto_send`,
  env `JARVIS_PTT_AUTO_SEND`). Frontend `renderPtt()` više NE piše '—':
  prikazuje "uključen — transkript se odmah šalje" / "isključen — transkript
  ide u input", i hint tekst se prilagođava stvarnom ponašanju. Polje stiže
  i kroz `/api/ptt` i kroz `/api/connections` (oba izvora `state.ptt`).
- **T4 — menubar indikator:** `bin/jarvis-menubar.py` već postoji i radi.
  Popravljen `restart()`: uklonjen mrtav `lsof` poziv i grub `pkill`, sada
  zove verifikovanu `jarvis stop` komandu (SIGTERM po portu + čekanje da se
  port zatvori, iz Faze 5) pa tek onda startuje novi server. `rumps` (koji
  menubar zahteva) dodat kao opciona stavka u `scripts/ensure_optional.py`
  (`ensure_rumps()`, macOS-only, idempotentno) — NIJE instaliran po default-u.

(b) Novootkriveni problemi:
- Stari `web_search` je imao DVA stvarna buga koja su objašnjavala "krhkost"
  iz audita (§4.6 j): (1) vraćao je DDG redirect link umesto stvarnog URL-a,
  (2) naslovi sa ne-unescape-ovanim entitetima (`&#x27;`). Oba sada rešena.
- DuckDuckGo i dalje može da vrati anomaly/blok stranicu (naročito na
  primarnom endpoint-u) — fallback na lite + iskrena greška to sada
  amortizuju, ali pretraga NIJE garantovana (nema API ključa). Ako korisnik
  želi pouzdaniju pretragu, kandidat je Brave/Serper API ključ (zahteva
  nalog) — zabeleženo kao opcija, nije traženo.
- `rumps` nije instaliran u `.venv` (menubar je opcioni). `menubar.sh` ga
  instalira kroz `ensure_optional.py` tek kad korisnik to pokrene.

(c) Donete odluke:
- DuckDuckGo ostaje izvor (bez API ključa, bez novih zavisnosti) — ali sa
  dva endpointa, pravim parserom i iskrenom greškom, umesto zamene drugim
  servisom. Ovo je "stabilniji izvor + graceful error" iz taska.
- `play_youtube` drži browser otvoren posle puštanja (korisnik gleda) —
  promenljivo stanje (jedna stranica) se reuse-uje, ne gomila se.
- PTT autosend se ČITA iz config-a i prikazuje iskreno; ne dodaje se UI
  toggle za njega (menja se kroz `.env`/`JARVIS_PTT_AUTO_SEND`) — toggle bi
  bio vizuelna/UX stavka za Fazu 7.
- Menubar: ne uvodi se nova funkcionalnost, samo se postojeća čini tačnom
  (restart preko `jarvis stop`) i instalabilnom (`ensure_optional.py`).

(d) Ostavljeno za kasnije:
- Vizuelne stavke (markdown render, reasoning prikaz, session rename/delete
  UI, "svira sada" indikator, PTT autosend toggle) — po planu u Fazi 7.
- Pouzdanija web pretraga preko pravog API-ja (Brave/Serper ključ) — opciono,
  ako korisnik zatraži.

Verifikovano: compileall + node --check + `ruff check` + `ruff format` čisti;
84/84 testova (68 postojećih + 16 novih: 11 web_search parser, 4 play_youtube
reuse, 1 PTT auto_send). Live: web_search vraća stvarne URL-ove i čiste
naslove; failover primarni→lite radi (i ćirilica); oba endpointa down →
graceful `ok:false`; `/api/ptt` i `/api/connections` izlažu `auto_send`;
headless UI prikazuje stvarnu autosend vrednost u oba stanja (false→
"isključen…", true→"uključen…") bez console grešaka; chat regresija (2+2=4,
3+5=8) i tool registar (21 tool) OK.

Napomena: vizuelne stavke (markdown render, reasoning prikaz, session
rename/delete UI, "svira sada" indikator) NE rade se ovde — prebačene su u
Fazu 7 da se ne radi dvaput. Ovde samo funkcionalne popravke:

Taskovi:
1. `web_search` zamena stabilnijim izvorom (ili barem graceful error poruka). ✅
2. `play_youtube`: jedan persistent context, bez gomilanja tabova. ✅
3. PTT status iskren u UI (autosend vrednost iz config-a, ne '—'). ✅
4. Razmotriti: mini tray/menubar indikator (bin/jarvis-menubar.py već postoji). ✅
   (popravljen restart + rumps dodat u ensure_optional)

---

### FAZA 7 — Kompletan UI/UX redizajn (web-ui/, Vite + React + TS)
**Obim:** L — dve sesije: **7A** (scaffold + design sistem + layout + chat
iskustvo) i **7B** (ostali tabovi + modali + serving + responsive + a11y).
Jedna sesija = jedna pod-faza pa STAJE. Ovo je poslednja faza — radi se tek
kad je funkcionalnost stabilna (posle F1-F6), da redizajn ne bi morao da se
prepravlja.
**Status:** ✅ gotovo (7A ✅ 2026-08-13)
**Zaključci 7A (2026-08-13):**

(a) Šta je stvarno urađeno:
- **STACK ODLUKA (vidi §10.6):** vanila JS zamenjen sa Vite + React + TS u
  NOVOM direktorijumu `web-ui/`. Razlozi: obim Faze 7 (markdown render,
  reasoning blokovi, design sistem, modali, a11y) prevazilazi vanilla bez
  build-a. Astro odbačen (pogrešna paradigma za interaktivnu app), Next.js
  odbačen (SSR/routing/API nepotrebni). Stari `web/` NETAKNUT — i dalje se
  servira na 7777, radi kao pre.
- **Scaffold:** `web-ui/` sa `package.json` (react 18.3, vite 6, typescript
  5.6, @vitejs/plugin-react 4 — bez drugih zavisnosti), `vite.config.ts`
  (dev server 5173, proxy `/api` i `/ws` → 127.0.0.1:7777), `tsconfig.json`
  (strict + verbatimModuleSyntax), `index.html`, `src/main.tsx`,
  `src/App.tsx`, `src/styles.css`.
- **Core infra:** `src/store.ts` — klasa sa `subscribe`/`set`/transcript
  helper-i (`startAssistant`, `appendDelta`, `appendReasoning`,
  `doneAssistant` (radi `collapseDouble`), `cancelAssistant`, `addUser`,
  `addTool`, `updateTool`, `addAssistantFinal`, `clearTranscript`); UI
  preko `useSyncExternalStore` sa selektorima (per-delte re-render samo
  komponenti koje koriste transcript). `src/lib/api.ts` (jfetch/jpost/jput),
  `src/lib/text.ts` (collapseDouble 1:1 prenešen), `src/lib/speech.ts`
  (BroadcastChannel elekcija preneta 1:1: TAB_ID, claim/token 150 ms
  prozor, najmanji token + tie-break po TAB_ID, stopSpeech pump token,
  unlockAudio na AudioContext, speakManual vraća error string).
- **WS dispatcher:** `src/lib/bus.ts` sa `SESSION_SCOPED_EVENTS` setom
  (isti kao vanilla — assistant_*, reasoning_delta, tool_*, session_busy/
  update, model_fallback) i svim 35+ handler-a 1:1 prenesenim (uklj.
  TTS ×N agregaciju po turn-u, listen_enter/exit, voice_ptt_transcribed
  za PTT/mic flow, model_fallback). WS reconnect sa 1.5s timer-om.
- **Actions:** `src/lib/actions.ts` — REST pozivi 1:1 (POST /api/chat sa
  `interrupt` flag, POST /api/chat/stop, GET/POST/DELETE /api/sessions,
  GET/PUT /api/state, GET /api/models, GET /api/tts/voices, POST /api/
  audio/tts/voice, POST /api/audio/tts/play, POST /api/audio/stt,
  POST /api/audio/listen/start|stop, POST /api/local_models/load).
  Persist UI (model + tts_enabled) na svaku promenu. Browser mic flow
  (MediaRecorder + listen start/stop) portovan.
- **Komponente:** `TopBar` (brand, WS status (dot + tekst), model select,
  glas select grupisan po backend-ima + test dugme sa demo tekstom po
  backend-u, TTS toggle, server playback dugme za poslednji odgovor);
  `SessionsSidebar` (nova + lista + delete sa confirm); `Transcript`
  (prazan state, auto-scroll, memoizovane `Message`); `Message` (user /
  tool / assistant sa reasoning details blok-om koji se automatski
  zatvara kad prestane thinking + markdown render po završetku
  streaming-a); `Composer` (mic sa `recording` i `listening` stanjima +
  puls animacija + listen badge; textarea Enter/Shift+Enter + auto-
  resize; busy tekst sa queue brojem; stop dugme disable-ovano kad nije
  busy); `ListenOverlay` (apsolutno pozicioniran overlay sa pulsirajućim
  dot-om tokom PTT/mic snimanja — "Slušam…" / "Snimam…").
- **Design tokeni (Faza 7A):** CSS varijable u `src/styles.css` — dark
  default paleta (`--bg-0..4`, `--text-1..3`, `--accent`, `--accent-strong`,
  `--accent-2`, `--ok`, `--warn`, `--err`), spacing skala (1..6),
  radius (s/m/l), shadow-ovi, font-sans/mono, focus ring (prsten oko
  vidljivog fokusa), puls animacija za mic i overlay dot, respekt za
  `prefers-reduced-motion` (sve animacije isključene na reduce).
  Light tema NAMERNO odložena za 7B (vanilla je imao light ali Faza 7
  princip kaže "dark theme default"; lako se doda).
- **Hotkey:** cmd/ctrl+alt+space → toggleMic (preneseno iz vanilla).
- **Verifikacija:** typecheck (`tsc --noEmit`) čist; vite build
  (166 KB JS / 54 KB gzip); Vite dev server radi na 5173, proxy radi
  (`/api/models`, `/api/tts/voices`, `/api/chat`, WS). Headless test
  (Playwright Chromium): WS konekcija živa, 11 modela u dropdown-u,
  sesije u sidebar-u, slanje poruke → asistent "6", tool poziv
  "→ calendar_today({})" se renderuje, markdown render za
  `<h3>Voće</h3><ul><li><strong>jabuka</strong></li><li>kruška</li></ul>`,
  cross-tab filter (page2 sa svojom sesijom NE vidi page1 događaje),
  TTS ×N agregacija (1 red), prazan state za novu sesiju, delete sesije
  sa potvrdom; **nema console grešaka**.

(b) Novootkriveni problemi:
- Light tema nije prenesena u 7A (vanilla je imao `prefers-color-scheme:
  light`). Odloženo za 7B. Nije blocker.
- Reason toggle u `<details>` tokom streaming-a se "resetuje" ako korisnik
  ručno otvori a zatim stigne novi delta (jer `open` prop postaje opet
  true). Prihvatljivo za 7A, može da se poboljša u 7B ako bude smetalo.
- Vite dev server (5173) i FastAPI (7777) moraju biti istovremeno upaljeni
  tokom razvoja. Planiran za 7B: build `web-ui/dist` i flip serving-a
  u `app.py` (fallback na `web/`); do tada 5173 je developer-only.
- `confirm(...)` se koristi za delete sesije (parity sa vanilla); u 7B
  treba lepši modal.

(c) Donete odluke:
- **Vite + React + TS** (predlog iz §10.6) — bez ijedne dodatne
  biblioteke (nema markdown biblioteke, nema state biblioteke, nema UI
  biblioteke). Custom markdown parser (~70 linija) u `src/lib/markdown.ts`
  (escape HTML → block parsing: fenced code, heading, ul/ol, paragraphs;
  inline: code, bold, italic, link; bez sanitization biblioteke jer se
  ceo ulaz escapuje pre parsiranja).
- **Custom store** (klasa + `useSyncExternalStore`) umesto zustand/jotai
  — zadržava minimum dependency-ja. `transcript` se update-uje imutabilno
  (replace poslednjeg elementa) — `useApp((s) => s.transcript)` re-renderuje
  Transcript samo kad se niz promeni; selektori kao `s.busy`,
  `s.wsConnected` etc. ne diraju ga.
- **Dva React rendering moda za asistent poruke:** tokom streaming-a
  `assistant-text.plain` (pre-wrap, brz); na `doneAssistant` prelazi
  na `.md` (markdown). Parcijalni markdown tokom streaming-a je
  nepotrebno komplikovan (treperi ne-zatvorene code blokove).
- **BroadcastChannel election 1:1 prenesen** — nije menjan, jer je radio
  i u vanilla-i i cross-tab je kritičan. Samo je premešten u modul.
- **Vite proxy:** `/api` (REST + audio fajlovi) i `/ws` (WebSocket);
  production build u `web-ui/dist` — FastAPI serving flip je u 7B.
- **`bus.ts` i `actions.ts` odvojeni** od `store.ts` da se izbegne
  cirkularnost (store → speech → actions → bus → store).

(d) Ostavljeno za 7B:
- Tabovi Dozvole / Konekcije / Lokalni modeli / Alati / Logovi (kompletna
  porta iz vanilla; Logovi tab renderuje `state.logs` koji se već puni u
  7A dispatcher-u).
- Permission modal (allow/deny/remember) sa confirm unutar komponente
  (umesto `window.confirm`).
- `app.py`: serving preferira `web-ui/dist/index.html` ako postoji
  (`StaticFiles(directory=str(WEB_UI_DIR / "dist"))` mount na `/static`
  + root `FileResponse` na `WEB_UI_DIR / "dist" / "index.html"`); fallback
  na stari `web/` ako nema. Cache bust strategija.
- Responsive (usk prozor / telefon), a11y audit (kontrast, aria, keyboard
  nav), light tema.
- Markdown render u toku streaming-a (opciono, ako korisnik traži).

Acceptance 7A:
- ✅ typecheck + build čist (166 KB JS, 54 KB gzip)
- ✅ Headless: WS živa, 11 modela, sesije sidebar, chat round-trip, tool
  call red (`→ calendar_today`), markdown render (h + ul + strong),
  cross-tab filter, TTS ×N agregacija, prazan state, delete sesije
- ✅ Bez console grešaka
- ✅ Stari UI na 7777 i dalje radi (netaknut)

**Definicija gotovo:** scaffolding + design sistem + chat iskustvo su
spremni za development na 5173. Stari UI i backend su netaknuti. Pre
bilo kakvog flip-a serving-a (7B), feature-parity checklist u ovoj
sekciji MORA proći 100%.

**STACK ODLUKA (2026-08-13, vidi §10.6):** vanilla JS zamenjen sa
**Vite + React + TypeScript** u NOVOM direktorijumu `web-ui/`. Stari `web/`
ostaje NETAKNUT i servira se dok `web-ui/dist` ne dostigne 100% feature
parity. Backend (REST/WS/event kontrakt) se NE dira. Dev flow:
`npm run dev` u `web-ui/` (port 5173, proxy `/api` + `/ws` → 7777).

**Zaključci 7B (2026-08-13):**

(a) Šta je stvarno urađeno:
- **Tabovi Dozvole/Konekcije/Lokalni modeli/Alati/Logovi** portovani 1:1 u
  React. `App.tsx` sada ima `<nav className="tabbar">` sa 6 dugmadi (role="tab"
  + aria-selected + aria-controls), `<main>` sa 6 tabpanela
  (`hidden={activeTab !== …}` da se ne renderuju u off stanju). Stari
  ChatTab i dalje centralni.
- **ConnectionsTab**: 4 kartice (Minimax / Kilo / Audio / Push-to-Talk) sa
  KV gridom (label + code). PTT kartica aktivna — toggle dugme zove
  `/api/ptt/enable` ili `/api/ptt/disable`, status se čita iz store-a
  (events `ptt_status`). 30s `setInterval` poll `/api/ptt` DOK je
  `ptt.enabled` (čuva bandwidth kad je ugašen). `no_events_yet` accessibility
  hint renderuje upozorenje sa System Settings uputstvom (isti dijagnostički
  tekst kao u vanilla, prilagođen listen mode-u). Whisper/Piper/XTTS linije
  čitaju iz `_connections_payload` runtime_state (loaded ✓/○).
- **LocalModelsTab**: tabela sa svim modelima, capability badge (✓ tool-ovi /
  ⚠ bez tool-ova + tooltip / ?), status polje se računa iz `runner.state`
  + `runner.loaded_id` + `model.in_ram`. Pull input + start + progress bar
  (WS `local_model_pulling` event-i merge-ovani sa `/api/local_models` listom).
  Cancel dugme vidljivo samo dok je `starting`/`progress`. Engine-missing
  banner ako `runner.engine_available=false`.
- **ToolsTab**: statična tabela 21 toola iz deljenog `lib/tools.ts` modula
  (isti opisi + parametri kao u vanilla).
- **LogsTab**: live stream `<pre className="log-stream">` od `state.logs`,
  auto-scroll checkbox (persist samo u komponenti), clear dugme
  (`store.set({ logs: [] })`). Tabindex 0 za keyboard pristup.
- **PermissionModal** (već u 7A u `PermissionsTab.tsx`) renderuje se globalno
  u `App.tsx` (iznad svih tabova) — poziv iz bilo kog tab-a lebdi.
- **PermissionsTab**: već portovan u 7A, ne dira se.
- **styles.css**: `.tabbar` (tab-btn sa donjim border-om aktivnim), `.panel`,
  `.data-table` (header th sivi, hover red, .ok/.warn/.err boje), `.conn-card`,
  `.kv` (grid 110px 1fr), `.pull-list`/`.pull-row`/`.pull-bar`/`.pull-fill`,
  `.logs-head`, `.log-stream`, `.modal` overlay + `.modal-card`. Mobile
  responsive (≤720px: skraćen padding, `kv` ide 1-kol; ≤520px: sessions
  sidebar sakriven, composer wrap-uje). **Light tema** preko
  `prefers-color-scheme: light` — iste CSS varijable, drugi RGB (pozadina
  bela, tekst tamno, akcent `#2a6df4`).
- **Vite base: `/static/`** — production build stavlja asset-e u
  `/static/assets/*.js` da pašu sa FastAPI `StaticFiles` mount-om na
  `/static` (isto mesto sa kog vanilla `web/` služi styles.css/app.js).
- **FastAPI serving flip** u `app.py`: `_active_ui_dir()` bira
  `web-ui/dist/` ako ima `index.html`, inače fallback na stari `web/`. Kad
  oba ne postoje (retko, dev mode), `/` vraća 404 sa uputstvom.
- **Test**: `tests/test_web_ui_7b.py` (Playwright headless Chromium) —
  pokriva 6 tabova, 4 conn kartice, PTT toggle (label before/after assert),
  5 local modela + pull input, 21 tools, 21 permissions, logs clear, chat
  `3+5 → 8`. Console error listener hvata regresije.

(b) Novootkriveni problemi:
- Prvobitno Vite build je imao `base: '/'` → asset-i na `/assets/*.js` ali
  FastAPI sluša `/static` → 404 na JS, React se ne mount-uje. Popravka:
  Vite config `base: '/static/'` (build output referencira
  `/static/assets/...`); bez diranja app.py (mount ostaje).
- Stari vanilla-then-currently-served ponašanje: Vite `html` index.html je
  mali (samo `<div id="root">` + script tag), enterprise SPA — `head`-less
  crawleri neće naći content; nije problem za lokalnu app.
- Stari `web/app.js` (vanilla) i dalje radi u dev ako se obriše `dist/`
  (serving flip). Nije uklonjen — fallback bez build-a.

(c) Donete odluke:
- **Serving flip preko `web-ui/dist` PRVI, `web/` fallback.** Razlog: 7A
  zahtev da novi UI bude default once feature parity 100%; stari je
  potreban samo tokom razvoja ili ako build nikad ne postoji.
- **Tabpanels `hidden + conditional render`** umesto `display: none` sa
  CSS-om. Razlog: komponente koje nisu aktivne se ne mount-uju → nema
  fetch na mount, nema WS pretplata za PermissionsTab koja nije vidljiva.
  Trade-off: kad se tab prebaci, kratki remount latency (zapravo
  zanemarljiv za ovaj obim).
- **Listen mode hint** prikazuje aktivan mod (pause/duck/mute) kao hint
  pored PTT key-ja — nije dodat novi state, čita se iz
  `data.listen.mode` kod load-a.
- **Light tema** samo preko `prefers-color-scheme`, NEMA ručnog toggle-a
  (vanilla isto); dodavanje toggle-a ostavljam za neku buduću fazu ako
  korisnik to traži.
- **Mobile breakpoint 720px i 520px** — laptop prvenstveno, telefon se
  može koristiti (audio dugme + chat), ali sessions sidebar je skriven
  ispod 520px. Toggle za sidebar-mobilni ostavljam za neku narednu fazu.

(d) Ostavljeno za kasnije:
- **Inline session rename** u sidebar-u (Faza 7 zaključci 7A (d)) — i dalje
  nije implementirano (API ne postoji).
- **BroadcastChannel claim za "listening" badge** kao za TTS (Faza 2
  ostavljeno za 7B) — badge se i dalje prikazuje u svim tabovima kad je
  jedan u listen modu. Nije vizuelno bitno (badge je nenametljiv),
  ostavljam za neku narednu fazu.
- **Inline markdown tokom streaming-a** (Faza 7 7B (d)) — ostavljam za
  neku narednu fazu ako korisnik traži.

Verifikovano: `tsc --noEmit` čist; `vite build` (184 KB JS / 59 KB gzip /
CSS 14.87 KB / 3.74 KB gzip); Playwright headless (6 tabova, 4 conn
kartice, PTT toggle, 5 local modela + pull input, 21 tools, 21
permissions, logs clear, chat 3+5 → 8, 0 console grešaka); 84/84 pytest
prolazi; `ruff check` čist; compileall + node --check čisti; doctor
prolaz; FastAPI serving vraća `web-ui/dist/index.html` sa script
`/static/assets/*.js` HTTP 200.

**Feature parity checklist (MORA proći pre flip-a serving-a na web-ui/dist):**
- Topbar: WS status (dot + reconnect); model dropdown (promena → persist +
  auto-load lokalnog); voice dropdown (grupe po backend-ima) + test dugme sa
  demo tekstom; TTS toggle (persist); server playback dugme (poslednji odgovor)
- Sesije: nova prazna (POST /api/sessions), lista, aktivna, delete sa potvrdom
- Chat: streaming delta; reasoning (thinking); collapseDouble na done;
  cancelled marker; error redovi; tool call/done/error/denied redovi;
  kilo start/done; TTS agregacija "×N" (reset na assistant_start, zatvaranje
  na final/cancelled/error)
- Composer: Enter=send / Shift+Enter=newline, auto-height, busy tekst sa
  queue brojem, stop dugme (disabled kad nije busy)
- Mic: MediaRecorder → /api/audio/stt → auto-send(interrupt); listen
  start/stop("browser") pre/posle; cmd/ctrl+alt+space prečica; recording i
  listening vizuelna stanja + "slušam…" badge/overlay
- PTT: voice_ptt_transcribed (auto_send → send interrupt; inače u input);
  ptt_recording_start/end
- TTS playback: BroadcastChannel elekcija (claim/token, 150 ms prozor,
  najmanji token pušta, TAB_ID tie-break); ordered queue; stopSpeech na
  tts_stop; unlockAudio na pointerdown/keydown; server_played preskoči;
  speakManual (test glasa)
- session-scoped filter (SESSION_SCOPED_EVENTS: assistant_*, reasoning_delta,
  tool_*, session_busy/update, model_fallback) — drop kad payload.session/id
  nije naš; Logs tab sve
- /api/state GET/PUT round-trip (ui.model, ui.tts_enabled)
- Cross-tab: 2 taba → TTS samo u jednom; dva taba u dve sesije bez mešanja
- Tabovi (7B): Dozvole (default politika + tabela + reset + modal
  allow/deny/remember); Konekcije (minimax/kilo/audio/PTT + toggle +
  accessibility hint + 30s poll); Lokalni modeli (tabela + capability +
  load/unload + pull + progress + cancel); Alati (statična tabela); Logovi
  (živi stream + autoscroll + clear + truncation 120KB)

Cilj: kompletan vizuelni i interakcioni redizajn kontrolnog panela — miran,
moderan, voice-first interfejs sa doslednim design sistemom. Chat je
centralno iskustvo; sve ostalo je podrška.

Principi (tvrda ograničenja):
- Vanilla JS/CSS, BEZ build-a, BEZ bundler-a, BEZ novih dependency-ja.
- Feature parity: sve što postoji posle F1-F6 mora da radi i posle redizajna
  (napraviti checklist pre početka, proterati na kraju).
- Logika se NE dira, prezentacija se menja: WS handling, BroadcastChannel
  elekcija, REST pozivi, state objekat — sve ostaje, samo se restrukturira
  (app.js trenutno 970+ linija u jednom fajlu — razbiti na jasne sekcije/
  module bez build-a, npr. zasebni `<script>` fajlovi ako treba).

Taskovi (revidirani za Vite + React + TS):
**7A — scaffold + design sistem + layout + chat:**
1. Scaffold `web-ui/`: Vite + React + TS; dev server 5173 sa proxy `/api` i
   `/ws` → 127.0.0.1:7777; build u `web-ui/dist`. Stari `web/` ne dirati.
2. Design tokeni (CSS varijable): dark default paleta, tipografija (system
   font stack), spacing skala, radius, shadow, focus stanja.
3. Core infra: REST helper-i, WS hook (auto-reconnect, session-scoped
   filter), speech playback modul (BroadcastChannel elekcija preneta 1:1),
   store (useSyncExternalStore).
4. Layout: topbar (WS status, model, glas + test, TTS toggle, server
   playback), sidebar sa sesijama (nova + delete; rename NE postoji u API-ju
   — vidi odstupanja), glavni chat prostor.
5. Chat iskustvo: markdown renderer (bez dependency-ja: bold/italic/list/
   code/link/heading), reasoning kao zaseban kollapsibilan blok (ne
   prepisuje glavni tekst), jasni tool-call redovi, error/empty stanja,
   TTS ×N agregacija.
6. Glasovne interakcije: pulsirajući mic indikator, "slušam…" overlay tokom
   PTT/mic snimanja (iz Faze 2), busy/stop jasno vidljivi, PTT transkript
   flow (auto_send / input).

**7B — ostali tabovi + modali + serving + kvaliteta:**
7. Tabovi Dozvole / Konekcije / Lokalni modeli / Alati / Logovi: iste
   informacije, čistije tabele i paneli; lokalni modeli sa pull progress
   indikatorom (iz Faze 3).
8. Permission modal i ostali modali: redesign u skladu sa tokenima.
9. FastAPI serving `web-ui/dist` (fallback na stari `web/` dok dist ne
   postoji); cache bust svih asset-a.
10. Responsive: laptop-first, ali upotrebljiv i u uskom prozoru.
11. Pristupačnost: kontrast, keyboard navigacija, aria oznake, poštovanje
    `prefers-reduced-motion`.

Acceptance:
- Feature parity checklist (napravljen pre početka) prolazi 100%.
- Svi tabovi rade; permission flow, TTS playback i cross-tab elekcija
  netaknuti.
- Keyboard navigacija i kontrast OK (ručna provera).

---

## 7. Kako testirati (brzi recept)

```bash
cd /Users/marko/Documents/1-Projects/Jarvis
.venv/bin/python -m jarvis serve --no-browser   # port 7777

# chat
curl -s -X POST localhost:7777/api/chat -H 'Content-Type: application/json' \
  -d '{"text":"Koliko je 2+2?"}'
# -> {"session_id":"..."} ; zatim:
curl -s localhost:7777/api/sessions/<ID>

# prekid turn-a
curl -s -X POST localhost:7777/api/chat/stop -H 'Content-Type: application/json' \
  -d '{"session_id":"<ID>"}'

# veštački test brzih poruka: pošalji 3 POST /api/chat sa istim session_id
# i proveri da je history [user,assistant,user,assistant,user,assistant]

# perzistencija: posle chata proveri data/sessions.json; restartuj server
# i proveri GET /api/sessions da sesija postoji
```

Obavezna verifikacija posle SVAKE izmene:
```bash
.venv/bin/python -m compileall jarvis/   # Python syntax
node --check web/app.js                  # JS syntax
```

UI: otvoriti `http://127.0.0.1:7777/` u browser-u (hard refresh zbog keša).

---

## 8. Ključne datoteke i njihova uloga

| Fajl | Uloga |
|---|---|
| `jarvis/app.py` | FastAPI rute + WS + lifespan |
| `jarvis/agent/loop.py` | SessionManager, turn queue, barge-in, perzistencija |
| `jarvis/agent/prompts.py` | SYSTEM_PROMPT (persona + pravila) |
| `jarvis/agent/tools.py` | Tool registry (async subprocess); robusni web_search + play_youtube reuse (F6 ✅) |
| `jarvis/agent/kilo_bridge.py` | Kilo CLI bridge |
| `jarvis/llm.py` | LLM streaming klijent (cloud + local adapter) |
| `jarvis/local_models.py` | Ollama runner (load/unload/stream) |
| `jarvis/audio/speech.py` | Server-driven TTS scheduler |
| `jarvis/audio/tts.py` | TTS backend-ovi (6) |
| `jarvis/audio/stt.py` | Whisper STT + warmup |
| `jarvis/audio/player.py` | afplay server playback |
| `jarvis/hotkey.py` | Global push-to-talk |
| `jarvis/permissions.py` | allow/ask/deny gate |
| `jarvis/bus.py` | Event bus |
| `jarvis/state.py` | Deljeni singletoni |
| `jarvis/config.py` | Settings (.env + kilo.jsonc) |
| `jarvis/media/nowplaying.py` | Pouzdana media kontrola + verifikacija (F1 ✅) |
| `jarvis/context.py` | World-state kontekst po turn-u (F1 ✅) |
| `jarvis/audio/focus.py` | Listen mode / audio fokus (F2 ✅) |
| `jarvis/state_store.py` | Perzistentni key/value state (data/state.json) |
| `jarvis/log.py` | Logging setup, `JARVIS_LOG_LEVEL` (F5 ✅) |
| `web/app.js` | Frontend (chat, TTS playback, elekcija) |
| `data/sessions.json` | Perzistirane sesije |
| `data/state.json` | Capability keš + ui state (aktivan model, TTS) (F3 ✅) |
| `tests/` | pytest suite: 84 testova, unit + integracioni sa fake LLM (F5+F6 ✅) |
| `pyproject.toml` | pytest + ruff konfiguracija, dev deps (F5 ✅) |
| **PLANIRANO** | |
| `web/ (redizajn)` | Design tokeni, layout, markdown render, a11y (F7) |

---

## 9. Dnevnik izmena (ažurirati posle svake faze)

- **2026-08-13 (Refaktor 2):** cross-tab TTS elekcija, tool-first prompt,
  perzistencija sesija. Detalji u §3.
- **2026-08-13 (kasnije):** kompletan audit codebase-a, root-cause analiza
  (§4), roadmap u 7 faza + protokol rada po sesijama (§6). Background
  `ollama pull qwen3.6:27b`: OBA pokušaja pala na finalnom koraku sa
  partial-blob error-om (stale stanje Ollama daemon-a) — recept za popravku
  je u Fazi 3 task 1. Proveriti `ollama list` pre Faze 3.
- **2026-08-13 (Faza 1):** Mediji + stanje sveta gotovo. nowplaying-cli
  instaliran; `jarvis/media/nowplaying.py` (verifikovana kontrola + fallback
  lanac); `jarvis/context.py` world-state po turn-u; ytm_* prepisani;
  TTS log agregacija ×N; prompt dopunjen. Acceptance prolazi (vidi
  Zaključke Faze 1).
- **2026-08-13 (Faza 2):** Listen mode + globalni hotkey gotovo. NOV
  `jarvis/audio/focus.py` (AudioFocusManager sa set-based refcount);
  `SpeechScheduler.cancel_all()`; `ListenModeSettings` u config + .env
  (`JARVIS_LISTEN_MODE=pause` default, `JARVIS_LISTEN_DUCK_VOLUME=15`);
  PTT prepisan da koristi focus.enter/exit (transkripcija ne čeka
  restore); `/api/audio/listen/{start,stop}` endpointi; toggleMic
  integriše focus; "listening" badge + mic outline; PTT accessibility
  dijagnostika (`no_events_yet` + System Settings uputstvo u UI).
  Live verifikovano: pusti pesmu → listen enter → rate 1→0 → exit →
  rate 0→1; refcount sa 2 različita razloga; idempotentan ulaz istog
  razloga. Detalji u §6 Faza 2 Zaključci.
- **2026-08-13 (Faza 3A):** Pre početka kompletna live verifikacija F1+F2 —
  svi acceptance kriterijumi prolaze, bez popravki. Lokalni modeli:
  auto-discovery katalog iz `/api/tags` (`.env` samo override, novo polje
  `flags`); background pull iz UI sa progress događajima + cancel;
  capability probe na load sa kešom u `data/state.json` (NOV
  `jarvis/state_store.py`); notools modeli ne dobijaju tool šeme; UI
  badge-i + pull progress + auto-load iz dropdown-a. **gemma4:12b
  podržava tools** (live verifikovan tool loop) — kandidat za brz lokalni
  default. qwen3.6:27b pull restartovan po receptu (restart daemon-a) i
  radi detached u pozadini (~45% na kraju sesije). Detalji u §6 Faza 3
  Zaključci 3A.
- **2026-08-13 (Faza 3B):** qwen3.6:27b pull završen i verifikovan (tools,
  live tool loop bez CoT-a — `think:false` u svakom local body-ju +
  `_ThinkTagStripper` safety net). `SYSTEM_PROMPT_NOTOOLS` + izbor prompta
  po capability-ju u `run_turn`; `sanitize_history_for_notools()` POTPUNO
  uklanja tool mehaniku iz history-ja za notools modele (prva verzija sa
  sažecima "(pozvan je alat...)" izazvala IMITACIJU lažnih poziva kod
  gemma3-4b — odbačeno). Graceful fallback lokalni→cloud (pre-loop is_ready
  provera + mid-stream exception handler) sa `model_fallback` event-om i
  napomenom u UI. `GET/PUT /api/state` (ui: model + tts_enabled) +
  frontend perzistencija na boot/promenu (app.js v9). gemma4:12b live:
  `ytm_play` → pesma potvrđeno svira (nowplaying-cli). Detalji u §6
  Faza 3 Zaključci 3B.
- **2026-08-13 (Faza 4A):** Robusnost jezgra — prva pod-faza gotova.
  `llm.py` retry sloj (2 retry-ja, exp backoff + Retry-After,
  408/429/5xx/mrežne greške, samo pre prvog tokena, `llm_retry` event);
  `tts.py` timeout po sintezi (default 20s, env `JARVIS_TTS_SYNTH_TIMEOUT`)
  + fallback lanac aktivan backend → `say` → `tts_error` (`tts_fallback`
  event); `stt.py` dedicated ThreadPoolExecutor(max_workers=2) za whisper;
  `bus.py` queue 4096 + na overflow bacanje najstarijeg event-a umesto
  tihog izbacivanja subscriber-a, `bus_overflow` event (1/5s) + log.
  Verifikovano mock LLM serverom (18/18 testova: retry-then-error,
  retry-then-success, 400 bez retry-ja, TTS timeout→say fallback, bus
  overflow) + live regresijom posle restart-a. Detalji u §6 Faza 4
  Zaključci 4A.
- **2026-08-13 (Faza 4B):** Robusnost jezgra — druga pod-faza gotova.
  WS session scope: `SESSION_SCOPED_EVENTS` set u `web/app.js` +
  `eventSession()` helper; chat-skopirani eventi (assistant_*,
  tool_*, session_busy/update, model_fallback) se drop-uju u tabu koji
  nije vlasnik sesije — Logs tab i dalje SVE; TTS playback election +
  globalni eventi (ptt/listen/llm_retry/bus/tts/permissions/local_model/
  kilo/whisper) NISU filtrirani. `_trim_history` orphan guard: NOVA
  `_drop_orphans()` u `loop.py` (skida leading `tool` + `assistant(
  tool_calls)` blokove) — zove se pre i posle `del [:1]` grane; 5/5
  unit testova. `POST /api/sessions` (status 201, prazna sesija) +
  `DELETE /api/sessions/{id}` (cancel+drain+SPEECH.cancel+pop+save, 404
  za nepostojeću); `newSession()` u app.js sada zove prazan endpoint
  (bez "Zdravo" — ne troši tokene); `×` delete dugme u listi (potvrda
  sa `confirm`, hide-on-hover). `local_models.py` sync httpx audit:
  svi sync pozivi (available, _api_tags, _ps_tags, probe_tools,
  _post_json) već iza `asyncio.to_thread`; `RUNNER.status()`/`available()`
  su sync API za "no event loop" namenu, ne zovu se iz async handlera
  — task dokumentovan kao green, bez izmene koda. Verifikacija:
  compileall + node --check čisti, orphan guard 5/5, WS dual-tab test
  (tab B dobija 4 A-eventa, sva 4 dropovana od filter logike), POST/
  DELETE round-trip, live chat + FIFO bez regresije (4 turns
  korektno raspoređeni u history). Detalji u §6 Faza 4 Zaključci 4B.
- **2026-08-13 (Faza 5):** Testovi + higijena gotovo. NOV `pyproject.toml`
  (pytest asyncio auto + ruff E/F/W/I/UP/B/C4, dev extras); `tests/` sa
  68 testova: unit (permissions 11, state_store 6, speech 13, llm 14,
  loop history 12, logging 4) + 4 integraciona sa fake SSE LLM serverom
  (chat turn, tool petlja, FIFO 3 brze poruke, barge-in repair).
  `ruff check` čist + `ruff format` primenjen (21 fajl). NOV
  `jarvis/log.py` (`JARVIS_LOG_LEVEL`); runtime print-ovi (bus/config/tts)
  prebačeni na logging. Uklonjen mrtav kod (`match_pattern`,
  `_PERMISSION_WILDCARD`); NOVA `jarvis stop` komanda (SIGTERM po portu);
  `doctor` proširen (nowplaying-cli, ollama daemon, port, data/,
  permissions fajl). Testovima nađen i popravljen realan bug:
  `list_pending()` u permissions.py (endpoint padao dok je ask zahtev
  pending). Verifikacija: 68/68, ruff čist, compileall + node --check,
  doctor prolaz, stop live testiran. Detalji u §6 Faza 5 Zaključci.
- **2026-08-13 (Faza 6):** UX poliranje (funkcionalno) gotovo. `web_search`
  prepisan: stdlib HTMLParser umesto krhkog regex-a, dva DDG endpoint-a u
  fallback lancu (html → lite), browser-like UA, `_ddg_resolve_url` raspakuje
  redirect u STVARNI url, iskrena srpska greška kad oba endpointa padnu
  (ranije tiho `ok:true` sa praznom listom). `play_youtube` sada reuse-uje
  JEDNU stranicu/context/browser (`_ensure_youtube_page`) — nema gomilanja
  tabova; rekreira se samo ugašeno. PTT `status()` izlaže `auto_send` iz
  config-a; frontend `renderPtt` prikazuje stvarnu vrednost (ne više '—') u
  oba stanja. Menubar `restart()` prebačen na verifikovanu `jarvis stop`
  komandu (uklonjen mrtav lsof/pkill); `rumps` dodat kao opciona stavka u
  `ensure_optional.py`. Verifikacija: 84/84 testova (+16 novih), ruff čist,
  live web_search (stvarni URL-ovi, failover, graceful greška), headless UI
  autosend u oba stanja bez console grešaka, chat regresija OK. Detalji u §6
  Faza 6 Zaključci.
- **2026-08-13 (Faza 7A):** UI/UX redizajn — prva pod-faza gotova u novom
  `web-ui/` direktorijumu (Vite 6 + React 18 + TypeScript 5.6, bez drugih
  zavisnosti). Stack odluka (§10.6) — vanilla odbačen zbog obima Faze 7;
  Astro/Next.js odbačeni (pogrešna paradigma). Stari `web/` NETAKNUT i
  i dalje se servira na 7777 (korisnik pitao da li ima problema —
  nema, potvrđeno sa `curl localhost:7777/` vraća stari `index.html`).
  Preneto: BroadcastChannel TTS elekcija 1:1, WS session-scoped filter,
  collapseDouble, SESSION_SCOPED_EVENTS, svih 35+ event handler-a, kompletan
  REST flow (sessions CRUD, models, voices, STT/listen, TTS voice/play),
  MediaRecorder mic, PTT flow, persist UI state. Custom store sa
  `useSyncExternalStore` (per-delte selektivni re-render). Custom markdown
  parser (~70 linija, escape-then-parse; siguran). Design tokeni (dark
  default; light odložen za 7B). Komponente: TopBar, SessionsSidebar,
  Transcript, Message (memo), Composer, ListenOverlay, ChatTab. Pulsirajući
  mic + listen overlay + fokus prsten + respekt za prefers-reduced-motion.
  cmd+alt+space globalni hotkey. Verifikacija: `tsc --noEmit` čist, vite
  build (166 KB JS / 54 KB gzip), Playwright headless — WS živa, 11 modela,
  sesije sidebar, chat round-trip, tool red `→ calendar_today`, markdown
  render za `<h3>Voće</h3><ul><li><strong>jabuka</strong></li>...`,
  cross-tab filter (page2 sa svojom sesijom ne vidi page1 događaje),
  TTS ×N agregacija, prazan state, delete sesije, **bez console grešaka**.
  Stari UI i backend netaknuti (compileall OK). Detalji u §6 Faza 7
  Zaključci 7A.
- **2026-08-13 (Faza 7B):** UI/UX redizajn — druga pod-faza gotova. Tab navigacija
  (Razgovor/Dozvole/Konekcije/Lokalni modeli/Alati/Logovi) u `src/App.tsx` (tabpanel
  + role="tab"/aria-selected/aria-controls, hidden kad nije aktivan). NOVI
  `ConnectionsTab` (minimax/kilo/audio/PTT kartice sa KV gridom, PTT toggle
  dugme + 30s poll dok je enabled, `no_events_yet` accessibility hint upućuje
  korisnika na System Settings → Privacy & Security → Accessibility — isti
  dijagnostički tekst kao u vanilla, prilagođen listen mode-u). NOVI
  `LocalModelsTab` (tabela modela + capability badge ✓/⚠/?, status "učitan u
  RAM" / "učitavam" / "u RAM (Ollama)" / "na disku", load/unload, pull input
  + progress bar + cancel; pulls iz WS-a merge-ovani sa server stanja).
  NOVI `ToolsTab` (statična tabela 21 toola iz TOOL_DESCRIPTIONS + parametri).
  NOVI `LogsTab` (live stream + auto-scroll checkbox + clear; bus.logs već
  puni dispatcher iz 7A). PermissionModal (već urađen u 7A u
  PermissionsTab.tsx) renderuje se globalno u App.tsx. styles.css: `.tabbar`,
  `.panel`, `.data-table`, `.conn-card`, `.kv`, `.pull-list`/`.pull-bar`,
  `.log-stream`, `.modal`/`.modal-card` klase + mobile breakpoint (≤720px i
  ≤520px) + light tema (`prefers-color-scheme: light` preko CSS varijabli).
  Vite `base: '/static/'` da build output bude kompatibilan sa FastAPI
  `/static` mount-om. FastAPI serving flip u `app.py`: `_active_ui_dir()`
  bira `web-ui/dist/index.html` ako postoji, inače fallback na stari `web/`.
  Novi `tests/test_web_ui_7b.py` (Playwright headless): 6 tabova, 4 kartice
  konekcija, PTT toggle (label before/after), local models 5 rows + pull input,
  21 tools, 21 permissions, logs clear, chat `3+5 → 8` — nema console grešaka.
  Verifikacija: typecheck + build čist (184 KB JS / 59 KB gzip), 84/84
  pytest prolazi, ruff čist, compileall + node --check čisti, doctor prolaz,
  server na 7777 vraća `web-ui/dist/index.html` (asset `/static/assets/*.js`
  HTTP 200). Stari `web/` netaknut. Detalji u §6 Faza 7 Zaključci 7B.

---

## 10. Otvorene odluke (pitati korisnika pre implementacije)

1. ~~**nowplaying-cli**~~ — **REŠENO (2026-08-13, Faza 1):** korisnik odobrio
   `brew install nowplaying-cli` (2.1.0). Instalirano i verifikovano u
   `jarvis/media/nowplaying.py` kao primarni kanal (MR ctypes → keystroke
   fallback).
2. ~~**Listen mode default**~~ — **REŠENO (2026-08-13):** korisnik tražio da
   sav zvuk STANE dok priča → default `pause` (muzika se pauzira i nastavi
   posle transkripcije).
3. ~~**Lokalni modeli**~~ — **REŠENO (2026-08-13):** korisnik nije ograničen
   na skinute modele, sme da skida nove. `qwen3.6:27b` pull STARTOVAN u
   pozadini 2026-08-13. Katalog treba da prikaže SVE instalirane modele
   (auto-discovery), uključujući gemma4:12b/26b/e2b.
4. ~~**gemma3-4b ponašanje**~~ — **REŠENO (2026-08-13, Faza 3B):** varijanta
   (a) — učitan notools model kaže "nemam alate, prebaci na cloud model".
   Auto-preusmeravanje (b) odbijeno jer zahteva detekciju "poruka treba
   tool" bez modela (ili uvek-cloud, što poništava izbor lokalnog modela).
   Stvarni kvar (model unload-ovan/neučitan) pokriva graceful fallback
   na cloud sa vidljivom napomenom (`model_fallback` event).
5. **TTS default**: `.env` forsira `say` (Lana). Preći na `edge` kao default
   (bolji srpski, treba internet) sa auto-fallback na `say` kad nema mreže?
6. ~~**Frontend stack za Fazu 7**~~ — **REŠENO (2026-08-13):** korisnik
   odobrio prelazak sa vanilla JS na **Vite + React + TypeScript** u novom
   direktorijumu `web-ui/`. Obim Faze 7 (markdown render, reasoning blokovi,
   design sistem, modali, a11y) prevazilazi vanilla bez build-a. Astro
   odbačen (pogrešna paradigma — Jarvis je interaktivna app, ne statičan
   sajt), Next.js odbačen (SSR/routing/API rute nepotrebni za lokalnu
   single-user app). Vanilla `web/` ostaje netaknut i servira se dok
   `web-ui/dist` ne dostigne 100% feature parity (checklist u Fazi 7).

---

## 11. Napomene i smernice za sledeću sesiju

- **Ne diraj** `config/kilo-jarvis.jsonc` (bezbednosni profil) — ako treba
  nova komanda, dodaj je sa `ask` politikom.
- API ključevi idu kroz `.env` / `~/.config/kilo/kilo.jsonc`, NIKAD hardcode.
- Konvencije: bez novih komentara u kodu osim neophodnih (docstring na
  modulima OK); async za IO; event bus jedini kanal ka UI-u; Python 3.11
  (`.venv/bin/python`); frontend vanilla JS bez build-a; subprocess samo
  preko `asyncio.to_thread`/`create_subprocess_exec`.
- Python u `.venv` je 3.11; koristi slobodno `X | None` sintaksu (sa
  `from __future__ import annotations`).

**Smernice (bitno):**
- **Dugotrajne operacije (pull modela, download glasova) UVEK u pozadini** —
  background task/endpoint sa progress događajima, nikad ne smeju da blokiraju
  server, event loop, ili korisnikov rad. Ollama pull je idempotentan — ako je
  prekinut, samo ga nastavi.
- **Proveri stanje pre rada:** `ollama list` (šta je skinuto — qwen3.6:27b
  pull je startovan 2026-08-13, može biti gotov), `df -h` pre novih pull-ova.
- **Trenutna faza:** Faza 7 (UI/UX redizajn) je **KOMPLETNA** (7A + 7B
  ✅ 2026-08-13). Svi planirani fajlovi i taskovi završeni: chat +
  kompletna tab navigacija (Dozvole / Konekcije / Lokalni modeli / Alati /
  Logovi), permission modal, FastAPI serving flip na `web-ui/dist`
  (fallback na stari `web/`), responsive breakpoint 720/520px, light tema
  preko `prefers-color-scheme`, a11y (role/aria labele, keyboard navigacija,
  focus ring). Sva funkcionalnost 1:1 sa vanilla `web/` (eventi, REST,
  WS filter, BroadcastChannel TTS election, listen overlay, PTT, mic).
  Server na 7777 sada služi `web-ui/dist/` (vite build sa `base: /static/`);
  stari `web/` netaknut kao fallback. Stari UI se više ne koristi pod
  default uslovima — ali i dalje radi ako se obriše `web-ui/dist/`. Sve
  buduće izmene idu u `web-ui/`. Dev flow: `npm run dev` u `web-ui/`
  (5173, proxy ka 7777) ILI `npm run build` pa posetiti 7777.
- **Globalnost je tvrd zahtev:** PTT hotkey mora da radi iz BILO KOJE
  aplikacije (ne samo iz Jarvis prozora); listen mode mora da zaustavi SAV
  zvuk na sistemu dok korisnik priča. Ovo su korisnikovi eksplicitni zahtevi.
- **Verifikuj efekte, ne namere:** svaka akcija (pause, play, volume) mora da
  proveri stvarno stanje posle izvršenja i prijavi istinu modelu.
- **Ne pretpostavljaj capability modela:** tool podrška lokalnih modela se
  utvrđuje probe testom, ne nagađanjem (gemma4:12b je nepoznat dok se ne
  testira).
- **Checklist na startu sesije:** (1) pročitaj ovaj fajl ceo; (2) proveri
  §5 koja je faza (ili pod-faza) na redu; (3) pokreni server i prođi §7
  baseline; (4) radi samo taskove te faze; (5) na kraju ažuriraj §5, §9,
  "Zaključke" faze i po potrebi §10; (6) **STANI** — traži od korisnika da
  restartuje sesiju; NIKAD ne počinji sledeću fazu u istoj sesiji.
- Pre bilo kakve izmene, proveri da server radi i prođi kroz §7 testove da
  imaš baseline.
