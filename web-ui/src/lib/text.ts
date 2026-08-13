export function collapseDouble(s: string): string {
  if (!s || s.length < 4) return s;
  if (s.length % 2 === 0) {
    const half = s.length / 2;
    if (s.slice(0, half) === s.slice(half)) return collapseDouble(s.slice(0, half));
  }
  const paragraphs = s.split(/\n\s*\n/);
  if (paragraphs.length > 1) {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const p of paragraphs) {
      const key = p.trim();
      if (key) {
        if (seen.has(key)) continue;
        seen.add(key);
      }
      out.push(p);
    }
    const joined = out.join('\n\n');
    if (joined !== s) return joined;
  }
  return s;
}
