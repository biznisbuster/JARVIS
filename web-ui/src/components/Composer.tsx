import { useEffect, useRef } from 'react';
import { store, useApp } from '../store';
import { sendDraft, stopTurn, toggleMic } from '../lib/actions';

export default function Composer() {
  const draft = useApp((s) => s.draft);
  const busy = useApp((s) => s.busy);
  const queued = useApp((s) => s.queued);
  const recording = useApp((s) => s.recording);
  const listenReasons = useApp((s) => s.listenReasons);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(200, el.scrollHeight) + 'px';
  }, [draft]);

  const listening = listenReasons.length > 0;
  const micClass = `mic-btn${recording ? ' recording' : ''}${listening ? ' listening' : ''}`;

  return (
    <div className="composer">
      <button
        type="button"
        id="mic"
        className={micClass}
        title="Push-to-talk (⌥⌘ Space)"
        aria-label="Mikrofon"
        onClick={() => void toggleMic()}
      >
        <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden>
          <path
            fill="currentColor"
            d="M12 14a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v5a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11z"
          />
        </svg>
        {listening && (
          <span className="listen-badge">🎙 slušam… ({listenReasons.join('+') || 'mic'})</span>
        )}
      </button>

      <textarea
        ref={taRef}
        id="input"
        rows={1}
        placeholder="Pitaj Jarvisa… (Enter = pošalji, Shift+Enter = novi red)"
        value={draft}
        onChange={(e) => store.set({ draft: e.target.value })}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            void sendDraft();
          }
        }}
      />

      <span className="busy-text" role="status">
        {busy ? (queued > 0 ? `… razmišljam (+${queued} u redu)` : '… razmišljam') : ''}
      </span>

      <button
        type="button"
        className="stop-btn"
        title="Prekini trenutni odgovor"
        disabled={!busy}
        onClick={() => void stopTurn()}
      >
        ■
      </button>

      <button type="button" className="btn primary" onClick={() => void sendDraft()}>
        Pošalji
      </button>
    </div>
  );
}
