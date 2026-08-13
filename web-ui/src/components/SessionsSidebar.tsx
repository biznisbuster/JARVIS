import { useApp } from '../store';
import { deleteSession, loadSession, newSession } from '../lib/actions';

export default function SessionsSidebar() {
  const sessions = useApp((s) => s.sessions);
  const sessionId = useApp((s) => s.sessionId);

  return (
    <aside className="sessions">
      <button type="button" className="btn primary sessions-new" onClick={() => void newSession()}>
        + Nova konverzacija
      </button>
      <ul className="sessions-list">
        {sessions.map((s) => (
          <li key={s.id} className={s.id === sessionId ? 'active' : ''}>
            <button
              type="button"
              className="session-title"
              title={s.title || s.id}
              onClick={() => void loadSession(s.id)}
            >
              {s.title || s.id}
            </button>
            <button
              type="button"
              className="session-del"
              title="Obriši sesiju"
              aria-label={`Obriši sesiju ${s.title || s.id}`}
              onClick={async (e) => {
                e.stopPropagation();
                if (!confirm(`Obriši sesiju "${s.title || s.id}"?`)) return;
                await deleteSession(s.id);
              }}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}
