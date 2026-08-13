import { TOOL_DESCRIPTIONS, TOOL_PARAMS } from '../lib/tools';

export default function ToolsTab() {
  return (
    <div className="panel">
      <table className="data-table">
        <thead>
          <tr>
            <th>Tool</th>
            <th>Opis</th>
            <th>Parametri</th>
          </tr>
        </thead>
        <tbody>
          {Object.keys(TOOL_DESCRIPTIONS).sort().map((name) => (
            <tr key={name}>
              <td><code>{name}</code></td>
              <td className="desc">{TOOL_DESCRIPTIONS[name]}</td>
              <td>
                {(TOOL_PARAMS[name] || []).length === 0
                  ? '—'
                  : (TOOL_PARAMS[name] || []).map((p) => (
                      <code key={p} style={{ marginRight: 4 }}>{p}</code>
                    ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
