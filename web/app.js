// Jarvis control-panel frontend. Talks to FastAPI via REST + WebSocket.
//
// Speech is server-driven: the backend synthesizes each sentence once while
// the model is still generating and pushes ordered `tts_speak` events. Every
// open tab receives the event, so tabs run a lightweight BroadcastChannel
// election (lowest random token wins) to pick exactly ONE tab for playback —
// synthesis is never repeated, only playback is elected.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  ws: null,
  sessionId: null,
  currentAssistantEl: null,
  currentAssistantText: '',
  lastAssistantFinal: '',
  permissions: { default_policy: 'ask', tools: {} },
  pending: new Map(),
  autoScroll: true,
  ttsEnabled: true,
  audioUnlocked: false,
  currentModel: null,
  ptt: null,
  connections: null,
};

const TOOL_DESCRIPTIONS = {
  time_now: 'Trenutno vreme i datum.',
  reminders_create: 'Kreiraj podsetnik u Apple Reminders.',
  reminders_list: 'Listaj aktivne podsetnike.',
  calendar_today: 'Današnji događaji iz Apple kalendara.',
  open_app: 'Otvori macOS aplikaciju.',
  open_url: 'Otvori URL u browseru.',
  web_search: 'Web pretraga (DuckDuckGo).',
  read_clipboard: 'Pročitaj sistemski clipboard.',
  write_clipboard: 'Zapiši tekst u clipboard.',
  system_volume: 'Podesi zvuk sistema.',
  ytm_play: 'Pusti pesmu u YouTube Music.',
  ytm_pause: 'Pauziraj YouTube Music.',
  ytm_resume: 'Nastavi YouTube Music.',
  ytm_next: 'Sledeća pesma (YTM).',
  ytm_previous: 'Prethodna pesma (YTM).',
  ytm_volume_up: 'Pojačaj zvuk (sistemski).',
  ytm_volume_down: 'Smanji zvuk (sistemski).',
  ytm_volume_mute: 'Mute sistemskog zvuka.',
  ytm_status: 'Verifikovan status reprodukcije (šta svira + YTM).',
  play_youtube: 'Pusti YouTube video u Chrome-u.',
  kilo_run: 'Pokreni kod/terminal zadatak preko Kilo Code agenta.',
};

// Events whose `payload.session` (or `payload.id`) names the session they
// belong to. Other tabs ignore them when they're for a different session,
// so two tabs in two sessions never see each other's transcripts. The Logs
// tab renders everything regardless (raw event stream).
const SESSION_SCOPED_EVENTS = new Set([
  'assistant_start',
  'assistant_delta',
  'reasoning_delta',
  'assistant_done',
  'assistant_cancelled',
  'assistant_error',
  'tool_call',
  'tool_done',
  'tool_error',
  'session_busy',
  'session_update',
  'model_fallback',
]);

function eventSession(payload) {
  if (!payload) return null;
  return payload.session || payload.id || null;
}

const TOOL_PARAMS = {
  time_now: [],
  reminders_create: ['title', 'list', 'due_iso'],
  reminders_list: ['list', 'limit'],
  calendar_today: ['calendar'],
  open_app: ['name'],
  open_url: ['url', 'browser'],
  web_search: ['query', 'max_results'],
  read_clipboard: [],
  write_clipboard: ['text'],
  system_volume: ['level', 'mute'],
  ytm_play: ['query'],
  ytm_pause: [],
  ytm_resume: [],
  ytm_next: [],
  ytm_previous: [],
  ytm_volume_up: [],
  ytm_volume_down: [],
  ytm_volume_mute: [],
  ytm_status: [],
  play_youtube: ['query'],
  kilo_run: ['prompt', 'cwd', 'max_duration_s'],
};

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.onopen = () => {
    $('#ws-dot').classList.add('ok');
    $('#ws-text').textContent = 'konekcija živa';
  };
  state.ws.onclose = () => {
    $('#ws-dot').classList.remove('ok');
    $('#ws-dot').classList.add('err');
    $('#ws-text').textContent = 'prekinuto — pokušavam ponovo…';
    setTimeout(connect, 1500);
  };
  state.ws.onerror = () => state.ws.close();
  state.ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      handleEvent(msg);
    } catch (err) {
      console.error('bad ws msg', err);
    }
  };
}

