import { useSyncExternalStore } from 'react';
import type { ModelInfo, SessionMeta, VoicesPayload } from './lib/types';
import { collapseDouble } from './lib/text';

export type TranscriptItem =
  | { id: number; role: 'user'; text: string }
  | {
      id: number;
      role: 'assistant';
      text: string;
      reasoning: string;
      thinking: boolean;
      streaming: boolean;
    }
  | { id: number; role: 'tool'; text: string };

export interface AppState {
  wsConnected: boolean;
  sessionId: string | null;
  sessions: SessionMeta[];
  transcript: TranscriptItem[];
  busy: boolean;
  queued: number;
  ttsEnabled: boolean;
  lastAssistantFinal: string;
  models: ModelInfo[];
  /** Confirmed backend-safe execution model; never a merely requested local model. */
  currentModel: string;
  /** User's latest local selection while the runner is transitioning. */
  pendingModel: string | null;
  /** Visible error from the latest model selection transition. */
  modelLoadError: string | null;
  voices: VoicesPayload | null;
  listenReasons: string[];
  recording: boolean;
  draft: string;
  logs: string[];
  permissions: {
    default_policy: 'allow' | 'ask' | 'deny';
    tools: Record<string, 'allow' | 'ask' | 'deny'>;
  } | null;
  pendingPerm: {
    request_id: string;
    tool: string;
    args: Record<string, unknown>;
    reason?: string;
  } | null;
  modalRemember: boolean;
  activeTab: string;
  localEngineMissing: boolean;
  ptt: PttState | null;
  localRunner: LocalRunner | null;
  localModels: LocalModel[];
  localPulls: LocalPull[];
}

export interface PttState {
  enabled: boolean;
  key?: string;
  auto_send?: boolean;
  no_events_yet?: boolean;
  error?: string;
}

export interface LocalRunner {
  engine_available: boolean;
  state: 'idle' | 'loading' | 'ready' | 'error' | 'unloading';
  loaded_id: string | null;
  loaded_tag?: string | null;
  target_id?: string | null;
  target_tag?: string | null;
  error: string | null;
  active_streams?: number;
}

export interface LocalModel {
  id: string;
  tag: string;
  n_ctx: number;
  keep_alive: string;
  size: number;
  capability?: 'tools' | 'notools' | 'unknown';
  in_ram?: boolean;
}

export interface LocalPull {
  tag: string;
  status: string;
  percent: number;
  detail: string;
}

let nextId = 1;

class Store {
  private listeners = new Set<() => void>();

  private s: AppState = {
    wsConnected: false,
    sessionId: null,
    sessions: [],
    transcript: [],
    busy: false,
    queued: 0,
    ttsEnabled: true,
    lastAssistantFinal: '',
    models: [],
    currentModel: '',
    pendingModel: null,
    modelLoadError: null,
    voices: null,
    listenReasons: [],
    recording: false,
    draft: '',
    logs: [],
    permissions: null,
    pendingPerm: null,
    modalRemember: false,
    activeTab: 'chat',
    localEngineMissing: false,
    ptt: null,
    localRunner: null,
    localModels: [],
    localPulls: [],
  };

  ttsTurnOpen = false;
  ttsAggId: number | null = null;
  ttsAggCount = 0;

  get state(): AppState {
    return this.s;
  }

  subscribe = (l: () => void): (() => void) => {
    this.listeners.add(l);
    return () => {
      this.listeners.delete(l);
    };
  };

  set = (patch: Partial<AppState>): void => {
    this.s = { ...this.s, ...patch };
    this.listeners.forEach((l) => l());
  };

  appendLog = (line: string): void => {
    let logs = [...this.s.logs, line];
    if (logs.length > 800) logs = logs.slice(-400);
    this.set({ logs });
  };

  addUser = (text: string): void => {
    this.set({ transcript: [...this.s.transcript, { id: nextId++, role: 'user', text }] });
  };

  addTool = (text: string): number => {
    const id = nextId++;
    this.set({ transcript: [...this.s.transcript, { id, role: 'tool', text }] });
    return id;
  };

  updateTool = (id: number, text: string): void => {
    this.set({
      transcript: this.s.transcript.map((m) => (m.id === id && m.role === 'tool' ? { ...m, text } : m)),
    });
  };

  addAssistantFinal = (text: string): void => {
    this.set({
      transcript: [
        ...this.s.transcript,
        { id: nextId++, role: 'assistant', text, reasoning: '', thinking: false, streaming: false },
      ],
    });
  };

  clearTranscript = (): void => {
    this.ttsAggId = null;
    this.ttsAggCount = 0;
    this.set({ transcript: [] });
  };

  startAssistant = (): void => {
    this.set({
      transcript: [
        ...this.s.transcript,
        { id: nextId++, role: 'assistant', text: '', reasoning: '', thinking: false, streaming: true },
      ],
    });
  };

  private lastAssistant(): Extract<TranscriptItem, { role: 'assistant' }> | null {
    for (let i = this.s.transcript.length - 1; i >= 0; i--) {
      const m = this.s.transcript[i];
      if (m.role === 'assistant') return m;
    }
    return null;
  }

  appendDelta = (text: string): void => {
    const m = this.lastAssistant();
    if (!m || !m.streaming) return;
    this.set({
      transcript: this.s.transcript.map((x) =>
        x.id === m.id && x.role === 'assistant' ? { ...x, text: x.text + text, thinking: false } : x,
      ),
    });
  };

  appendReasoning = (text: string): void => {
    const m = this.lastAssistant();
    if (!m || !m.streaming) return;
    this.set({
      transcript: this.s.transcript.map((x) =>
        x.id === m.id && x.role === 'assistant' ? { ...x, reasoning: x.reasoning + text, thinking: true } : x,
      ),
    });
  };

  doneAssistant = (): void => {
    const m = this.lastAssistant();
    if (!m) return;
    const text = collapseDouble(m.text);
    this.set({
      transcript: this.s.transcript.map((x) =>
        x.id === m.id && x.role === 'assistant' ? { ...x, text, thinking: false, streaming: false } : x,
      ),
      lastAssistantFinal: text.trim() ? text : this.s.lastAssistantFinal,
    });
  };

  cancelAssistant = (): void => {
    const m = this.lastAssistant();
    if (!m) return;
    this.set({
      transcript: this.s.transcript.map((x) =>
        x.id === m.id && x.role === 'assistant'
          ? { ...x, text: `${x.text || ''}  ■ (prekinuto)`, thinking: false, streaming: false }
          : x,
      ),
    });
  };

  dismissPerm = (): void => {
    this.set({ pendingPerm: null, modalRemember: false });
  };
}

export const store = new Store();

export function useApp<T>(selector: (s: AppState) => T): T {
  return useSyncExternalStore(store.subscribe, () => selector(store.state));
}
