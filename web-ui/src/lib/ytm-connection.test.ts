import { describe, expect, it } from 'vitest';
import { isYtmConnected, ytmStatusLabel } from './ytm-connection';
import type { YtmConnectionStatus } from './ytm-connection';

const base: YtmConnectionStatus = {
  state: 'DISCONNECTED',
  connected: false,
  needs_login: false,
  page_ready: false,
  search_ready: false,
  player_loaded: false,
  playing: null,
  error: null,
};

describe('YouTube Music connection UI state', () => {
  it('does not show connected before backend confirmation', () => {
    const connecting = { ...base, state: 'CONNECTING' as const };
    const loginRequired = { ...base, state: 'NEEDS_LOGIN' as const, needs_login: true };

    expect(isYtmConnected(connecting)).toBe(false);
    expect(ytmStatusLabel(connecting)).toBe('Povezivanje…');
    expect(isYtmConnected(loginRequired)).toBe(false);
    expect(ytmStatusLabel(loginRequired)).toBe('Potrebna prijava');
  });

  it('shows connected only for a confirmed connected response', () => {
    const connected = {
      ...base,
      state: 'CONNECTED' as const,
      connected: true,
      page_ready: true,
      search_ready: true,
    };

    expect(isYtmConnected(connected)).toBe(true);
    expect(ytmStatusLabel(connected)).toBe('Povezan');
  });
});
