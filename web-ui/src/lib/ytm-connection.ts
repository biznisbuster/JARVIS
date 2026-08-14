export type YtmConnectionState = 'DISCONNECTED' | 'NEEDS_LOGIN' | 'CONNECTING' | 'CONNECTED' | 'ERROR';

export interface YtmConnectionStatus {
  state: YtmConnectionState;
  connected: boolean;
  needs_login: boolean;
  page_ready: boolean;
  search_ready: boolean;
  player_loaded: boolean;
  playing: boolean | null;
  error: string | null;
}

export function isYtmConnected(status: YtmConnectionStatus | null): boolean {
  return status?.state === 'CONNECTED' && status.connected === true;
}

export function ytmStatusLabel(status: YtmConnectionStatus): string {
  if (isYtmConnected(status)) return 'Povezan';
  if (status.state === 'CONNECTING') return 'Povezivanje…';
  if (status.state === 'NEEDS_LOGIN') return 'Potrebna prijava';
  if (status.state === 'ERROR') return 'Greška';
  return 'Nije povezan';
}

export function ytmConnectLabel(status: YtmConnectionStatus): string {
  if (isYtmConnected(status)) return 'Ponovo poveži YouTube Music';
  if (status.state === 'NEEDS_LOGIN') return 'Otvori YouTube Music prijavu';
  return 'Poveži YouTube Music';
}
