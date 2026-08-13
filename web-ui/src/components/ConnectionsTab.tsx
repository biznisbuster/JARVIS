import { useEffect, useState } from 'react';
import { jfetch, jpost } from '../lib/api';
import { store, useApp } from '../store';
import type { PttState } from '../store';

interface ConnectionsPayload {
  llm: {
    provider: string;
    base_url: string;
    model: string;
    small_model?: string;
    api_key_set: boolean;
  };
  kilo: {
    bin: string;
    available: boolean;
    config_path: string;
    config_exists: boolean;
  };
  whisper: {
    backend: string;
    model: string;
    device: string;
    compute: string;
    loaded: boolean;
  };
  tts: {
    backend: string;
    output: string;
    active: { backend: string; voice: string };
    piper: { voice: string; length_scale: number; say_voice: string; loaded: boolean };
    xtts: { model: string; language: string; use_gpu: boolean; speaker_wav: string; loaded: boolean };
  };
  ptt: PttState | null;
  listen: { active: boolean; reasons: string[]; prev_volume: number | null; restore_pending: boolean };
}

const LISTEN_HINT = 'sav zvuk na macOS-u se utiše na 0 dok snimaš i vraća posle 0.6 s';

export default function ConnectionsTab() {
  const [data, setData] = useState<ConnectionsPayload | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const ptt = useApp((s) => s.ptt);
  const listenReasons = useApp((s) => s.listenReasons);

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    if (!ptt?.enabled) return;
    const id = setInterval(() => {
      jfetch<PttState>('/api/ptt')
        .then((p) => store.set({ ptt: p }))
        .catch(() => {});
    }, 30000);
    return () => clearInterval(id);
  }, [ptt?.enabled]);

  async function load() {
    try {
      const c = await jfetch<ConnectionsPayload>('/api/connections');
      setData(c);
      setErr(null);
      if (c.ptt) store.set({ ptt: c.ptt });
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function togglePtt() {
    const url = ptt?.enabled ? '/api/ptt/disable' : '/api/ptt/enable';
    try {
      const res = await jpost<{ ok: boolean; ptt: PttState; error?: string }>(url, {});
      if (!res.ok) store.addTool(`⚠ PTT: ${res.error || 'switch failed'}`);
      store.set({ ptt: res.ptt });
    } catch (e) {
      store.addTool(`⚠ PTT: ${(e as Error).message}`);
    }
  }

  if (err) {
    return (
      <div className="panel">
        <p className="hint err">⚠ konekcije: {err}</p>
        <button type="button" onClick={() => void load()}>Pokušaj ponovo</button>
      </div>
    );
  }
  if (!data) {
    return <div className="panel"><p className="hint">učitavam…</p></div>;
  }

  const listenHint = LISTEN_HINT;

  return (
    <div className="panel">
      <ConnCard title="Minimax token plan (LLM)">
        <KV k="provider" v={data.llm.provider} />
        <KV k="base_url" v={data.llm.base_url} />
        <KV k="model" v={data.llm.model + (data.llm.small_model ? `  ·  small=${data.llm.small_model}` : '')} />
        <KV k="api_key" v={data.llm.api_key_set ? '✓ postavljen' : '⚠ nije postavljen'} />
        <p className="hint">Ključ i endpoint se čitaju iz <code>~/.config/kilo/kilo.jsonc</code>.</p>
      </ConnCard>

      <ConnCard title="Kilo CLI (kod/terminal)">
        <KV k="bin" v={data.kilo.bin} />
        <KV k="config" v={data.kilo.config_path + (data.kilo.config_exists ? '' : '  ⚠ nedostaje')} />
        <KV k="dostupan" v={data.kilo.available ? '✓' : '✗ npm install -g @kilocode/cli'} />
      </ConnCard>

      <ConnCard title="Audio (lokalno)">
        <KV k="whisper" v={`${data.whisper.backend} ${data.whisper.model} (${data.whisper.device}, ${data.whisper.compute}) ${data.whisper.loaded ? '✓' : '○'}`} />
        <KV k="piper" v={`${data.tts.piper.voice}  ·  fallback say: ${data.tts.piper.say_voice || 'auto'} ${data.tts.piper.loaded ? '✓' : '○'}`} />
        <KV k="xtts" v={`${data.tts.xtts.model} (${data.tts.xtts.language}, gpu=${data.tts.xtts.use_gpu}) ${data.tts.xtts.loaded ? '✓' : '○'}`} />
        <KV k="active" v={`${data.tts.active.backend} / ${data.tts.active.voice}`} />
      </ConnCard>

      <ConnCard title="Global Push-to-Talk">
        <KV k="key" v={ptt?.key || '—'} />
        <KV k="auto-send" v={ptt?.auto_send ? 'uključen — transkript se odmah šalje' : 'isključen — transkript ide u input'} />
        <KV k="status" v={ptt?.enabled ? '● ACTIVE — drži taster da snimaš' : '○ ugašen'} />
        <p className="hint">{renderPttHint(ptt, listenHint, listenReasons.length > 0)}</p>
        <div className="row">
          <button type="button" className="primary" onClick={() => void togglePtt()}>
            {ptt?.enabled ? 'Isključi' : 'Uključi'}
          </button>
        </div>
      </ConnCard>
    </div>
  );
}

function ConnCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="conn-card">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <div className="kv">
      <span>{k}</span>
      <code>{v}</code>
    </div>
  );
}

function renderPttHint(ptt: PttState | null, listenHint: string, isListening: boolean): React.ReactNode {
  const tail = ptt?.auto_send
    ? 'transkript se odmah šalje kao poruka.'
    : 'transkript ide u input polje.';
  if (ptt?.enabled) {
    if (ptt.no_events_yet) {
      return (
        <>
          ⚠ Listener aktivan ali ne dobija evente već &gt;60s. Otvori{' '}
          <b>System Settings → Privacy & Security → Accessibility</b> i daj
          dozvolu procesu koji je pokrenuo Jarvis (obično Terminal / iTerm /
          Python — pogledaj u Task Manager koji PID ima <code>jarvis serve</code>).
          Bez dozvole pynput tiho guta key evente.
        </>
      );
    }
    return `Drži taster bilo gde na macOS-u. Kad pustiš, ${listenHint}; ${tail}`;
  }
  if (ptt?.error) {
    return `Greška: ${ptt.error} — verovatno treba Accessibility dozvola (System Settings → Privacy & Security → Accessibility).`;
  }
  if (isListening) return `Trenutno u listen mode-u (${listenHint}).`;
  return `PTT radi BILO GDE na macOS-u (nije samo u Jarvis prozoru). Drži taster da snimiš, pusti da transkribuje.`;
}