function handleEvent(msg) {
  const { kind, payload, t } = msg;
  if (kind === 'ping' || kind === 'hello') return;

  appendLog(`[${new Date(t * 1000).toLocaleTimeString()}] ${kind} ${JSON.stringify(payload || {}).slice(0, 200)}`);

  // Drop chat events for sessions other than the one this tab owns, so two
  // tabs in different sessions never see each other's transcripts. Global
  // events (tts_*, ptt_*, listen_*, llm_retry, bus_overflow, ...) are NOT
  // filtered — the TTS playback election already handles cross-tab audio.
  if (state.sessionId && SESSION_SCOPED_EVENTS.has(kind)) {
    const target = eventSession(payload);
    if (target && target !== state.sessionId) return;
  }

  if (kind === 'assistant_start') {
    if (!ttsTurnOpen) {
      ttsTurnOpen = true;
      ttsAggEl = null;
      ttsAggCount = 0;
    }
    state.currentAssistantEl = addMsg('assistant', '');
    state.currentAssistantText = '';
    if (state.currentAssistantEl) {
      delete state.currentAssistantEl.dataset.reasoning;
      state.currentAssistantEl.classList.remove('thinking');
    }
  } else if (kind === 'assistant_delta') {
    state.currentAssistantText += payload.text;
    if (state.currentAssistantEl) {
      state.currentAssistantEl.textContent = state.currentAssistantText;
      state.currentAssistantEl.classList.remove('thinking');
    }
  } else if (kind === 'reasoning_delta') {
    if (state.currentAssistantEl) {
      if (!state.currentAssistantEl.dataset.reasoning) {
        state.currentAssistantEl.dataset.reasoning = '';
      }
      state.currentAssistantEl.dataset.reasoning += payload.text;
      state.currentAssistantEl.classList.add('thinking');
      state.currentAssistantEl.textContent = state.currentAssistantEl.dataset.reasoning;
    }
  } else if (kind === 'assistant_done') {
    const final = collapseDouble(state.currentAssistantText);
    const doneEl = state.currentAssistantEl;
    if (doneEl) doneEl.textContent = final;
    if (final && final.trim()) state.lastAssistantFinal = final;
    state.currentAssistantEl = null;
    if (payload.final) ttsTurnOpen = false;
  } else if (kind === 'assistant_cancelled') {
    if (state.currentAssistantEl) {
      state.currentAssistantEl.textContent =
        (state.currentAssistantText || '') + '  ■ (prekinuto)';
    }
    state.currentAssistantEl = null;
    ttsTurnOpen = false;
  } else if (kind === 'assistant_error') {
    addMsg('tool', `⚠ greška: ${payload.error}`);
    state.currentAssistantEl = null;
    ttsTurnOpen = false;
  } else if (kind === 'session_busy') {
    renderBusy(payload);
  } else if (kind === 'tool_call') {
    addMsg('tool', `→ ${payload.tool}(${JSON.stringify(payload.args)})`);
  } else if (kind === 'tool_done') {
    if (payload.denied) addMsg('tool', `⛔ ${payload.tool} odbijen`);
  } else if (kind === 'tool_error') {
    addMsg('tool', `⚠ ${payload.tool}: ${payload.error}`);
  } else if (kind === 'permission_request') {
    showPermissionModal(payload);
  } else if (kind === 'permissions_changed') {
    state.permissions = payload;
    renderPermissions();
  } else if (kind === 'kilo_start') {
    addMsg('tool', `▶ kilo: ${(payload.prompt || '').slice(0, 80)}…`);
  } else if (kind === 'kilo_done') {
    addMsg('tool', `■ kilo ok=${payload.ok} exit=${payload.exit} elapsed=${payload.elapsed?.toFixed?.(1) ?? payload.elapsed}s`);
  } else if (kind === 'recording_start') {
    $('#mic').classList.add('recording');
  } else if (kind === 'recording_end') {
    $('#mic').classList.remove('recording');
  } else if (kind === 'listen_enter') {
    $('#mic').classList.add('listening');
    setListenBadge(`🎙 slušam… (${(payload.reasons || []).join('+') || 'mic'})`);
  } else if (kind === 'listen_exit') {
    $('#mic').classList.remove('listening');
    clearListenBadge();
  } else if (kind === 'ptt_recording_start') {
    $('#mic').classList.add('recording');
    addMsg('tool', '🎙 PTT snima…');
  } else if (kind === 'ptt_recording_end') {
    $('#mic').classList.remove('recording');
  } else if (kind === 'whisper_loading') {
    addMsg('tool', `… učitavam Whisper (${payload.model})`);
  } else if (kind === 'whisper_ready') {
    addMsg('tool', `✓ Whisper spreman (${payload.device})`);
  } else if (kind === 'whisper_result') {
    addMsg('tool', `✓ STT (${payload.language}): "${(payload.text || '').slice(0, 120)}"`);
  } else if (kind === 'tts_speak') {
    enqueueSpeech(payload);
  } else if (kind === 'tts_stop') {
    stopSpeech();
  } else if (kind === 'tts_done') {
    ttsAggCount += 1;
    const text = `✓ TTS ×${ttsAggCount} (${payload.engine})`;
    if (ttsAggEl && ttsAggEl.isConnected) {
      ttsAggEl.textContent = text;
    } else {
      ttsAggEl = addMsg('tool', text);
    }
  } else if (kind === 'tts_error') {
    addMsg('tool', `⚠ TTS: ${payload.error}`);
  } else if (kind === 'tts_fallback') {
    addMsg('tool', `⚠ TTS ${payload.engine} nije uspeo (${(payload.error || '').slice(0, 80)}) — prelazim na say`);
  } else if (kind === 'llm_retry') {
    addMsg('tool', `… LLM pokušaj ${payload.attempt + 1} posle ${(payload.reason || 'greške')} (čekam ${payload.delay}s)`);
  } else if (kind === 'bus_overflow') {
    addMsg('tool', `⚠ red događaja prepun — najstariji događaji odbačeni`);
  } else if (kind === 'local_model_loading') {
    addMsg('tool', `… učitavam lokalni model: ${payload.id}`);
    } else if (kind === 'local_model_pulling') {
      updatePullProgress(payload);
    } else if (kind === 'local_model_ready') {
    addMsg('tool', `✓ lokalni model učitan: ${payload.loaded_id}`);
    loadLocalModels().then(refreshModelDropdown);
  } else if (kind === 'local_model_unloaded') {
    addMsg('tool', `■ lokalni model oslobođen iz RAM-a`);
    loadLocalModels().then(refreshModelDropdown);
  } else if (kind === 'local_model_error') {
    addMsg('tool', `⚠ lokalni model: ${payload.error || 'load failed'}`);
    loadLocalModels();
  } else if (kind === 'model_fallback') {
    const name = (payload.model || '').replace(/^local:/, '');
    addMsg('tool', `⚠ lokalni model ${name} nije dostupan (${payload.reason || 'greška'}) — odgovaram cloud modelom`);
  } else if (kind === 'voice_ptt_transcribed') {
    if (!payload.ok) {
      addMsg('tool', `⚠ PTT: ${payload.error || 'transcribe failed'}`);
      return;
    }
    if (payload.skipped === 'empty') {
      addMsg('tool', `… PTT: ništa nisam čuo`);
      return;
    }
    const text = payload.text || '';
    if (!text) return;
    if (payload.auto_send) {
      addMsg('user', '🎙 ' + text);
      sendText(text, { interrupt: true });
    } else {
      const ta = $('#input');
      ta.value = (ta.value ? ta.value + ' ' : '') + text;
      ta.focus();
      ta.style.height = 'auto';
      ta.style.height = Math.min(200, ta.scrollHeight) + 'px';
      addMsg('user', '🎙 ' + text);
    }
  } else if (kind === 'ptt_status') {
    state.ptt = payload;
    renderPtt();
  }
}

