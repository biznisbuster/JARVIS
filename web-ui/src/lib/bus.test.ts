import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { store } from '../store';

let handleEvent: typeof import('./bus').handleEvent;

function resetStore(): void {
  store.clearTranscript();
  store.set({
    draft: '',
    logs: [],
    sessionId: null,
    currentModel: '',
  });
}

describe('PTT transcript delivery', () => {
  beforeEach(async () => {
    vi.stubGlobal('window', {});
    ({ handleEvent } = await import('./bus'));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetStore();
  });

  it('sends auto-send transcripts and only displays them after chat accepts them', async () => {
    const chatBodies: Record<string, unknown>[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith('/api/chat')) {
          chatBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
          return Promise.resolve(new Response(JSON.stringify({ session_id: 'session-1' }), { status: 200 }));
        }
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }));
      }),
    );

    handleEvent({
      kind: 'voice_ptt_transcribed',
      t: 1,
      payload: { ok: true, text: 'koliko je sati', auto_send: true },
    });

    expect(store.state.transcript).toEqual([]);
    await vi.waitFor(() => expect(store.state.transcript).toHaveLength(1));
    expect(store.state.transcript[0]).toMatchObject({ role: 'user', text: '🎙 koliko je sati' });
    expect(chatBodies).toEqual([
      { text: 'koliko je sati', session_id: null, model: null, interrupt: true, source: 'ptt' },
    ]);
  });

  it('does not show a failed auto-send transcript as a sent chat message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).endsWith('/api/chat')) {
          return Promise.resolve(new Response(JSON.stringify({ detail: 'backend unavailable' }), { status: 503 }));
        }
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }));
      }),
    );

    handleEvent({
      kind: 'voice_ptt_transcribed',
      t: 1,
      payload: { ok: true, text: 'test', auto_send: true },
    });

    await vi.waitFor(() => expect(store.state.transcript).toHaveLength(1));
    expect(store.state.transcript[0]).toMatchObject({ role: 'tool', text: '⚠ chat: backend unavailable' });
  });

  it('keeps non-auto-send transcripts as drafts instead of fake chat messages', () => {
    handleEvent({
      kind: 'voice_ptt_transcribed',
      t: 1,
      payload: { ok: true, text: 'samo nacrt', auto_send: false },
    });

    expect(store.state.draft).toBe('samo nacrt');
    expect(store.state.transcript).toHaveLength(1);
    expect(store.state.transcript[0]).toMatchObject({
      role: 'tool',
      text: '🎙 PTT transkript je u input polju — pritisni Pošalji.',
    });
  });

  it('does not auto-send rejected or no-speech PTT results', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    handleEvent({
      kind: 'voice_ptt_transcribed',
      t: 1,
      payload: { ok: true, skipped: 'no_speech', auto_send: true },
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(store.state.transcript.at(-1)).toMatchObject({ role: 'tool', text: '… PTT: nisam jasno čuo' });
  });

  it('falls back to system playback when browser audio is blocked', async () => {
    class BlockedAudio {
      onended: (() => void) | null = null;
      onpause: (() => void) | null = null;
      onerror: (() => void) | null = null;

      constructor(public readonly _url: string) {}

      play(): Promise<void> {
        return Promise.reject(new Error('NotAllowedError'));
      }

      pause(): void {
        this.onpause?.();
      }
    }

    vi.stubGlobal('Audio', BlockedAudio);
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).endsWith('/api/audio/tts/play')) {
          return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
        }
        return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }));
      }),
    );

    handleEvent({
      kind: 'tts_speak',
      t: 1,
      payload: { url: '/api/audio/file/reply.wav', text: 'Odgovor iz JARVIS-a', server_played: false },
    });

    await vi.waitFor(() => {
      expect(vi.mocked(fetch)).toHaveBeenCalledWith(
        '/api/audio/tts/play',
        expect.objectContaining({ method: 'POST' }),
      );
    });
    expect(store.state.transcript.at(-1)).toMatchObject({
      role: 'tool',
      text: '⚠ Browser je blokirao TTS — odgovor puštam preko sistema.',
    });
  });
});
