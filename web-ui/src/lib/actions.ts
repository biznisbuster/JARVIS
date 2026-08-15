import { jfetch, jpost, jput } from './api';
import { store } from '../store';
import { speakManual, stopSpeech } from './speech';
import type { ModelsPayload, SessionDetail, SessionMeta, VoicesPayload } from './types';

export async function refreshSessions(): Promise<void> {
  try {
    const list = await jfetch<SessionMeta[]>('/api/sessions');
    store.set({ sessions: list });
  } catch {
    /* ignore */
  }
}

export async function loadSession(id: string): Promise<void> {
  store.set({ sessionId: id });
  stopSpeech();
  store.clearTranscript();
  try {
    const data = await jfetch<SessionDetail>(`/api/sessions/${encodeURIComponent(id)}`);
    for (const m of data.messages || []) {
      if (m.role === 'user') store.addUser(m.content || '');
      else if (m.role === 'assistant') store.addAssistantFinal(m.content || '');
      else if (m.role === 'tool') store.addTool(`${m.name}: ${m.content}`);
    }
  } catch (e) {
    store.addTool(`⚠ sesija: ${(e as Error).message}`);
  }
  void refreshSessions();
}

export async function newSession(): Promise<void> {
  stopSpeech();
  try {
    const data = await jpost<SessionMeta>('/api/sessions', {});
    store.set({ sessionId: data.id });
    store.clearTranscript();
    void refreshSessions();
  } catch (e) {
    store.addTool(`⚠ nova sesija: ${(e as Error).message}`);
  }
}

export async function deleteSession(id: string): Promise<void> {
  try {
    const r = await fetch(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!r.ok) return;
    if (store.state.sessionId === id) {
      store.set({ sessionId: null });
      store.clearTranscript();
      stopSpeech();
    }
    void refreshSessions();
  } catch (e) {
    store.addTool(`⚠ brisanje: ${(e as Error).message}`);
  }
}

export async function sendText(
  text: string,
  opts?: { interrupt?: boolean; userLabel?: string },
): Promise<boolean> {
  stopSpeech();
  try {
    const data = await jpost<{ session_id: string }>('/api/chat', {
      text,
      session_id: store.state.sessionId,
      model: store.state.currentModel || null,
      interrupt: !!opts?.interrupt,
    });
    if (opts?.userLabel) store.addUser(opts.userLabel);
    store.set({ sessionId: data.session_id });
    void refreshSessions();
    return true;
  } catch (e) {
    store.addTool(`⚠ chat: ${(e as Error).message}`);
    return false;
  }
}

export async function sendDraft(): Promise<void> {
  const text = store.state.draft.trim();
  if (!text) return;
  store.set({ draft: '' });
  store.addUser(text);
  await sendText(text, { interrupt: false });
}

export async function stopTurn(): Promise<void> {
  const id = store.state.sessionId;
  if (!id) return;
  stopSpeech();
  try {
    await jpost('/api/chat/stop', { session_id: id });
  } catch {
    /* ignore */
  }
}

export function persistUI(patch: { model?: string; tts_enabled?: boolean }): void {
  jput('/api/state', patch).catch(() => {});
}

export async function loadPersistedUI(): Promise<string | null> {
  try {
    const d = await jfetch<{ ui?: { model?: string; tts_enabled?: boolean } }>('/api/state');
    const ui = d.ui || {};
    if (ui.tts_enabled === false) store.set({ ttsEnabled: false });
    return ui.model || null;
  } catch {
    return null;
  }
}

export async function ensureLocalLoaded(modelId: string): Promise<void> {
  store.addTool(`… učitavam lokalni model ${modelId} u RAM`);
  try {
    const data = await jpost<{ ok: boolean; error?: string }>('/api/local_models/load', {
      model_id: modelId,
    });
    if (!data.ok) store.addTool(`⚠ load: ${data.error}`);
  } catch (e) {
    store.addTool(`⚠ load: ${(e as Error).message}`);
  }
}