function renderBusy(payload) {
  const stopBtn = $('#stop');
  const busyText = $('#busy-text');
  stopBtn.disabled = !payload.busy;
  if (payload.busy) {
    busyText.textContent = payload.queued > 0 ? `… razmišljam (+${payload.queued} u redu)` : '… razmišljam';
  } else {
    busyText.textContent = '';
  }
}

function addMsg(role, text) {
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  el.textContent = text;
  $('#transcript').appendChild(el);
  if (state.autoScroll) $('#transcript').scrollTop = $('#transcript').scrollHeight;
  return el;
}

// Collapse exact whole-text doubles and verbatim duplicate paragraphs
// (defensive; the server already collapses MiniMax duplicates).
function collapseDouble(s) {
  if (!s || s.length < 4) return s;
  if (s.length % 2 === 0) {
    const half = s.length / 2;
    if (s.slice(0, half) === s.slice(half)) return collapseDouble(s.slice(0, half));
  }
  const paragraphs = s.split(/\n\s*\n/);
  if (paragraphs.length > 1) {
    const seen = new Set();
    const out = [];
    for (const p of paragraphs) {
      const key = p.trim();
      if (key) {
        if (seen.has(key)) continue;
        seen.add(key);
      }
      out.push(p);
    }
    const joined = out.join('\n\n');
    if (joined !== s) return joined;
  }
  return s;
}

// ---- speech playback (server-driven) ---------------------------------------

let ttsTurnOpen = false;
let ttsAggEl = null;
let ttsAggCount = 0;

const speech = {
  queue: [],
  audio: null,
  token: 0,
  pumping: false,
};

const TAB_ID = Math.random().toString(36).slice(2) + Date.now().toString(36);
const speechChannel = ('BroadcastChannel' in window) ? new BroadcastChannel('jarvis-speech') : null;
const speechClaims = new Map();

if (speechChannel) {
  speechChannel.onmessage = (ev) => {
    const m = ev.data || {};
    if (m.type !== 'claim') return;
    const c = speechClaims.get(m.id);
    if (!c) return;
    if (m.token < c.token || (m.token === c.token && (m.tab || '') < TAB_ID)) c.win = false;
  };
}

function enqueueSpeech(p) {
  if (!state.ttsEnabled) return;
  if (p.server_played) return;
  if (!speechChannel) {
    speech.queue.push(p);
    pumpSpeech();
    return;
  }
  const id = p.url;
  const myToken = Math.random().toString(36).slice(2);
  const stopToken = speech.token;
  speechClaims.set(id, { token: myToken, win: true });
  speechChannel.postMessage({ type: 'claim', id, token: myToken, tab: TAB_ID });
  setTimeout(() => {
    const c = speechClaims.get(id);
    speechClaims.delete(id);
    if (c && c.win && speech.token === stopToken) {
      speech.queue.push(p);
      pumpSpeech();
    }
  }, 150);
}

async function pumpSpeech() {
  if (speech.pumping) return;
  speech.pumping = true;
  const myToken = speech.token;
  try {
    while (speech.queue.length > 0 && speech.token === myToken) {
      const item = speech.queue.shift();
      await playAudioFile(item.url);
    }
  } finally {
    if (speech.token === myToken) speech.pumping = false;
  }
}

function playAudioFile(url) {
  return new Promise((resolve) => {
    const a = new Audio(url);
    speech.audio = a;
    let settled = false;
    const done = () => {
      if (settled) return;
      settled = true;
      if (speech.audio === a) speech.audio = null;
      resolve();
    };
    a.onended = done;
    a.onpause = done;
    a.onerror = done;
    unlockAudio();
    a.play().catch(done);
  });
}

function stopSpeech() {
  speech.token++;
  speech.queue.length = 0;
  speech.pumping = false;
  if (speech.audio) {
    try { speech.audio.pause(); } catch (e) { /* ignore */ }
    speech.audio = null;
  }
}

function unlockAudio() {
  if (state.audioUnlocked) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    if (ctx.state === 'suspended') ctx.resume();
    const buf = ctx.createBuffer(1, 1, 22050);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start(0);
    state.audioUnlocked = true;
  } catch (e) { /* ignore */ }
}

