import { useEffect, useState } from 'react';
import { jfetch, jput } from '../lib/api';
import { store, useApp } from '../store';
import { TOOL_DESCRIPTIONS } from '../lib/tools';

interface PermissionsPayload {
  default_policy: 'allow' | 'ask' | 'deny';
  tools: Record<string, 'allow' | 'ask' | 'deny'>;
}

export function PermissionModal() {
  const pending = useApp((s) => s.pendingPerm);
  const remember = useApp((s) => s.modalRemember);

  const allow = async () => {
    if (!pending) return;
    try {
      await jfetch('/api/permissions/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: pending.request_id, action: 'allow', remember }),
      });
    } finally {
      store.dismissPerm();
    }
  };

  const deny = async () => {
    if (!pending) return;
    try {
      await jfetch('/api/permissions/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: pending.request_id, action: 'deny', remember }),
      });
    } finally {
      store.dismissPerm();
    }
  };

  if (!pending) return null;

  return (
    <div className="modal" role="dialog" aria-modal="true" aria-labelledby="perm-title">
      <div className="modal-card">
        <h3 id="perm-title">Zahtev za dozvolu</h3>
        <p>Jarvis želi da izvrši:</p>
        <div className="perm-tool">{pending.tool}</div>
        <pre className="perm-args">{JSON.stringify(pending.args, null, 2)}</pre>
        <label className="remember">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => store.set({ modalRemember: e.target.checked })}
          />{' '}
          Zapamti izbor za ovaj tool
        </label>
        <div className="modal-actions">
          <button type="button" onClick={deny}>Odbij</button>
          <button type="button" className="primary" onClick={allow} autoFocus>
            Dozvoli
          </button>
        </div>
      </div>
    </div>
  );
}

async function loadPermissions() {
  try {
    const data = await jfetch<PermissionsPayload>('/api/permissions');
    store.set({ permissions: data });
  } catch (e) {
    store.addTool(`⚠ dozvole: ${(e as Error).message}`);
  }
}

export default function PermissionsTab() {
  const permissions = useApp((s) => s.permissions);
  const [defaultPolicy, setDefaultPolicy] = useState<'allow' | 'ask' | 'deny'>('ask');

  useEffect(() => {
    void loadPermissions();
  }, []);

  useEffect(() => {
    if (permissions?.default_policy) setDefaultPolicy(permissions.default_policy);
  }, [permissions?.default_policy]);

  const tools = permissions?.tools || {};
  const known = Object.keys(TOOL_DESCRIPTIONS);
  const all = Array.from(new Set([...known, ...Object.keys(tools)])).sort();

  const saveDefault = async () => {
    try {
      await jput('/api/permissions', { default_policy: defaultPolicy });
      await loadPermissions();
    } catch (e) {
      store.addTool(`⚠ dozvole: ${(e as Error).message}`);
    }
  };

  const setPolicy = async (name: string, value: 'allow' | 'ask' | 'deny') => {
    try {
      await jput('/api/permissions', { tools: { [name]: value } });
      await loadPermissions();
    } catch (e) {
      store.addTool(`⚠ dozvole: ${(e as Error).message}`);
    }
  };

  return (
    <div className="panel">
      <div className="row">
        <label htmlFor="perm-default">Podrazumevana politika:</label>
        <select
          id="perm-default"
          value={defaultPolicy}
          onChange={(e) => setDefaultPolicy(e.target.value as 'allow' | 'ask' | 'deny')}
        >
          <option value="allow">allow — automatski dozvoli</option>
          <option value="ask">ask — uvek pitaj</option>
          <option value="deny">deny — blokiraj</option>
        </select>
        <button type="button" className="primary" onClick={saveDefault}>
          Sačuvaj
        </button>
      </div>
      <table className="data-table">
        <thead>
          <tr>
            <th>Tool</th>
            <th>Opis</th>
            <th>Politika</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {all.map((name) => {
            const policy = (tools[name] || permissions?.default_policy || 'ask') as
              | 'allow'
              | 'ask'
              | 'deny';
            const desc = TOOL_DESCRIPTIONS[name] || '—';
            return (
              <tr key={name}>
                <td><code>{name}</code></td>
                <td className="desc">{desc}</td>
                <td>
                  <select
                    aria-label={`Politika za ${name}`}
                    value={policy}
                    onChange={(e) => void setPolicy(name, e.target.value as 'allow' | 'ask' | 'deny')}
                  >
                    <option value="allow">allow</option>
                    <option value="ask">ask</option>
                    <option value="deny">deny</option>
                  </select>
                </td>
                <td>
                  <button
                    type="button"
                    onClick={() => void setPolicy(name, (permissions?.default_policy || 'ask') as 'allow' | 'ask' | 'deny')}
                  >
                    reset
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
