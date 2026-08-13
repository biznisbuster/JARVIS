import { useEffect } from 'react';
import { store, useApp } from './store';
import { handleEvent } from './lib/bus';
import { unlockAudio } from './lib/speech';
import { bootModels, loadVoices, refreshSessions, toggleMic } from './lib/actions';
import TopBar from './components/TopBar';
import ChatTab from './components/ChatTab';
import PermissionsTab, { PermissionModal } from './components/PermissionsTab';
import ConnectionsTab from './components/ConnectionsTab';
import LocalModelsTab from './components/LocalModelsTab';
import ToolsTab from './components/ToolsTab';
import LogsTab from './components/LogsTab';

const TABS: { id: string; label: string }[] = [
  { id: 'chat', label: 'Razgovor' },
  { id: 'permissions', label: 'Dozvole' },
  { id: 'connections', label: 'Konekcije' },
  { id: 'local-models', label: 'Lokalni modeli' },
  { id: 'tools', label: 'Alati' },
  { id: 'logs', label: 'Logovi' },
];

function useWebSocket() {
  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => store.set({ wsConnected: true });
      ws.onclose = () => {
        store.set({ wsConnected: false });
        if (!closed) timer = setTimeout(connect, 1500);
      };
      ws.onerror = () => ws?.close();
      ws.onmessage = (ev) => {
        try {
          handleEvent(JSON.parse(ev.data));
        } catch (err) {
          console.error('bad ws msg', err);
        }
      };
    };

    connect();
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      ws?.close();
    };
  }, []);
}

export default function App() {
  useWebSocket();

  useEffect(() => {
    void refreshSessions();
    void bootModels();
    void loadVoices();
  }, []);

  useEffect(() => {
    const unlock = () => unlockAudio();
    const hotkey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.altKey && e.code === 'Space') {
        e.preventDefault();
        void toggleMic();
      }
    };
    document.addEventListener('pointerdown', unlock, { capture: true });
    document.addEventListener('keydown', unlock, { capture: true });
    document.addEventListener('keydown', hotkey);
    return () => {
      document.removeEventListener('pointerdown', unlock, { capture: true });
      document.removeEventListener('keydown', unlock, { capture: true });
      document.removeEventListener('keydown', hotkey);
    };
  }, []);

  const activeTab = useApp((s) => s.activeTab);
  const setTab = (id: string) => store.set({ activeTab: id });

  return (
    <div className="app">
      <TopBar />
      <nav className="tabbar" aria-label="Sekcije">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`tab-btn${activeTab === t.id ? ' active' : ''}`}
            role="tab"
            aria-selected={activeTab === t.id}
            aria-controls={`tab-${t.id}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <main className="main">
        <div hidden={activeTab !== 'chat'} id="tab-chat" role="tabpanel" className="tabpanel">
          {activeTab === 'chat' && <ChatTab />}
        </div>
        <div hidden={activeTab !== 'permissions'} id="tab-permissions" role="tabpanel" className="tabpanel">
          {activeTab === 'permissions' && <PermissionsTab />}
        </div>
        <div hidden={activeTab !== 'connections'} id="tab-connections" role="tabpanel" className="tabpanel">
          {activeTab === 'connections' && <ConnectionsTab />}
        </div>
        <div hidden={activeTab !== 'local-models'} id="tab-local-models" role="tabpanel" className="tabpanel">
          {activeTab === 'local-models' && <LocalModelsTab />}
        </div>
        <div hidden={activeTab !== 'tools'} id="tab-tools" role="tabpanel" className="tabpanel">
          {activeTab === 'tools' && <ToolsTab />}
        </div>
        <div hidden={activeTab !== 'logs'} id="tab-logs" role="tabpanel" className="tabpanel">
          {activeTab === 'logs' && <LogsTab />}
        </div>
      </main>
      <PermissionModal />
    </div>
  );
}
