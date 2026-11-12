export function escapeHtml(str) {
  if (!str) return "";
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const LINK_SCHEMES = new Set(["http", "https", "mailto"]);
const IMAGE_SCHEMES = new Set(["http", "https"]);

function unescapeHtml(str) {
  return str
    .replace(/&quot;/g, '"')
    .replace(/&gt;/g, ">")
    .replace(/&lt;/g, "<")
    .replace(/&amp;/g, "&");
}

// Returns the escaped URL when its scheme is allowed (or it has none), else null.
// The scheme is checked on exactly the string that is returned. Any whitespace
// or control character fails closed, because browsers strip some of them
// before detecting the scheme.
export function safeUrl(escapedUrl, allowedSchemes) {
  if (!escapedUrl) return null;
  const url = escapedUrl.replace(/^[\u0000-\u0020]+|[\u0000-\u0020]+$/g, "");
  if (!url || /[\s\u0000-\u001f\u007f-\u009f\ufeff]/.test(url)) return null;
  const scheme = unescapeHtml(url).match(/^([a-zA-Z][a-zA-Z0-9+.\-]*):/);
  if (scheme && !allowedSchemes.has(scheme[1].toLowerCase())) return null;
  return url;
}

export function renderMarkdown(text) {
  if (!text) return "";
  let html = escapeHtml(text);

  // Code blocks (``` ... ```)
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<div class="md-pre"><code>${code.trim()}</code></div>`;
  });

  // Inline code
  html = html.replace(/`([^`]+)`/g, '<span class="md-code">$1</span>');

  // Images before links
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_, alt, url) => {
    const src = safeUrl(url, IMAGE_SCHEMES);
    return src === null ? alt : `<img class="md-img" alt="${alt}" src="${src}" />`;
  });

  // Links
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
    const href = safeUrl(url, LINK_SCHEMES);
    return href === null
      ? label
      : `<a class="md-link" href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });

  // Headings
  html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // Horizontal rule
  html = html.replace(/^---+$/gm, '<hr class="md-hr" />');

  // Bold + Italic
  html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/~~(.+?)~~/g, '<del>$1</del>');

  // Blockquotes
  html = html.replace(/^&gt; (.+)$/gm, '<div class="md-quote">$1</div>');

  // Task lists
  html = html.replace(/^- \[x\] (.+)$/gm, '<div class="md-task"><input type="checkbox" checked disabled />$1</div>');
  html = html.replace(/^- \[ \] (.+)$/gm, '<div class="md-task"><input type="checkbox" disabled />$1</div>');

  // Unordered lists
  html = html.replace(/^- (.+)$/gm, '<li class="md-li">$1</li>');
  html = html.replace(/(<li class="md-li">[\s\S]*?<\/li>)/g, '<ul class="md-ul">$1</ul>');
  // Remove nested ul tags
  html = html.replace(/<\/ul>\s*<ul class="md-ul">/g, '');

  // Ordered lists
  html = html.replace(/^\d+\. (.+)$/gm, '<li class="md-oli">$1</li>');
  html = html.replace(/(<li class="md-oli">[\s\S]*?<\/li>)/g, '<ol class="md-ol">$1</ol>');
  html = html.replace(/<\/ol>\s*<ol class="md-ol">/g, '');

  // Tables
  html = html.replace(/^(\|.+\|)\n(\|[-| :]+\|)\n((?:\|.+\|\n?)+)/gm, (_, header, _sep, body) => {
    const ths = header.split("|").filter(c => c.trim()).map(c => `<th>${c.trim()}</th>`).join("");
    const rows = body.trim().split("\n").map(row => {
      const tds = row.split("|").filter(c => c.trim()).map(c => `<td>${c.trim()}</td>`).join("");
      return `<tr>${tds}</tr>`;
    }).join("");
    return `<table class="md-table"><thead><tr>${ths}</tr></thead><tbody>${rows}</tbody></table>`;
  });

  // Paragraphs (double newline)
  html = html.replace(/\n\n/g, '</p><p class="md-p">');
  // Single newlines to <br>
  html = html.replace(/\n/g, '<br />');

  return `<p class="md-p">${html}</p>`;
}