export async function bootModels(): Promise<void> {
  try {
    const m = await jfetch<ModelsPayload>('/api/models');
    store.set({ models: m.available });
    const persisted = await loadPersistedUI();
    let sel = m.current || '';
    if (persisted && m.available.some((x) => x.id === persisted)) sel = persisted;
    else if (!sel || !m.available.some((x) => x.id === sel)) sel = m.available[0]?.id || '';
    store.set({ currentModel: sel });
    if (sel.startsWith('local:')) void ensureLocalLoaded(sel.slice('local:'.length));
  } catch (e) {
    store.addTool(`⚠ modeli: ${(e as Error).message}`);
  }
}

export async function refreshModels(): Promise<void> {
  try {
    const m = await jfetch<ModelsPayload>('/api/models');
    const prev = store.state.currentModel;
    store.set({ models: m.available });
    if (prev && m.available.some((x) => x.id === prev)) return;
    const next = m.current || m.available[0]?.id || '';
    store.set({ currentModel: next });
  } catch (e) {
    store.addTool(`⚠ modeli: ${(e as Error).message}`);
  }
}

export async function onModelChange(id: string): Promise<void> {
  store.set({ currentModel: id });
  persistUI({ model: id });
  if (id.startsWith('local:')) await ensureLocalLoaded(id.slice('local:'.length));
}

export async function loadVoices(): Promise<void> {
  try {
    const data = await jfetch<VoicesPayload>('/api/tts/voices');
    store.set({ voices: data });
  } catch (e) {
    store.addTool(`⚠ voices: ${(e as Error).message}`);
  }
}

export async function setVoice(backend: string, voice: string, testText?: string): Promise<void> {
  try {
    const data = await jpost<{ ok: boolean; error?: string }>('/api/audio/tts/voice', {
      backend,
      voice,
    });
    if (!data.ok) {
      store.addTool(`⚠ glas: ${data.error}`);
      return;
    }
    if (testText) {
      const err = await speakManual(testText);
      if (err) store.addTool(`⚠ TTS: ${err}`);
    }
  } catch (e) {
    store.addTool(`⚠ glas: ${(e as Error).message}`);
  }
}

export function toggleTts(): void {
  const next = !store.state.ttsEnabled;
  if (!next) stopSpeech();
  store.set({ ttsEnabled: next });
  persistUI({ tts_enabled: next });
}

export async function playLastOnServer(): Promise<void> {
  const final = store.state.lastAssistantFinal;
  if (!final.trim()) return;
  try {
    await jpost('/api/audio/tts/play', { text: final, force: true });
  } catch (e) {
    store.addTool(`⚠ TTS: ${(e as Error).message}`);
  }
}

let mediaRecorder: MediaRecorder | null = null;
let micChunks: Blob[] = [];

function pickMime(): string {
  for (const m of [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
    'audio/mp4',
  ]) {
    if (MediaRecorder.isTypeSupported(m)) return m;
  }
  return '';
}

export async function toggleMic(): Promise<void> {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream, { mimeType: pickMime() });
    micChunks = [];
    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) micChunks.push(e.data);
    };
    mediaRecorder.onstop = async () => {
      const blob = new Blob(micChunks, { type: mediaRecorder?.mimeType });
      stream.getTracks().forEach((t) => t.stop());
      store.set({ recording: false });
      jpost('/api/audio/listen/stop', { reason: 'browser' }).catch(() => {});
      const fd = new FormData();
      fd.append('audio', blob, 'mic.webm');
      try {
        const data = await jfetch<{ ok: boolean; text?: string; error?: string }>('/api/audio/stt', {
          method: 'POST',
          body: fd,
        });
        if (data.ok && data.text) {
          store.addUser('🎙 ' + data.text);
          await sendText(data.text, { interrupt: true });
        } else if (!data.ok) {
          store.addTool(`⚠ STT greška: ${data.error}`);
        }
      } catch (e) {
        store.addTool(`⚠ STT: ${(e as Error).message}`);
      }
    };
    jpost('/api/audio/listen/start', { reason: 'browser' }).catch(() => {});
    mediaRecorder.start();
    stopSpeech();
    store.set({ recording: true });
  } catch (err) {
    store.addTool(`⚠ mikrofon: ${(err as Error).message}`);
    jpost('/api/audio/listen/stop', { reason: 'browser' }).catch(() => {});
  }
}
