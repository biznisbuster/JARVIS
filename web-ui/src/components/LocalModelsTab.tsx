import { useEffect, useState } from 'react';
import { jfetch, jpost } from '../lib/api';
import { store, useApp } from '../store';
import type { LocalModel, LocalPull, LocalRunner } from '../store';

interface LocalModelsPayload {
  runner: LocalRunner;
  models: LocalModel[];
  pulls: LocalPull[];
}

const PULL_STATUS_LABEL: Record<string, string> = {
  starting: 'pokrećem…',
  progress: 'progress',
  done: '✓ skinuto',
  error: '⚠ greška',
  cancelled: 'otkazano',
};

export default function LocalModelsTab() {
  const [data, setData] = useState<LocalModelsPayload | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pullTag, setPullTag] = useState('');
  const wsPulls = useApp((s) => s.localPulls);

  useEffect(() => {
    void load();
  }, []);

  async function load() {
    try {
      const d = await jfetch<LocalModelsPayload>('/api/local_models');
      setData(d);
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function loadModel(id: string) {
    store.addTool(`… učitavam lokalni model ${id} u RAM`);
    try {
      const res = await jpost<{ ok: boolean; error?: string }>('/api/local_models/load', {
        model_id: id,
      });
      if (!res.ok) store.addTool(`⚠ load: ${res.error}`);
      await load();
    } catch (e) {
      store.addTool(`⚠ load: ${(e as Error).message}`);
    }
  }

  async function unloadModel() {
    try {
      await jpost('/api/local_models/unload', {});
      await load();
    } catch (e) {
      store.addTool(`⚠ unload: ${(e as Error).message}`);
    }
  }

  async function startPull() {
    const tag = pullTag.trim();
    if (!tag) return;
    try {
      const res = await jpost<{ ok: boolean; error?: string }>('/api/local_models/pull', { tag });
      if (!res.ok) store.addTool(`⚠ pull: ${res.error}`);
      else setPullTag('');
    } catch (e) {
      store.addTool(`⚠ pull: ${(e as Error).message}`);
    } finally {
      await load();
    }
  }

  async function cancelPull(tag: string) {
    try {
      await jpost('/api/local_models/pull/cancel', { tag });
    } catch (e) {
      store.addTool(`⚠ cancel pull: ${(e as Error).message}`);
    }
  }

  if (err) {
    return (
      <div className="panel">
        <p className="hint err">⚠ lokalni modeli: {err}</p>
        <button type="button" onClick={() => void load()}>Pokušaj ponovo</button>
      </div>
    );
  }
  if (!data) {
    return <div className="panel"><p className="hint">učitavam…</p></div>;
  }

  const runner = data.runner;
  const models = data.models;
  const serverPulls = data.pulls || [];
  const livePulls = mergePulls(serverPulls, wsPulls);

  return (
    <div className="panel">
      <div className="row">
        <h3>Lokalni modeli (Ollama)</h3>
        <button type="button" className="primary" onClick={() => void load()}>Osveži</button>
      </div>
      <p className="hint">
        Prikazani su SVI modeli koje Ollama ima na disku. Modeli označeni sa{' '}
        <span className="warn">⚠ bez tool-ova</span> ne mogu da izvršavaju akcije
        (muzika, kalendar…) — za to prebaci na cloud model.{' '}
        <code>JARVIS_LOCAL_MODELS</code> u <code>.env</code> služi samo za override
        parametara (<code>id|tag|n_ctx|keep_alive|flags</code>). Kad je model
        učitan, pojavljuje se u dropdown-u gore i može se izabrati kao aktivan LLM.
        Zahteva <code>ollama serve</code> u pozadini.
      </p>
      {runner && !runner.engine_available && (
        <div className="banner">
          <code>ollama</code> daemon nije pokrenut. Pokreni <code>ollama serve</code>{' '}
          (ili <code>brew services start ollama</code>) i osveži ovu stranicu.
        </div>
      )}
      <table className="data-table">
        <thead>
          <tr>
            <th>Model</th>
            <th>Ollama tag</th>
            <th>Veličina</th>
            <th>Alati</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {models.length === 0 && (
            <tr>
              <td colSpan={6} className="hint">
                Nema modela na disku. Ollama daemon nije dostupan ili nijedan model
                nije skinut — koristi pull ispod.
              </td>
            </tr>
          )}
          {models.map((m) => {
            const isLoaded = runner.state === 'ready' && runner.loaded_id === m.id;
            const isLoading = runner.state === 'loading' && runner.loaded_id === m.id;
            const status = isLoaded
              ? <span className="ok">● učitan u RAM</span>
              : isLoading
                ? <span className="hint">… učitavam</span>
                : m.in_ram
                  ? <span className="hint">● u RAM (Ollama)</span>
                  : <span className="hint">○ na disku</span>;
            const action = isLoaded
              ? <button type="button" onClick={() => void unloadModel()}>Oslobodi iz RAM-a</button>
              : <button type="button" className="primary" onClick={() => void loadModel(m.id)}>Učitaj u RAM</button>;
            return (
              <tr key={m.id}>
                <td><code>{m.id}</code></td>
                <td>
                  <code>{m.tag}</code>{' '}
                  <span className="hint">ctx {m.n_ctx} · keep {m.keep_alive}</span>
                </td>
                <td>{fmtSize(m.size)}</td>
                <td>{capabilityBadge(m.capability)}</td>
                <td>{status}</td>
                <td>{action}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 'var(--space-3)' }}>
        <input
          type="text"
          placeholder="npr. qwen3:14b ili llama3.2:3b"
          value={pullTag}
          onChange={(e) => setPullTag(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void startPull();
          }}
          style={{ flex: 1 }}
          aria-label="Ollama tag za pull"
        />
        <button type="button" className="primary" onClick={() => void startPull()}>
          ⬇ Skini model (pull)
        </button>
      </div>
      {livePulls.length > 0 && (
        <div className="pull-list">
          {livePulls.map((p) => (
            <PullRow key={p.tag} p={p} onCancel={() => void cancelPull(p.tag)} />
          ))}
        </div>
      )}
    </div>
  );
}

function PullRow({ p, onCancel }: { p: LocalPull; onCancel: () => void }) {
  const pct = Math.round(Number(p.percent || 0));
  const inProgress = p.status === 'starting' || p.status === 'progress';
  const label =
    p.status === 'progress'
      ? `${pct}% ${p.detail || ''}`
      : PULL_STATUS_LABEL[p.status] || p.status;
  return (
    <div className="pull-row" data-tag={p.tag}>
      <div className="pull-head">
        <code>{p.tag}</code>
        <span className={p.status === 'error' ? 'err' : p.status === 'done' ? 'ok' : 'hint'}>
          {label}
        </span>
      </div>
      <div className="pull-bar" aria-hidden>
        <div className="pull-fill" style={{ width: `${pct}%` }} />
      </div>
      {inProgress && (
        <button type="button" onClick={onCancel}>Otkaži</button>
      )}
    </div>
  );
}

function mergePulls(server: LocalPull[], live: LocalPull[]): LocalPull[] {
  const map = new Map<string, LocalPull>();
  for (const p of server) map.set(p.tag, p);
  for (const p of live) map.set(p.tag, p);
  return Array.from(map.values());
}

function fmtSize(bytes: number | undefined): string {
  if (!bytes) return '—';
  const gb = bytes / (1024 ** 3);
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

function capabilityBadge(cap: 'tools' | 'notools' | null | undefined) {
  if (cap === 'tools') return <span className="ok">✓ tool-ovi</span>;
  if (cap === 'notools') {
    return (
      <span className="warn" title="Model ne podržava function calling — ne može da izvršava akcije">
        ⚠ bez tool-ova
      </span>
    );
  }
  return <span className="hint">?</span>;
}
