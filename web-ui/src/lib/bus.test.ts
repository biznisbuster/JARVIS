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
    pendingModel: null,
    modelLoadError: null,
    models: [],
    localRunner: null,
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

  it('preserves a PTT transcript while a local model transition is pending', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    store.set({ currentModel: 'cloud:model', pendingModel: 'local:model-a' });

    handleEvent({
      kind: 'voice_ptt_transcribed',
      t: 1,
      payload: { ok: true, text: 'sačuvaj glas', auto_send: true },
    });

    await vi.waitFor(() => expect(store.state.draft).toBe('sačuvaj glas'));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(store.state.transcript.at(-1)).toMatchObject({
      role: 'tool',
      text: '… lokalni model se još učitava — transkript je sačuvan u inputu.',
    });
  });

  it('does not activate a stale local_model_ready event', () => {
    store.set({ currentModel: 'cloud:model', pendingModel: 'local:model-b' });

    handleEvent({
      kind: 'local_model_ready',
      t: 1,
      payload: {
        engine_available: true,
        state: 'ready',
        loaded_id: 'model-a',
        loaded_tag: 'model-a',
        target_id: null,
        target_tag: null,
        error: null,
        active_streams: 0,
      },
    });

    expect(store.state.currentModel).toBe('cloud:model');
    expect(store.state.pendingModel).toBe('local:model-b');
  });

  it('removes a no-longer-ready local model from confirmed execution state', () => {
    store.set({
      currentModel: 'local:model-a',
      localRunner: {
        engine_available: true,
        state: 'ready',
        loaded_id: 'model-a',
        target_id: null,
        error: null,
        active_streams: 0,
      },
      models: [{ id: 'cloud:model', label: 'Cloud' }, { id: 'local:model-a', label: 'Local' }],
    });

    handleEvent({
      kind: 'local_model_unloading',
      t: 1,
      payload: {
        engine_available: true,
        state: 'unloading',
        loaded_id: 'model-a',
        target_id: 'model-a',
        error: null,
        active_streams: 0,
      },
    });

    expect(store.state.currentModel).toBe('cloud:model');
    expect(store.state.modelLoadError).toContain('unloading');
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