document.addEventListener('pointerdown', unlockAudio, { capture: true });
document.addEventListener('keydown', unlockAudio, { capture: true });

function addSpeakButton(el, text) {
  const btn = document.createElement('button');
  btn.className = 'speak-btn';
  btn.textContent = '▶ Slušaj';
  btn.onclick = () => { unlockAudio(); speakManual(text); };
  el.appendChild(btn);
}

// Manual replay (voice test button, ▶ Slušaj): synthesize once, play here.
async function speakManual(text) {
  if (!text || !text.trim()) return;
  try {
    const r = await fetch('/api/audio/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      const msg = err.detail || err.error || err.message || `HTTP ${r.status}`;
      addMsg('tool', `⚠ TTS (${r.status}): ${msg}`);
      return;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    a.onended = () => URL.revokeObjectURL(url);
    unlockAudio();
    try {
      await a.play();
    } catch (err) {
      URL.revokeObjectURL(url);
      addMsg('tool', `⚠ TTS: browser blokira auto-play — klikni ▶ Slušaj`);
    }
  } catch (err) {
    addMsg('tool', `⚠ TTS: ${err.message}`);
  }
}

function appendLog(line) {
  const el = $('#log-stream');
  el.textContent += line + '\n';
  if (el.childElementCount === 0 && el.textContent.length > 120000) {
    el.textContent = el.textContent.slice(-60000);
  }
  if (state.autoScroll) el.scrollTop = el.scrollHeight;
}

// ---- chat send -------------------------------------------------------------

async function sendText(text, opts = {}) {
  stopSpeech();
  const r = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text,
      session_id: state.sessionId,
      model: state.currentModel || null,
      interrupt: !!opts.interrupt,
    }),
  });
  const data = await r.json();
  state.sessionId = data.session_id;
  refreshSessions();
}

async function send() {
  const ta = $('#input');
  const text = ta.value.trim();
  if (!text) return;
  ta.value = '';
  ta.style.height = 'auto';
  addMsg('user', text);
  await sendText(text, { interrupt: false });
}

async function stopTurn() {
  if (!state.sessionId) return;
  stopSpeech();
  await fetch('/api/chat/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: state.sessionId }),
  });
}

// ---- mic -------------------------------------------------------------------

let mediaRecorder = null;
let micChunks = [];

async function toggleMic() {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream, { mimeType: pickMime() });
    micChunks = [];
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) micChunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      const blob = new Blob(micChunks, { type: mediaRecorder.mimeType });
      stream.getTracks().forEach(t => t.stop());
      $('#mic').classList.remove('recording');
      fetch('/api/audio/listen/stop', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: 'browser' }),
      }).catch(() => {});
      const fd = new FormData();
      fd.append('audio', blob, 'mic.webm');
      const r = await fetch('/api/audio/stt', { method: 'POST', body: fd });
      const data = await r.json();
      if (data.ok && data.text) {
        addMsg('user', '🎙 ' + data.text);
        await sendText(data.text, { interrupt: true });
      } else if (!data.ok) {
        addMsg('tool', `⚠ STT greška: ${data.error}`);
      }
    };
    fetch('/api/audio/listen/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'browser' }),
    }).catch(() => {});
    mediaRecorder.start();
    stopSpeech();
    $('#mic').classList.add('recording');
  } catch (err) {
    addMsg('tool', `⚠ mikrofon: ${err.message}`);
    fetch('/api/audio/listen/stop', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'browser' }),
    }).catch(() => {});
  }
}

function setListenBadge(text) {
  const mic = $('#mic');
  if (!mic) return;
  let badge = mic.querySelector('.listen-badge');
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'listen-badge';
    mic.appendChild(badge);
  }
  badge.textContent = text;
}
function clearListenBadge() {
  const mic = $('#mic');
  if (!mic) return;
  const badge = mic.querySelector('.listen-badge');
  if (badge) badge.remove();
}

function pickMime() {
  for (const m of ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']) {
    if (MediaRecorder.isTypeSupported(m)) return m;
  }
  return '';
}

// ---- sessions --------------------------------------------------------------

async function refreshSessions() {
  const r = await fetch('/api/sessions');
  const list = await r.json();
  const ul = $('#sessions-list');
  ul.innerHTML = '';
  for (const s of list) {
    const li = document.createElement('li');
    if (s.id === state.sessionId) li.classList.add('active');
    const title = document.createElement('span');
    title.className = 'session-title';
    title.textContent = s.title || s.id;
    title.onclick = () => loadSession(s.id);
    li.appendChild(title);
    const del = document.createElement('button');
    del.className = 'session-del';
    del.textContent = '×';
    del.title = 'Obriši sesiju';
    del.onclick = async (ev) => {
      ev.stopPropagation();
      const ok = confirm(`Obriši sesiju "${s.title || s.id}"?`);
      if (!ok) return;
      const rr = await fetch(`/api/sessions/${encodeURIComponent(s.id)}`, { method: 'DELETE' });
      if (rr.ok) {
        if (state.sessionId === s.id) {
          state.sessionId = null;
          $('#transcript').innerHTML = '';
          stopSpeech();
        }
        refreshSessions();
      }
    };
    li.appendChild(del);
    ul.appendChild(li);
  }
}

async function loadSession(id) {
  state.sessionId = id;
  stopSpeech();
  $('#transcript').innerHTML = '';
  const r = await fetch(`/api/sessions/${id}`);
  const data = await r.json();
  for (const m of data.messages || []) {
    if (m.role === 'user') addMsg('user', m.content);
    else if (m.role === 'assistant') addMsg('assistant', m.content || '');
    else if (m.role === 'tool') addMsg('tool', `${m.name}: ${m.content}`);
  }
  refreshSessions();
}

