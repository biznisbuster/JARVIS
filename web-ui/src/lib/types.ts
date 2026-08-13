export interface SessionMeta {
  id: string;
  title?: string;
  updated?: number;
}

export interface HistoryMessage {
  role: string;
  content?: string;
  name?: string;
}

export interface SessionDetail {
  id: string;
  title?: string;
  messages: HistoryMessage[];
}

export interface ModelInfo {
  id: string;
  label: string;
}

export interface ModelsPayload {
  available: ModelInfo[];
  current?: string;
}

export interface VoiceInfo {
  id: string;
  label: string;
}

export interface VoicesPayload {
  backend: string;
  voice: string;
  voices: Record<string, VoiceInfo[]>;
}

export interface BusEvent {
  kind: string;
  t: number;
  payload?: Record<string, unknown>;
}

export interface TtsSpeakPayload {
  url: string;
  text?: string;
  engine?: string;
  server_played?: boolean;
  session?: string;
}
