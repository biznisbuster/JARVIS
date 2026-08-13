function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function safeUrl(url: string): string {
  const u = url.trim();
  return /^https?:\/\//i.test(u) ? u : '#';
}

function inline(text: string): string {
  const parts = text.split(/(`[^`]+`)/g);
  return parts
    .map((part) => {
      if (part.startsWith('`') && part.endsWith('`') && part.length > 1) {
        return `<code>${part.slice(1, -1)}</code>`;
      }
      let s = part;
      s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
      s = s.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
      s = s.replace(/(^|[\s(])_([^_\n]+)_/g, '$1<em>$2</em>');
      s = s.replace(
        /\[([^\]]+)\]\(([^)\s]+)\)/g,
        (_m, label: string, url: string) =>
          `<a href="${safeUrl(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`,
      );
      return s;
    })
    .join('');
}

export function renderMarkdown(src: string): string {
  const lines = escapeHtml(src).split('\n');
  const out: string[] = [];
  let para: string[] = [];
  let i = 0;

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.map(inline).join('<br>')}</p>`);
      para = [];
    }
  };

  while (i < lines.length) {
    const line = lines[i];

    if (line.trimStart().startsWith('```')) {
      flushPara();
      const buf: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith('```')) {
        buf.push(lines[i]);
        i++;
      }
      i++;
      out.push(`<pre><code>${buf.join('\n')}</code></pre>`);
      continue;
    }

    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushPara();
      const lvl = Math.min(h[1].length + 1, 5);
      out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`);
      i++;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      flushPara();
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*[-*]\s+/, ''))}</li>`);
        i++;
      }
      out.push(`<ul>${items.join('')}</ul>`);
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      flushPara();
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*\d+[.)]\s+/, ''))}</li>`);
        i++;
      }
      out.push(`<ol>${items.join('')}</ol>`);
      continue;
    }

    if (line.trim() === '') {
      flushPara();
      i++;
      continue;
    }

    para.push(line);
    i++;
  }
  flushPara();
  return out.join('');
}