async function newSession() {
  stopSpeech();
  const r = await fetch('/api/sessions', { method: 'POST' });
  const data = await r.json();
  state.sessionId = data.id;
  $('#transcript').innerHTML = '';
  refreshSessions();
}

// ---- permissions -----------------------------------------------------------

async function loadPermissions() {
  const r = await fetch('/api/permissions');
  state.permissions = await r.json();
  $('#perm-default').value = state.permissions.default_policy || 'ask';
  renderPermissions();
}

function renderPermissions() {
  const tbody = $('#perm-tbody');
  tbody.innerHTML = '';
  const tools = state.permissions.tools || {};
  const known = Object.keys(TOOL_DESCRIPTIONS);
  const all = Array.from(new Set([...known, ...Object.keys(tools)])).sort();
  for (const name of all) {
    const tr = document.createElement('tr');
    const desc = TOOL_DESCRIPTIONS[name] || '—';
    const policy = tools[name] || state.permissions.default_policy || 'ask';
    tr.innerHTML = `
      <td><code>${name}</code></td>
      <td class="desc">${desc}</td>
      <td>
        <select data-tool="${name}">
          <option value="allow"${policy === 'allow' ? ' selected' : ''}>allow</option>
          <option value="ask"${policy === 'ask' ? ' selected' : ''}>ask</option>
          <option value="deny"${policy === 'deny' ? ' selected' : ''}>deny</option>
        </select>
      </td>
      <td><button data-reset="${name}">reset</button></td>
    `;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll('select[data-tool]').forEach(sel => {
    sel.onchange = async () => {
      await fetch('/api/permissions', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tools: { [sel.dataset.tool]: sel.value } }),
      });
    };
  });
  tbody.querySelectorAll('button[data-reset]').forEach(btn => {
    btn.onclick = async () => {
      await fetch('/api/permissions', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tools: { [btn.dataset.reset]: state.permissions.default_policy } }),
      });
    };
  });
}

async function saveDefaultPolicy() {
  await fetch('/api/permissions', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ default_policy: $('#perm-default').value }),
  });
}

// ---- permission modal ------------------------------------------------------

function showPermissionModal(req) {
  state.pending.set(req.request_id, req);
  $('#perm-modal').classList.remove('hidden');
  $('#perm-modal-tool').textContent = req.tool;
  $('#perm-modal-args').textContent = JSON.stringify(req.args, null, 2);
  $('#perm-modal-allow').onclick = () => resolvePerm(req.request_id, 'allow');
  $('#perm-modal-deny').onclick = () => resolvePerm(req.request_id, 'deny');
}

async function resolvePerm(id, action) {
  $('#perm-modal').classList.add('hidden');
  await fetch('/api/permissions/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: id, action, remember: $('#perm-modal-remember').checked }),
  });
  state.pending.delete(id);
}

// ---- connections -----------------------------------------------------------

async function loadConnections() {
  try {
    const r = await fetch('/api/connections');
    const c = await r.json();
    state.connections = c;
    $('#conn-mm-url').textContent = c.llm.base_url;
    $('#conn-mm-model').textContent = c.llm.model + (c.llm.small_model ? '  /  small=' + c.llm.small_model : '');
    $('#conn-mm-key').textContent = c.llm.api_key_set ? '✓ postavljen' : '⚠ nije postavljen';
    $('#model-text').textContent = `model: ${c.llm.model}`;
    $('#conn-kilo-bin').textContent = c.kilo.bin;
    $('#conn-kilo-cfg').textContent = c.kilo.config_path + (c.kilo.config_exists ? '' : '  ⚠ nedostaje');
    $('#conn-kilo-ok').textContent = c.kilo.available ? '✓' : '✗ npm install -g @kilocode/cli';
    $('#conn-whisper').textContent = `${c.whisper.model} (${c.whisper.device}, ${c.whisper.compute})`;
    $('#conn-piper').textContent = `${c.tts.piper.voice}  ·  fallback say: ${c.tts.piper.say_voice || 'auto'}`;
    state.ptt = c.ptt || null;
    renderPtt();
  } catch (e) {
    addMsg('tool', `⚠ konekcije: ${e.message}`);
  }
}

async function renderPtt() {
  const ptt = state.ptt;
  if (!ptt) return;
  $('#ptt-key').textContent = ptt.key || '—';
  const lm = (state.connections && state.connections.listen && state.connections.listen.mode) || 'pause';
  $('#ptt-mute').textContent = `listen: ${lm}`;
  $('#ptt-autosend').textContent = ptt.auto_send
    ? 'uključen — transkript se odmah šalje'
    : 'isključen — transkript ide u input';
  const pttTail = ptt.auto_send
    ? 'transkript se odmah šalje kao poruka.'
    : 'transkript ide u input polje.';
  if (ptt.enabled) {
    $('#ptt-status').textContent = '● ACTIVE — drži taster da snimaš';
    $('#ptt-toggle').textContent = 'Isključi';
    if (ptt.no_events_yet) {
      $('#ptt-hint').innerHTML =
        '⚠ Listener aktivan ali ne dobija evente već >60s. ' +
        'Otvori <b>System Settings → Privacy & Security → Accessibility</b> i ' +
        'daj dozvolu procesu koji je pokrenuo Jarvis ' +
        '(obično Terminal / iTerm / Python — pogledaj u Task Manager koji PID ima <code>jarvis serve</code>). ' +
        'Bez dozvole pynput tiho guta key evente.';
    } else {
      $('#ptt-hint').textContent = `Drži taster bilo gde na macOS-u. Kad pustiš, muzika se pauzira dok snimaš, pa nastavlja; ${pttTail}`;
    }
  } else {
    $('#ptt-status').textContent = '○ ugašen';
    $('#ptt-toggle').textContent = 'Uključi';
    if (ptt.error) {
      $('#ptt-hint').textContent = `Greška: ${ptt.error} — verovatno treba Accessibility dozvola (System Settings → Privacy & Security → Accessibility).`;
    } else {
      $('#ptt-hint').textContent = 'PTT radi BILO GDE na macOS-u (nije samo u Jarvis prozoru). Drži taster da snimiš, pusti da transkribuje.';
    }
  }
}

