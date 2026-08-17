import { jfetch, jpost, jput } from './api';
import { store } from '../store';
import type { LocalModel, LocalPull, LocalRunner } from '../store';
import { speakManual, stopSpeech } from './speech';
import type { ModelsPayload, SessionDetail, SessionMeta, VoicesPayload } from './types';

interface LocalModelsPayload {
  runner: LocalRunner;
  models: LocalModel[];
  pulls: LocalPull[];
}

interface LocalLoadPayload {
  ok: boolean;
  error?: string;
  error_code?: string;
  runner?: LocalRunner;
}

let modelSelectionGeneration = 0;

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
  opts?: {
    interrupt?: boolean;
    userLabel?: string;
    source?: 'text' | 'ptt';
    preserveOnBlocked?: boolean;
  },
): Promise<boolean> {
  const pending = store.state.pendingModel;
  if (pending) {
    if (opts?.preserveOnBlocked !== false) {
      const draft = store.state.draft;
      store.set({ draft: draft ? `${draft} ${text}` : text });
    }
    store.addTool('… lokalni model se još učitava — transkript je sačuvan u inputu.');
    return false;
  }

  const requestedModel = store.state.currentModel || null;
  stopSpeech();
  try {
    const data = await jpost<{ session_id: string }>('/api/chat', {
      text,
      session_id: store.state.sessionId,
      model: requestedModel,
      interrupt: !!opts?.interrupt,
      source: opts?.source || 'text',
    });
    if (opts?.userLabel) store.addUser(opts.userLabel);
    store.set({ sessionId: data.session_id });
    void refreshSessions();
    return true;
  } catch (e) {
    if (opts?.source === 'ptt' || opts?.userLabel) {
      const draft = store.state.draft;
      store.set({ draft: draft ? `${draft} ${text}` : text });
    }
    store.addTool(`⚠ chat: ${(e as Error).message}`);
    return false;
  }
}

