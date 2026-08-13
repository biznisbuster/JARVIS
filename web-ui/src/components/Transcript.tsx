import { memo, useEffect, useRef } from 'react';
import { useApp, type TranscriptItem } from '../store';
import { renderMarkdown } from '../lib/markdown';

const Message = memo(function Message({ item }: { item: TranscriptItem }) {
  if (item.role === 'user') {
    return <div className="msg user">{item.text}</div>;
  }
  if (item.role === 'tool') {
    return <div className="msg tool">{item.text}</div>;
  }
  return (
    <div className="msg assistant">
      {item.reasoning && (
        <details className="reasoning" open={item.thinking || undefined}>
          <summary>Razmišljanje</summary>
          <div className="reasoning-text">{item.reasoning}</div>
        </details>
      )}
      {item.streaming ? (
        <div className="assistant-text plain">
          {item.text}
          {item.thinking && <span className="thinking-dots">…</span>}
        </div>
      ) : item.text ? (
        <div
          className="assistant-text md"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(item.text) }}
        />
      ) : null}
    </div>
  );
});

export default function Transcript() {
  const items = useApp((s) => s.transcript);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items]);

  return (
    <div className="transcript" ref={ref}>
      {items.length === 0 && (
        <div className="empty-state">
          <div className="empty-icon" aria-hidden>✦</div>
          <p>Nema poruka — pitaj Jarvisa nešto.</p>
          <p className="empty-sub">
            Glasom: drži PTT taster ili klikni na mikrofon. Tekstom: kucaj i pritisni Enter.
          </p>
        </div>
      )}
      {items.map((m) => (
        <Message key={m.id} item={m} />
      ))}
    </div>
  );
}