async function refreshPtt() {
  try {
    const r = await fetch('/api/ptt');
    const ptt = await r.json();
    state.ptt = ptt;
    renderPtt();
  } catch (e) { /* ignore */ }
}
setInterval(() => { if (state.ptt && state.ptt.enabled) refreshPtt(); }, 30000);

async function togglePtt() {
  const ptt = state.ptt || {};
  const url = ptt.enabled ? '/api/ptt/disable' : '/api/ptt/enable';
  const r = await fetch(url, { method: 'POST' });
  const data = await r.json();
  if (!data.ok) {
    addMsg('tool', `⚠ PTT: ${data.error || 'switch failed'}`);
  }
  state.ptt = data.ptt || ptt;
  renderPtt();
}

$('#ptt-toggle').onclick = togglePtt;

async function loadPersistedUI() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    const ui = d.ui || {};
    if (ui.tts_enabled === false) {
      state.ttsEnabled = false;
      $('#tts-toggle').textContent = '🔇';
      $('#tts-toggle').title = 'Jarvis glas: isključen';
    }
    return ui.model || null;
  } catch (e) {
    return null;
  }
}

function persistUI(patch) {
  fetch('/api/state', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  }).catch(() => {});
}

async function ensureLocalLoaded(modelId) {
  addMsg('tool', `… učitavam lokalni model ${modelId} u RAM`);
  try {
    const rr = await fetch('/api/local_models/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: modelId }),
    });
    const data = await rr.json();
    if (!data.ok) addMsg('tool', `⚠ load: ${data.error}`);
  } catch (e) {
    addMsg('tool', `⚠ load: ${e.message}`);
  }
}

async function loadModels() {
  const r = await fetch('/api/models');
  const m = await r.json();
  const persisted = await loadPersistedUI();
  const sel = $('#model-select');
  sel.innerHTML = '';
  for (const mdl of m.available) {
    const opt = document.createElement('option');
    opt.value = mdl.id;
    opt.textContent = mdl.label;
    sel.appendChild(opt);
  }
  if (persisted && [...sel.options].some((o) => o.value === persisted)) {
    sel.value = persisted;
  } else if (m.current) {
    sel.value = m.current;
  }
  sel.onchange = onModelChange;
  state.currentModel = sel.value;
  if (state.currentModel && state.currentModel.startsWith('local:')) {
    ensureLocalLoaded(state.currentModel.slice('local:'.length));
  }
}

async function onModelChange() {
  const sel = $('#model-select');
  state.currentModel = sel.value;
  persistUI({ model: sel.value });
  if (!state.currentModel.startsWith('local:')) return;
  await ensureLocalLoaded(state.currentModel.slice('local:'.length));
}

async function refreshModelDropdown() {
  try {
    const r = await fetch('/api/models');
    const m = await r.json();
    const sel = $('#model-select');
    const prev = state.currentModel || sel.value;
    sel.innerHTML = '';
    for (const mdl of m.available) {
      const opt = document.createElement('option');
      opt.value = mdl.id;
      opt.textContent = mdl.label;
      sel.appendChild(opt);
    }
    if (prev && [...sel.options].some((o) => o.value === prev)) {
      sel.value = prev;
    } else if (m.current) {
      sel.value = m.current;
    }
    sel.onchange = onModelChange;
    state.currentModel = sel.value;
  } catch (e) {
    addMsg('tool', `⚠ modeli: ${e.message}`);
  }
}

// ---- Local models panel ----------------------------------------------------

