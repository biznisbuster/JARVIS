import { store } from '../store';
import type { TtsSpeakPayload } from './types';

const TAB_ID = Math.random().toString(36).slice(2) + Date.now().toString(36);
const channel: BroadcastChannel | null =
  'BroadcastChannel' in window ? new BroadcastChannel('jarvis-speech') : null;
const claims = new Map<string, { token: string; win: boolean }>();
const queue: TtsSpeakPayload[] = [];

let audio: HTMLAudioElement | null = null;
let stopToken = 0;
let pumping = false;
let unlocked = false;

if (channel) {
  channel.onmessage = (ev: MessageEvent) => {
    const m = (ev.data || {}) as { type?: string; id?: string; token?: string; tab?: string };
    if (m.type !== 'claim' || !m.id) return;
    const c = claims.get(m.id);
    if (!c) return;
    if ((m.token ?? '') < c.token || (m.token === c.token && (m.tab || '') < TAB_ID)) c.win = false;
  };
}

export function unlockAudio(): void {
  if (unlocked) return;
  try {
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ctx = new Ctx();
    if (ctx.state === 'suspended') void ctx.resume();
    const buf = ctx.createBuffer(1, 1, 22050);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start(0);
    unlocked = true;
  } catch {
    /* ignore */
  }
}

export function enqueueSpeech(p: TtsSpeakPayload): void {
  if (!store.state.ttsEnabled) return;
  if (p.server_played) return;
  if (!channel) {
    queue.push(p);
    void pump();
    return;
  }
  const id = p.url;
  const myToken = Math.random().toString(36).slice(2);
  const stopAt = stopToken;
  claims.set(id, { token: myToken, win: true });
  channel.postMessage({ type: 'claim', id, token: myToken, tab: TAB_ID });
  setTimeout(() => {
    const c = claims.get(id);
    claims.delete(id);
    if (c && c.win && stopToken === stopAt) {
      queue.push(p);
      void pump();
    }
  }, 150);
}

async function pump(): Promise<void> {
  if (pumping) return;
  pumping = true;
  const myToken = stopToken;
  try {
    while (queue.length > 0 && stopToken === myToken) {
      const item = queue.shift();
      if (item) await playAudioFile(item.url, item.text || '');
    }
  } finally {
    if (stopToken === myToken) pumping = false;
  }
}

async function playThroughServer(text: string): Promise<boolean> {
  if (!text.trim()) return false;
  try {
    const response = await fetch('/api/audio/tts/play', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, force: true }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    store.addTool('⚠ Browser je blokirao TTS — odgovor puštam preko sistema.');
    return true;
  } catch (err) {
    store.addTool(`⚠ TTS: browser i sistemska reprodukcija nisu uspeli (${(err as Error).message})`);
    return false;
  }
}

function playAudioFile(url: string, text: string): Promise<void> {
  return new Promise((resolve) => {
    const a = new Audio(url);
    audio = a;
    let settled = false;
    let fallbackStarted = false;
    const done = () => {
      if (settled) return;
      settled = true;
      if (audio === a) audio = null;
      resolve();
    };
    a.onended = done;
    a.onpause = done;
    a.onerror = done;
    unlockAudio();
    a.play().catch(() => {
      if (settled || fallbackStarted) return;
      fallbackStarted = true;
      void playThroughServer(text).finally(done);
    });
  });
}

export function stopSpeech(): void {
  stopToken++;
  queue.length = 0;
  pumping = false;
  if (audio) {
    try {
      audio.pause();
    } catch {
      /* ignore */
    }
    audio = null;
  }
}

export async function speakManual(text: string): Promise<string | null> {
  if (!text || !text.trim()) return null;
  try {
    const r = await fetch('/api/audio/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) {
      const err = (await r.json().catch(() => ({}))) as Record<string, unknown>;
      const msg = err.detail || err.error || err.message || `HTTP ${r.status}`;
      return `${r.status}: ${msg}`;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    a.onended = () => URL.revokeObjectURL(url);
    unlockAudio();
    try {
      await a.play();
    } catch {
      URL.revokeObjectURL(url);
      return 'browser blokira auto-play — klikni ▶ ponovo';
    }
  } catch (err) {
    return (err as Error).message;
  }
  return null;
}
