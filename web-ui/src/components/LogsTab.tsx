import { useEffect, useRef, useState } from 'react';
import { useApp, store } from '../store';

export default function LogsTab() {
  const logs = useApp((s) => s.logs);
  const [autoScroll, setAutoScroll] = useState(true);
  const ref = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || !autoScroll) return;
    el.scrollTop = el.scrollHeight;
  }, [logs, autoScroll]);

  const text = logs.join('\n');

  return (
    <div className="panel">
      <div className="logs-head">
        <span className="hint">Živi log događaja iz bus-a.</span>
        <label>
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
          />{' '}
          auto-scroll
        </label>
        <button type="button" onClick={() => store.set({ logs: [] })}>Očisti</button>
      </div>
      <pre ref={ref} className="log-stream" tabIndex={0} aria-label="Log događaja">
        {text || '\n'}
      </pre>
    </div>
  );
}