function fmtSize(bytes) {
  if (!bytes) return '—';
  const gb = bytes / (1024 ** 3);
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

function capabilityBadge(cap) {
  if (cap === 'tools') return `<span class="ok">✓ tool-ovi</span>`;
  if (cap === 'notools') return `<span class="warn" title="Model ne podržava function calling — ne može da izvršava akcije">⚠ bez tool-ova</span>`;
  return `<span class="subtle">?</span>`;
}

async function loadLocalModels() {
  try {
    const r = await fetch('/api/local_models');
    const d = await r.json();
    const runner = d.runner || {};
    const models = d.models || [];
    $('#local-engine-missing').classList.toggle('hidden', !!runner.engine_available);
    const tbody = $('#local-models-tbody');
    tbody.innerHTML = '';
    if (!models.length) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td colspan="6" class="subtle">Nema modela na disku. Ollama daemon nije dostupan ili nijedan model nije skinut — koristi pull ispod.</td>`;
      tbody.appendChild(tr);
    }
    for (const m of models) {
      const tr = document.createElement('tr');
      const isLoaded = runner.state === 'ready' && runner.loaded_id === m.id;
      const isLoading = runner.state === 'loading' && runner.loaded_id === m.id;
      const status = isLoaded
        ? `<span class="ok">● učitan u RAM</span>`
        : isLoading
          ? `<span class="subtle">… učitavam</span>`
          : m.in_ram
            ? `<span class="subtle">● u RAM (Ollama)</span>`
            : `<span class="subtle">○ na disku</span>`;
      const action = isLoaded
        ? `<button data-unload>Oslobodi iz RAM-a</button>`
        : `<button class="primary" data-load="${m.id}">Učitaj u RAM</button>`;
      tr.innerHTML = `
        <td><code>${m.id}</code></td>
        <td class="desc"><code>${m.tag}</code> <span class="subtle">ctx ${m.n_ctx} · keep ${m.keep_alive}</span></td>
        <td>${fmtSize(m.size)}</td>
        <td>${capabilityBadge(m.capability)}</td>
        <td>${status}</td>
        <td>${action}</td>
      `;
      tbody.appendChild(tr);
    }
    tbody.querySelectorAll('button[data-load]').forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = '… učitavam';
        try {
          const rr = await fetch('/api/local_models/load', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_id: btn.dataset.load }),
          });
          const data = await rr.json();
          if (!data.ok) addMsg('tool', `⚠ load: ${data.error}`);
        } catch (e) {
          addMsg('tool', `⚠ load: ${e.message}`);
        }
      };
    });
    tbody.querySelectorAll('button[data-unload]').forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await fetch('/api/local_models/unload', { method: 'POST' });
        } catch (e) {
          addMsg('tool', `⚠ unload: ${e.message}`);
        }
      };
    });
    renderPulls(d.pulls || []);
  } catch (e) {
    addMsg('tool', `⚠ lokalni modeli: ${e.message}`);
  }
}

function renderPulls(pulls) {
  const box = $('#pull-list');
  if (!box) return;
  box.innerHTML = '';
  for (const p of pulls) {
    const row = document.createElement('div');
    row.className = 'pull-row';
    row.dataset.tag = p.tag;
    const pct = Number(p.percent || 0).toFixed(0);
    let label = '';
    if (p.status === 'starting') label = 'pokrećem…';
    else if (p.status === 'progress') label = `${pct}% ${p.detail || ''}`;
    else if (p.status === 'done') label = '✓ skinuto';
    else if (p.status === 'error') label = `⚠ greška: ${p.detail || ''}`;
    else if (p.status === 'cancelled') label = 'otkazano';
    row.innerHTML = `
      <div class="pull-head"><code>${p.tag}</code> <span class="subtle">${label}</span></div>
      <div class="pull-bar"><div class="pull-fill" style="width:${pct}%"></div></div>
      ${p.status === 'starting' || p.status === 'progress'
        ? `<button data-cancel-pull="${p.tag}">Otkaži</button>` : ''}
    `;
    box.appendChild(row);
  }
  box.querySelectorAll('button[data-cancel-pull]').forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await fetch('/api/local_models/pull/cancel', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tag: btn.dataset.cancelPull }),
        });
      } catch (e) {
        addMsg('tool', `⚠ cancel pull: ${e.message}`);
      }
    };
  });
}

function updatePullProgress(payload) {
  const box = $('#pull-list');
  if (!box) return;
  let row = box.querySelector(`.pull-row[data-tag="${CSS.escape(payload.tag)}"]`);
  if (!row) {
    loadLocalModels();
    return;
  }
  const pct = Number(payload.percent || 0).toFixed(0);
  let label = '';
  if (payload.status === 'starting') label = 'pokrećem…';
  else if (payload.status === 'progress') label = `${pct}% ${payload.detail || ''}`;
  else if (payload.status === 'done') label = '✓ skinuto';
  else if (payload.status === 'error') label = `⚠ greška: ${payload.detail || ''}`;
  else if (payload.status === 'cancelled') label = 'otkazano';
  const head = row.querySelector('.pull-head .subtle');
  if (head) head.textContent = label;
  const fill = row.querySelector('.pull-fill');
  if (fill) fill.style.width = `${pct}%`;
  if (['done', 'error', 'cancelled'].includes(payload.status)) {
    const btn = row.querySelector('button[data-cancel-pull]');
    if (btn) btn.remove();
    if (payload.status === 'done') loadLocalModels().then(refreshModelDropdown);
  }
}

async function startPull() {
  const input = $('#pull-tag');
  const tag = (input.value || '').trim();
  if (!tag) return;
  const btn = $('#pull-start');
  btn.disabled = true;
  try {
    const r = await fetch('/api/local_models/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag }),
    });
    const data = await r.json();
    if (!data.ok) addMsg('tool', `⚠ pull: ${data.error}`);
    else input.value = '';
  } catch (e) {
    addMsg('tool', `⚠ pull: ${e.message}`);
  } finally {
    btn.disabled = false;
    loadLocalModels();
  }
}

// ---- TTS voice selector ----------------------------------------------------

async function loadVoices() {
  try {
    const r = await fetch('/api/tts/voices');
    const data = await r.json();
    const sel = $('#voice-select');
    sel.innerHTML = '';
    const groups = [
      { id: 'say', label: 'macOS (Siri, offline)' },
      { id: 'edge', label: 'Microsoft Edge (online, najbolji srpski)' },
      { id: 'azure', label: 'Azure Speech (online)' },
      { id: 'elevenlabs', label: 'ElevenLabs (premium multilingual)' },
      { id: 'piper', label: 'Piper (offline, robot)' },
      { id: 'xtts', label: 'Coqui XTTS (kloniranje glasa)' },
    ];
    for (const g of groups) {
      const list = (data.voices || {})[g.id] || [];
      if (!list.length) continue;
      const og = document.createElement('optgroup');
      og.label = g.label;
      for (const v of list) {
        const opt = document.createElement('option');
        opt.value = `${g.id}:${v.id}`;
        opt.textContent = v.label;
        opt.dataset.backend = g.id;
        if (g.id === data.backend && v.id === data.voice) opt.selected = true;
        og.appendChild(opt);
      }
      sel.appendChild(og);
    }
    if (sel.value) {
      const [be, vo] = selValueParts();
      $('#voice-test').title = `Proba: ${be} / ${vo}`;
    }
  } catch (e) {
    addMsg('tool', `⚠ voices: ${e.message}`);
  }
}

async function setVoiceAndTest(next) {
  const [backend, voice] = selValueParts();
  try {
    const r = await fetch('/api/audio/tts/voice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ backend, voice }),
    });
    const data = await r.json();
    if (!data.ok) {
      addMsg('tool', `⚠ glas: ${data.error}`);
      return;
    }
    if (next && voice) speakManual(next);
  } catch (e) {
    addMsg('tool', `⚠ glas: ${e.message}`);
  }
}

function selValueParts() {
  const val = $('#voice-select').value || '';
  const idx = val.indexOf(':');
  if (idx < 0) return [val, ''];
  return [val.slice(0, idx), val.slice(idx + 1)];
}

// ---- tools tab -------------------------------------------------------------

function renderTools() {
  const tbody = $('#tools-tbody');
  tbody.innerHTML = '';
  for (const name of Object.keys(TOOL_DESCRIPTIONS)) {
    const tr = document.createElement('tr');
    const params = (TOOL_PARAMS[name] || []).map(p => `<code>${p}</code>`).join(', ');
    tr.innerHTML = `
      <td><code>${name}</code></td>
      <td class="desc">${TOOL_DESCRIPTIONS[name]}</td>
      <td>${params || '—'}</td>
    `;
    tbody.appendChild(tr);
  }
}

// ---- tabs ------------------------------------------------------------------

$$('.tabs button').forEach(btn => {
  btn.onclick = () => {
    $$('.tabs button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    $$('.tab').forEach(t => t.classList.remove('active'));
    $(`#tab-${btn.dataset.tab}`).classList.add('active');
    if (btn.dataset.tab === 'permissions') loadPermissions();
    if (btn.dataset.tab === 'connections') loadConnections();
    if (btn.dataset.tab === 'local-models') loadLocalModels();
  };
});

