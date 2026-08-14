import { afterEach, describe, expect, it, vi } from 'vitest';
import { store } from '../store';

describe('local model selection', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    store.set({ currentModel: '' });
  });

  it.fails('test_local_model_not_active_until_ready', async () => {
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
        return Promise.resolve(new Response('{}', { status: 200 }));
      }),
    );

    store.set({ currentModel: 'cloud:model' });
    const transition = onModelChange('local:fake-model');

    await Promise.resolve();
    expect(store.state.currentModel).toBe('cloud:model');

    releaseLoad(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    await transition;
  });
});
