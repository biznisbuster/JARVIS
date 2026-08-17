import { afterEach, describe, expect, it, vi } from 'vitest';
import { store } from '../store';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function readyRunner(id: string) {
  return {
    engine_available: true,
    state: 'ready' as const,
    loaded_id: id,
    loaded_tag: id,
    target_id: null,
    target_tag: null,
    error: null,
    active_streams: 0,
  };
}

function resetStore(): void {
  store.clearTranscript();
  store.set({
    currentModel: '',
    pendingModel: null,
    modelLoadError: null,
    localRunner: null,
    models: [],
    draft: '',
    busy: false,
  });
}

describe('local model selection', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetStore();
  });

  it('does not make a local model current before the load response is ready', async () => {
    vi.stubGlobal('window', {});
    const { onModelChange } = await import('./actions');
    let releaseLoad!: (response: Response) => void;
    const loadResponse = new Promise<Response>((resolve) => {
      releaseLoad = resolve;
    });

    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).endsWith('/api/local_models/load')) return loadResponse;
        return Promise.resolve(jsonResponse({ ok: true }));
      }),
    );

    store.set({ currentModel: 'cloud:model', draft: 'sačuvaj me' });
    const transition = onModelChange('local:fake-model');

    await vi.waitFor(() => expect(store.state.pendingModel).toBe('local:fake-model'));
    expect(store.state.currentModel).toBe('cloud:model');
    expect(store.state.draft).toBe('sačuvaj me');

    releaseLoad(jsonResponse({ ok: true, runner: readyRunner('fake-model') }));
    await transition;
    expect(store.state.currentModel).toBe('local:fake-model');
    expect(store.state.pendingModel).toBeNull();
  });

  it('ignores a stale A success when B is selected before A finishes', async () => {
    vi.stubGlobal('window', {});
    const { onModelChange } = await import('./actions');
    const releases: Array<(response: Response) => void> = [];
    const loads = [
      new Promise<Response>((resolve) => releases.push(resolve)),
      new Promise<Response>((resolve) => releases.push(resolve)),
    ];
    let loadIndex = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).endsWith('/api/local_models/load')) return loads[loadIndex++];
        return Promise.resolve(jsonResponse({ ok: true }));
      }),
    );

    store.set({ currentModel: 'cloud:model' });
    const a = onModelChange('local:model-a');
    const b = onModelChange('local:model-b');
    await vi.waitFor(() => expect(store.state.pendingModel).toBe('local:model-b'));

    releases[0](jsonResponse({ ok: true, runner: readyRunner('model-a') }));
    await a;
    expect(store.state.currentModel).toBe('cloud:model');
    expect(store.state.pendingModel).toBe('local:model-b');

    releases[1](jsonResponse({ ok: true, runner: readyRunner('model-b') }));
    await b;
    expect(store.state.currentModel).toBe('local:model-b');
    expect(store.state.pendingModel).toBeNull();
  });

  it('does not let a late local success overwrite an explicit cloud choice', async () => {
    vi.stubGlobal('window', {});
    const { onModelChange } = await import('./actions');
    let releaseLoad!: (response: Response) => void;
    const loadResponse = new Promise<Response>((resolve) => {
      releaseLoad = resolve;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).endsWith('/api/local_models/load')) return loadResponse;
        return Promise.resolve(jsonResponse({ ok: true }));
      }),
    );

    const local = onModelChange('local:model-a');
    await vi.waitFor(() => expect(store.state.pendingModel).toBe('local:model-a'));
    await onModelChange('cloud:model');
    expect(store.state.currentModel).toBe('cloud:model');
    expect(store.state.pendingModel).toBeNull();

    releaseLoad(jsonResponse({ ok: true, runner: readyRunner('model-a') }));
    await local;
    expect(store.state.currentModel).toBe('cloud:model');
    expect(store.state.pendingModel).toBeNull();
  });

  it('keeps the confirmed model and surfaces a local load failure', async () => {
    vi.stubGlobal('window', {});
    const { onModelChange } = await import('./actions');
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).endsWith('/api/local_models/load')) {
          return Promise.resolve(new Response(JSON.stringify({ error: 'warmup failed' }), { status: 400 }));
        }
        return Promise.resolve(jsonResponse({ ok: true }));
      }),
    );

    store.set({ currentModel: 'cloud:model' });
    await onModelChange('local:model-a');

    expect(store.state.currentModel).toBe('cloud:model');
    expect(store.state.pendingModel).toBeNull();
    expect(store.state.modelLoadError).toContain('warmup failed');
  });

  it('blocks typed send while a transition is pending and preserves the draft', async () => {
    vi.stubGlobal('window', {});
    const { sendDraft } = await import('./actions');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    store.set({ currentModel: 'cloud:model', pendingModel: 'local:model-a', draft: 'ne šalji još' });

    await sendDraft();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(store.state.draft).toBe('ne šalji još');
  });

  it('routes browser-mic transcription through the pending transition gate', async () => {
    vi.stubGlobal('window', {});
    const { toggleMic } = await import('./actions');
    const chatBodies: unknown[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/chat')) chatBodies.push(init?.body);
      const body = url.endsWith('/api/audio/stt') ? { ok: true, text: 'browser pending' } : { ok: true };
      return Promise.resolve(jsonResponse(body));
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('navigator', {
      mediaDevices: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
      },
    });
    class FakeMediaRecorder {
      static isTypeSupported(): boolean {
        return false;
      }

      state = 'inactive';
      mimeType = 'audio/webm';
      ondataavailable: ((event: { data: { size: number } }) => void) | null = null;
      onstop: (() => void) | null = null;

      start(): void {
        this.state = 'recording';
      }

      stop(): void {
        this.state = 'inactive';
        this.onstop?.();
      }
    }
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
    store.set({ currentModel: 'cloud:model', pendingModel: 'local:model-a', draft: '' });

    await toggleMic();
    await toggleMic();
    await vi.waitFor(() => expect(store.state.draft).toBe('browser pending'));

    expect(chatBodies).toHaveLength(0);
    expect(store.state.transcript.at(-1)).toMatchObject({
      role: 'tool',
      text: '… lokalni model se još učitava — transkript je sačuvan u inputu.',
    });
  });

  it('keeps busy and model transition state independent', () => {
    store.set({ busy: true, pendingModel: 'local:model-a' });
    expect(store.state.busy).toBe(true);
    expect(store.state.pendingModel).toBe('local:model-a');
  });

  it('boots a persisted cloud preference as the confirmed model', async () => {
    vi.stubGlobal('window', {});
    const { bootModels } = await import('./actions');
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        if (String(input).endsWith('/api/models')) {
          return Promise.resolve(
            jsonResponse({
              current: 'cloud:default',
              available: [{ id: 'cloud:default', label: 'Cloud' }],
            }),
          );
        }
        if (String(input).endsWith('/api/state')) return Promise.resolve(jsonResponse({ ui: { model: 'cloud:default' } }));
        return Promise.resolve(jsonResponse({ ok: true }));
      }),
    );

    await bootModels();

    expect(store.state.currentModel).toBe('cloud:default');
    expect(store.state.pendingModel).toBeNull();
  });

  it('boots a persisted local preference as pending until the runner confirms ready', async () => {
    vi.stubGlobal('window', {});
    const { bootModels } = await import('./actions');
    let releaseLoad!: (response: Response) => void;
    const loadResponse = new Promise<Response>((resolve) => {
      releaseLoad = resolve;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/api/models')) {
          return Promise.resolve(
            jsonResponse({
              current: 'cloud:default',
              available: [
                { id: 'cloud:default', label: 'Cloud' },
                { id: 'local:fake-model', label: 'Local' },
              ],
            }),
          );
        }
        if (url.endsWith('/api/state')) return Promise.resolve(jsonResponse({ ui: { model: 'local:fake-model' } }));
        if (url.endsWith('/api/local_models')) {
          return Promise.resolve(
            jsonResponse({
              runner: {
                engine_available: true,
                state: 'idle',
                loaded_id: null,
                target_id: null,
                error: null,
                active_streams: 0,
              },
            }),
          );
        }
        if (url.endsWith('/api/local_models/load')) return loadResponse;
        return Promise.resolve(jsonResponse({ ok: true }));
      }),
    );

    const boot = bootModels();
    await vi.waitFor(() => expect(store.state.pendingModel).toBe('local:fake-model'));
    expect(store.state.currentModel).toBe('cloud:default');

    releaseLoad(jsonResponse({ ok: true, runner: readyRunner('fake-model') }));
    await boot;
    expect(store.state.currentModel).toBe('local:fake-model');
    expect(store.state.pendingModel).toBeNull();
  });

  it('keeps a persisted local preference and shows an error when boot loading fails', async () => {
    vi.stubGlobal('window', {});
    const { bootModels } = await import('./actions');
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      void init;
      const url = String(input);
      if (url.endsWith('/api/models')) {
        return Promise.resolve(
          jsonResponse({
            current: 'cloud:default',
            available: [
              { id: 'cloud:default', label: 'Cloud' },
              { id: 'local:fake-model', label: 'Local' },
            ],
          }),
        );
      }
      if (url.endsWith('/api/state')) return Promise.resolve(jsonResponse({ ui: { model: 'local:fake-model' } }));
      if (url.endsWith('/api/local_models')) {
        return Promise.resolve(
          jsonResponse({
            runner: {
              engine_available: true,
              state: 'error',
              loaded_id: null,
              target_id: null,
              error: 'Ollama unavailable',
              active_streams: 0,
            },
          }),
        );
      }
      if (url.endsWith('/api/local_models/load')) {
        return Promise.resolve(new Response(JSON.stringify({ error: 'warmup failed' }), { status: 400 }));
      }
      return Promise.resolve(jsonResponse({ ok: true }));
    });
    vi.stubGlobal('fetch', fetchMock);

    await bootModels();

    expect(store.state.currentModel).toBe('cloud:default');
    expect(store.state.pendingModel).toBeNull();
    expect(store.state.modelLoadError).toContain('warmup failed');
    const calls = fetchMock.mock.calls as Array<[RequestInfo | URL, RequestInit | undefined]>;
    expect(calls.some(([input, init]) => String(input).endsWith('/api/state') && init?.method === 'PUT')).toBe(false);
  });
});
