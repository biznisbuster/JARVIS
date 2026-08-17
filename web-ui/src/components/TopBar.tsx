import { useApp } from '../store';
import { onModelChange, playLastOnServer, setVoice, toggleTts } from '../lib/actions';

const VOICE_GROUPS = [
  { id: 'say', label: 'macOS (Siri, offline)' },
  { id: 'edge', label: 'Microsoft Edge (online, najbolji srpski)' },
  { id: 'azure', label: 'Azure Speech (online)' },
  { id: 'elevenlabs', label: 'ElevenLabs (premium multilingual)' },
  { id: 'piper', label: 'Piper (offline, robot)' },
  { id: 'xtts', label: 'Coqui XTTS (kloniranje glasa)' },
];

const DEMO_TEXT: Record<string, string> = {
  edge: 'Ovaj glas dolazi sa Microsoft Edge servisa preko interneta.',
  azure: 'Ovaj glas koristi Azure govorni servis, najprirodniji je za srpski.',
  elevenlabs: 'Ovaj glas je sa ElevenLabs platforme, premium kvalitet i podržava srpski.',
  piper: 'Ovaj glas je robotski, ali radi potpuno bez interneta.',
  xtts: 'Ovaj glas je kloniran preko Coqui XTTS modela, lokalno i prirodno.',
};

function splitVoiceValue(val: string): [string, string] {
  const idx = val.indexOf(':');
  if (idx < 0) return [val, ''];
  return [val.slice(0, idx), val.slice(idx + 1)];
}

export default function TopBar() {
  const wsConnected = useApp((s) => s.wsConnected);
  const models = useApp((s) => s.models);
  const currentModel = useApp((s) => s.currentModel);
  const pendingModel = useApp((s) => s.pendingModel);
  const modelLoadError = useApp((s) => s.modelLoadError);
  const busy = useApp((s) => s.busy);
  const voices = useApp((s) => s.voices);
  const ttsEnabled = useApp((s) => s.ttsEnabled);

  const voiceValue = voices ? `${voices.backend}:${voices.voice}` : '';

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-logo" aria-hidden>✦</span>
        <span className="brand-title">Jarvis</span>
        <span className="brand-sub">lični AI asistent</span>
      </div>

      <div className="topbar-status">
        <span className={`ws-dot ${wsConnected ? 'ok' : 'err'}`} aria-hidden />
        <span className="ws-text">{wsConnected ? 'konekcija živa' : 'prekinuto — pokušavam ponovo…'}</span>

        <span className="topbar-sep" aria-hidden>·</span>

        <label className="visually-hidden" htmlFor="model-select">Aktivan model</label>
        <select
          id="model-select"
          title={pendingModel ? 'Lokalni model se učitava' : 'Aktivan model'}
          value={pendingModel || currentModel}
          disabled={busy}
          onChange={(e) => void onModelChange(e.target.value)}
        >
          {models.map((m) => (
            <option key={m.id} value={m.id}>{m.label}</option>
          ))}
        </select>

        {pendingModel && (
          <span className="model-transition" role="status">
            … učitavam lokalni model
          </span>
        )}
        {modelLoadError && (
          <span className="model-error" role="alert" title={modelLoadError}>
            ⚠ model: {modelLoadError}
          </span>
        )}

        <label className="visually-hidden" htmlFor="voice-select">Jarvis glas (TTS)</label>
        <select
          id="voice-select"
          title="Jarvis glas (TTS)"
          value={voiceValue}
          onChange={(e) => {
            const [be, vo] = splitVoiceValue(e.target.value);
            void setVoice(be, vo);
          }}
        >
          {voices &&
            VOICE_GROUPS.map((g) => {
              const list = voices.voices[g.id] || [];
              if (!list.length) return null;
              return (
                <optgroup key={g.id} label={g.label}>
                  {list.map((v) => (
                    <option key={`${g.id}:${v.id}`} value={`${g.id}:${v.id}`}>{v.label}</option>
                  ))}
                </optgroup>
              );
            })}
        </select>

        <button
          type="button"
          className="icon-btn"
          title="Proba glasa"
          onClick={() => {
            const [be, vo] = splitVoiceValue(voiceValue);
            const demo = DEMO_TEXT[be] || 'Ovaj glas je sa Apple Siri motora, potpuno lokalno i besplatno.';
            void setVoice(be, vo, demo);
          }}
        >
          ▶
        </button>

        <button
          type="button"
          className="icon-btn"
          title={ttsEnabled ? 'Jarvis glas: uključen' : 'Jarvis glas: isključen'}
          aria-pressed={ttsEnabled}
          onClick={toggleTts}
        >
          {ttsEnabled ? '🔊' : '🔇'}
        </button>

        <button
          type="button"
          className="icon-btn"
          title="Reprodukuj poslednji odgovor kroz sistemske zvučnike"
          onClick={() => void playLastOnServer()}
        >
          🔈
        </button>
      </div>
    </header>
  );
}
