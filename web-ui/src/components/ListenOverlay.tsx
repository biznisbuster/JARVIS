import { useApp } from '../store';

export default function ListenOverlay() {
  const listenReasons = useApp((s) => s.listenReasons);
  const recording = useApp((s) => s.recording);

  if (!listenReasons.length && !recording) return null;

  return (
    <div className="listen-overlay" role="status" aria-live="polite">
      <span className="listen-pulse" aria-hidden />
      <div className="listen-copy">
        <strong>{recording ? 'Snimam…' : 'Slušam…'}</strong>
        <span className="listen-sub">
          {listenReasons.length ? `izvor: ${listenReasons.join(' + ')} · ` : ''}
          zvuk je zaustavljen dok pričaš
        </span>
      </div>
    </div>
  );
}
