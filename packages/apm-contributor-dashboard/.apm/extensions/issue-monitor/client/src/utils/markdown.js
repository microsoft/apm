export function escapeHtml(str) {
  if (!str) return "";
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
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
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img class="md-img" alt="$1" src="$2" />');

  // Links
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a class="md-link" href="$2" target="_blank">$1</a>');

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