export async function sendDraft(): Promise<void> {
  const text = store.state.draft.trim();
  if (!text) return;
  if (store.state.pendingModel) return;
  const accepted = await sendText(text, { interrupt: false, preserveOnBlocked: false });
  if (accepted) {
    store.set({ draft: '' });
    store.addUser(text);
  }
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

export async function ensureLocalLoaded(modelId: string, generation?: number): Promise<LocalRunner> {
  store.addTool(`… učitavam lokalni model ${modelId} u RAM`);
  const data = await jpost<LocalLoadPayload>('/api/local_models/load', {
    model_id: modelId,
  });
  if (!data.ok || !data.runner) {
    throw new Error(data.error || 'lokalni model nije spreman');
  }
  if (generation === undefined || generation === modelSelectionGeneration) {
    store.set({ localRunner: data.runner });
  }
  return data.runner;
}

export async function bootModels(): Promise<void> {
  try {
    const m = await jfetch<ModelsPayload>('/api/models');
    store.set({ models: m.available });
    const persisted = await loadPersistedUI();
    let desired = m.current || '';
    if (persisted && m.available.some((x) => x.id === persisted)) desired = persisted;
    else if (!desired || !m.available.some((x) => x.id === desired)) {
      desired = m.available.find((x) => !x.id.startsWith('local:'))?.id || m.available[0]?.id || '';
    }

    const generation = ++modelSelectionGeneration;
    if (!desired.startsWith('local:')) {
      store.set({ currentModel: desired, pendingModel: null, modelLoadError: null });
      return;
    }

    const safeCurrent = m.available.find((x) => !x.id.startsWith('local:'))?.id || '';
    store.set({
      currentModel: safeCurrent,
      pendingModel: desired,
      modelLoadError: null,
    });

    let runner: LocalRunner | null = null;
    try {
      const local = await jfetch<LocalModelsPayload>('/api/local_models');
      runner = local.runner;
      store.set({ localRunner: runner });
    } catch {
      // The authoritative load endpoint below still gets a chance to
      // reconcile readiness; its failure is shown as the transition error.
    }

    const localId = desired.slice('local:'.length);
    if (runner?.state === 'ready' && runner.loaded_id === localId) {
      store.set({ currentModel: desired, pendingModel: null, modelLoadError: null });
      return;
    }

    try {
      const loaded = await ensureLocalLoaded(localId, generation);
      if (generation !== modelSelectionGeneration) return;
      if (loaded.state !== 'ready' || loaded.loaded_id !== localId) {
        throw new Error('backend nije potvrdio ready stanje lokalnog modela');
      }
      store.set({ currentModel: desired, pendingModel: null, modelLoadError: null, localRunner: loaded });
    } catch (e) {
      if (generation !== modelSelectionGeneration) return;
      store.set({ pendingModel: null, modelLoadError: (e as Error).message });
      store.addTool(`⚠ lokalni model: ${(e as Error).message}`);
    }
  } catch (e) {
    store.addTool(`⚠ modeli: ${(e as Error).message}`);
  }
}

export async function refreshModels(): Promise<void> {
  try {
    const m = await jfetch<ModelsPayload>('/api/models');
    store.set({ models: m.available });
    if (!store.state.currentModel && !store.state.pendingModel) {
      const next = m.current || m.available.find((x) => !x.id.startsWith('local:'))?.id || '';
      store.set({ currentModel: next });
    }
  } catch (e) {
    store.addTool(`⚠ modeli: ${(e as Error).message}`);
  }
}

export async function onModelChange(id: string): Promise<void> {
  const generation = ++modelSelectionGeneration;
  if (!id.startsWith('local:')) {
    store.set({ currentModel: id, pendingModel: null, modelLoadError: null });
    persistUI({ model: id });
    return;
  }

  const localId = id.slice('local:'.length);
  const safeCloud = store.state.models.find((model) => !model.id.startsWith('local:'))?.id || '';
  const confirmedCurrent = store.state.currentModel.startsWith('local:') ? safeCloud : store.state.currentModel;
  store.set({
    currentModel: confirmedCurrent,
    pendingModel: id,
    modelLoadError: null,
  });
  persistUI({ model: id });

  try {
    const runner = await ensureLocalLoaded(localId, generation);
    if (generation !== modelSelectionGeneration) return;
    if (runner.state !== 'ready' || runner.loaded_id !== localId) {
      throw new Error('backend nije potvrdio ready stanje lokalnog modela');
    }
    store.set({ currentModel: id, pendingModel: null, modelLoadError: null, localRunner: runner });
  } catch (e) {
    if (generation !== modelSelectionGeneration) return;
    store.set({ pendingModel: null, modelLoadError: (e as Error).message });
    store.addTool(`⚠ lokalni model: ${(e as Error).message}`);
  }
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
      try {
        await jpost('/api/audio/listen/stop', { reason: 'browser' });
      } catch {
        store.addTool('⚠ audio focus: vraćanje zvuka nije potvrđeno');
      }
      const fd = new FormData();
      fd.append('audio', blob, 'mic.webm');
      try {
        const data = await jfetch<{ ok: boolean; text?: string; error?: string }>('/api/audio/stt', {
          method: 'POST',
          body: fd,
        });
        if (data.ok && data.text) {
          await sendText(data.text, {
            interrupt: true,
            userLabel: '🎙 ' + data.text,
          });
        } else if (!data.ok) {
          store.addTool(`⚠ STT greška: ${data.error}`);
        }
      } catch (e) {
        store.addTool(`⚠ STT: ${(e as Error).message}`);
      }
    };
    await jpost('/api/audio/listen/start', { reason: 'browser' });
    mediaRecorder.start();
    stopSpeech();
    store.set({ recording: true });
  } catch (err) {
    store.addTool(`⚠ mikrofon: ${(err as Error).message}`);
    jpost('/api/audio/listen/stop', { reason: 'browser' }).catch(() => {});
  }
}