$('#local-refresh').onclick = () => loadLocalModels().then(refreshModelDropdown);
const pullStartBtn = $('#pull-start');
if (pullStartBtn) pullStartBtn.onclick = startPull;
const pullTagInput = $('#pull-tag');
if (pullTagInput) pullTagInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') startPull(); });

// ---- composer --------------------------------------------------------------

$('#send').onclick = send;
$('#stop').onclick = stopTurn;
$('#input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  e.target.style.height = 'auto';
  e.target.style.height = Math.min(200, e.target.scrollHeight) + 'px';
});

$('#new-session').onclick = newSession;
$('#mic').onclick = toggleMic;
$('#perm-default-save').onclick = saveDefaultPolicy;
$('#autoscroll').onchange = (e) => state.autoScroll = e.target.checked;
$('#logs-clear').onclick = () => { $('#log-stream').textContent = ''; };
$('#voice-select').onchange = () => setVoiceAndTest();
$('#voice-test').onclick = () => {
  const be = selValueParts()[0];
  const demo = {
    edge: 'Ovaj glas dolazi sa Microsoft Edge servisa preko interneta.',
    azure: 'Ovaj glas koristi Azure govorni servis, najprirodniji je za srpski.',
    elevenlabs: 'Ovaj glas je sa ElevenLabs platforme, premium kvalitet i podržava srpski.',
    piper: 'Ovaj glas je robotski, ali radi potpuno bez interneta.',
    xtts: 'Ovaj glas je kloniran preko Coqui XTTS modela, lokalno i prirodno.',
  }[be] || 'Ovaj glas je sa Apple Siri motora, potpuno lokalno i besplatno.';
  setVoiceAndTest(demo);
};
$('#tts-toggle').onclick = () => {
  state.ttsEnabled = !state.ttsEnabled;
  if (!state.ttsEnabled) stopSpeech();
  $('#tts-toggle').textContent = state.ttsEnabled ? '🔊' : '🔇';
  $('#tts-toggle').title = state.ttsEnabled ? 'Jarvis glas: uključen' : 'Jarvis glas: isključen';
  persistUI({ tts_enabled: state.ttsEnabled });
};
$('#tts-server').onclick = async () => {
  const final = state.lastAssistantFinal || '';
  if (!final.trim()) return;
  try {
    await fetch('/api/audio/tts/play', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: final, force: true }),
    });
  } catch (e) {
    addMsg('tool', `⚠ TTS: ${e.message}`);
  }
};

document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.altKey && e.code === 'Space') {
    e.preventDefault();
    toggleMic();
  }
});

// boot
connect();
loadConnections();
loadModels();
loadLocalModels().then(refreshModelDropdown);
loadVoices();
renderTools();
refreshSessions();
