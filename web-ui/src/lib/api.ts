export async function jfetch<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) {
    const err = (await r.json().catch(() => ({}))) as Record<string, unknown>;
    const msg = err.detail || err.error || err.message || `HTTP ${r.status}`;
    throw new Error(String(msg));
  }
  return (await r.json()) as T;
}

export function jpost<T>(url: string, body: unknown): Promise<T> {
  return jfetch<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function jput<T>(url: string, body: unknown): Promise<T> {
  return jfetch<T>(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
