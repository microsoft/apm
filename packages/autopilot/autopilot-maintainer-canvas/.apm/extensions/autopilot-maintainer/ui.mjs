export function renderHtml({ csrfToken, repo }) {
    const token = String(csrfToken || "").replace(/[<>"]/g, "");
    const safeRepo = String(repo || "microsoft/apm").replace(/[<>]/g, "");
    return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Autopilot maintainer</title>
  <script>
    (function () {
      var pref = "auto";
      try { pref = localStorage.getItem("autopilot-maintainer-theme") || "auto"; } catch (_e) {}
      var resolved = pref;
      if (pref !== "light" && pref !== "dark") {
        var host = document.documentElement.getAttribute("data-color-mode") || "";
        if (host !== "light" && host !== "dark" && document.body) {
          host = document.body.getAttribute("data-color-mode") || "";
        }
        if (host === "light" || host === "dark") resolved = host;
        else resolved = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
      }
      document.documentElement.setAttribute("data-apm-theme", resolved === "light" ? "light" : "dark");
    })();
  </script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html[data-apm-theme="dark"] {
      color-scheme: dark;
      --apm-bg: #0d1117;
      --apm-fg: #e6edf3;
      --apm-muted: #8b949e;
      --apm-border: #30363d;
      --apm-card: #161b22;
      --apm-chip: #21262d;
      --apm-input: #0d1117;
      --apm-accent: #58a6ff;
      --apm-accent-bg: #1f6feb24;
      --apm-link: #58a6ff;
      --apm-nav-fg: #ffffff;
      --apm-nav-border: rgba(255,255,255,0.35);
      --apm-nav-bg: #010409;
      --apm-row-line: #21262d;
      --apm-hover: rgba(136,198,255,0.08);
      --apm-modal: #161b22;
      --apm-modal-fg: #c9d1d9;
      --apm-advice-bg: #30363d;
      --apm-advice-fg: #8b949e;
    }
    html[data-apm-theme="light"] {
      color-scheme: light;
      --apm-bg: #ffffff;
      --apm-fg: #1f2328;
      --apm-muted: #656d76;
      --apm-border: #d0d7de;
      --apm-card: #f6f8fa;
      --apm-chip: #ffffff;
      --apm-input: #ffffff;
      --apm-accent: #0969da;
      --apm-accent-bg: #ddf4ff;
      --apm-link: #0969da;
      --apm-nav-fg: #1f2328;
      --apm-nav-border: #d0d7de;
      --apm-nav-bg: #f6f8fa;
      --apm-row-line: #d8dee4;
      --apm-hover: rgba(9,105,218,0.05);
      --apm-modal: #ffffff;
      --apm-modal-fg: #1f2328;
      --apm-advice-bg: #eaeef2;
      --apm-advice-fg: #656d76;
    }
    html, body {
      background: var(--apm-bg);
      color: var(--apm-fg);
    }
    body {
      font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif);
      font-size: var(--text-body-medium, 13px);
      line-height: var(--leading-body-medium, 18px);
      padding: 10px;
    }
    body[data-layout="wide"] {
      padding: 16px;
      font-size: var(--text-body-medium, 14px);
      line-height: var(--leading-body-medium, 20px);
    }
    body[data-layout="wide"] .navbar {
      margin: -16px -16px 0 -16px;
      padding: 12px 16px;
    }
    #table { background: var(--apm-bg); color: var(--apm-fg); width: 100%; }
    .navbar {
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 10px 8px; margin: -10px -10px 0 -10px;
      background: var(--apm-nav-bg);
      border-bottom: 1px solid var(--apm-border);
      gap: 8px; flex-wrap: wrap;
    }
    .navbar-left { display: flex; align-items: center; gap: 14px; }
    .navbar-logo { height: 22px; width: 22px; display: block; flex-shrink: 0; }
    .navbar-divider { width: 1px; height: 24px; background: var(--apm-border); }
    .navbar-title { font-size: 14px; font-weight: 600; color: var(--apm-nav-fg); letter-spacing: -0.2px; }
    .navbar-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .navbar-repo {
      display: flex; align-items: center; gap: 5px;
      font-size: 12px; color: var(--apm-nav-fg); text-decoration: none;
      padding: 4px 10px; border: 1px solid var(--apm-nav-border);
      border-radius: 6px;
    }
    .navbar-repo:hover { border-color: var(--apm-nav-fg); }
    .navbar-repo svg { fill: currentColor; }
    .btn-refresh {
      background: transparent; color: var(--apm-nav-fg);
      border: 1px solid var(--apm-nav-border); border-radius: 6px;
      padding: 4px 10px; font-size: 12px; cursor: pointer;
    }
    .btn-refresh:hover { border-color: var(--apm-nav-fg); }
    .theme-select {
      background: var(--apm-nav-bg); color: var(--apm-nav-fg);
      border: 1px solid var(--apm-nav-border); border-radius: 6px;
      padding: 4px 8px; font-size: 12px; cursor: pointer;
    }
    .theme-select:hover { border-color: var(--apm-nav-fg); }
    .theme-select option { background: var(--apm-card); color: var(--apm-fg); }
    .subtitle { color: var(--apm-muted); font-size: 11px; margin: 8px 0 10px; }
    .live-dot {
      display: inline-block; width: 8px; height: 8px; border-radius: 50%;
      background: #3fb950; margin-right: 6px; animation: pulse 2s infinite;
    }
    .live-dot.pending { background: #d29922; }
    .live-dot.running { background: #3fb950; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
    .activity-bar { display: none; margin-bottom: 12px; }
    .activity-item {
      display: flex; align-items: center; gap: 8px;
      padding: 8px 12px; margin-bottom: 6px;
      border-radius: 8px; font-size: 12px; font-weight: 600;
    }
    .activity-item.pending { background: #d2992218; border: 1px solid #d2992240; color: #d29922; }
    .activity-item.running { background: #23863618; border: 1px solid #23863640; color: #3fb950; }
    .activity-item.saving { background: #1f6feb18; border: 1px solid #1f6feb40; color: #58a6ff; }
    .sessions-panel {
      background: var(--apm-card); border: 1px solid var(--apm-border); border-radius: 8px;
      padding: 0; margin-bottom: 12px; overflow: hidden;
    }
    .sessions-toggle {
      display: flex; align-items: center; gap: 8px; width: 100%;
      background: transparent; border: none; color: var(--apm-fg);
      padding: 8px 12px; cursor: pointer; font-size: 12px; font-weight: 600; text-align: left;
    }
    .sessions-toggle:hover { background: var(--apm-hover); }
    .sessions-chevron { color: var(--apm-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; width: 1.2em; }
    .sessions-summary { color: var(--apm-muted); font-weight: 500; margin-left: auto; font-size: 11px; }
    .sessions-body { border-top: 1px solid var(--apm-border); padding: 8px 12px 10px; }
    .sessions-panel.collapsed .sessions-body { display: none; }
    .sessions-head {
      display: flex; align-items: center; justify-content: space-between;
      font-size: 11px; font-weight: 600; color: var(--apm-muted);
      text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px;
    }
    .session-empty { font-size: 12px; color: var(--apm-muted); padding: 4px 0; }
    .sessions-table { width: 100%; }
    .sessions-table th, .sessions-table td { font-size: 12px; padding: 6px 8px; }
    .sessions-table td.child-name { padding-left: 1.5rem; }
    .session-actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
    .session-actions a, .session-actions button { text-decoration: none; }
    .session-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 220px; }
    .session-skill { color: var(--apm-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
    .btn-sm:disabled, .btn-start:disabled, .btn-open-session:disabled, .btn-refresh:disabled, .btn-purple:disabled {
      opacity: 0.55; cursor: wait;
    }
    .error-bar {
      background: #da363320; border: 1px solid #da3633; border-radius: 6px;
      padding: 8px 12px; margin-bottom: 12px; font-size: 12px; color: #f85149;
    }
    .stats { display: none; }
    .stat-card {
      background: var(--apm-card); border: 1px solid var(--apm-border);
      border-radius: 8px; padding: 10px 14px; min-width: 90px; text-align: center;
      cursor: pointer;
    }
    .stat-card.active { border-color: var(--apm-accent); }
    .stat-card .num { font-size: 22px; font-weight: 700; }
    .stat-card .label { font-size: 11px; color: var(--apm-muted); text-transform: uppercase; letter-spacing: 0.5px; }
    .bulk-action-bar {
      display: flex; align-items: center; justify-content: flex-end; gap: 6px; flex-wrap: wrap;
      grid-column: 1 / -1;
      padding: 4px 0 0; margin: 0;
      background: transparent; border: none;
    }
    .bulk-action-bar button {
      flex: 0 0 auto; max-width: 11rem;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .bulk-count { font-size: 11px; font-weight: 600; color: var(--apm-muted); margin-right: 2px; }
    body[data-layout="wide"] .bulk-action-bar {
      width: 100%; margin-left: auto;
    }
    .filter-bar {
      display: grid; grid-template-columns: 1fr 1fr; gap: 6px;
      margin: 0 0 10px; padding: 8px;
      background: var(--apm-card); border: 1px solid var(--apm-border); border-radius: 8px;
    }
    .filter-q {
      grid-column: 1 / -1;
      background: var(--apm-input); color: var(--apm-fg); border: 1px solid var(--apm-border);
      border-radius: 6px; padding: 6px 8px; font-size: 12px; width: 100%;
    }
    .filter-select {
      background: var(--apm-input); color: var(--apm-fg); border: 1px solid var(--apm-border);
      border-radius: 6px; padding: 5px 6px; font-size: 12px; width: 100%;
    }
    .filter-chip {
      padding: 3px 10px; font-size: 11px; font-weight: 600; border-radius: 12px;
      cursor: pointer; border: 1px solid var(--apm-border); background: var(--apm-chip); color: var(--apm-muted);
    }
    .filter-chip.active { border-color: var(--apm-accent); color: var(--apm-accent); background: var(--apm-accent-bg); }
    .filter-count { grid-column: 1 / -1; font-size: 11px; color: var(--apm-muted); text-align: right; }
    .label-filter { position: relative; }
    #label-filter-toggle { text-align: left; cursor: pointer; }
    #label-filter-toggle.active { border-color: var(--apm-accent); color: var(--apm-accent); }
    .label-filter-menu {
      position: absolute; z-index: 30; top: calc(100% + 4px); left: 0; right: 0;
      min-width: 16rem; max-height: 16rem; overflow: auto;
      background: var(--apm-card); border: 1px solid var(--apm-border);
      border-radius: 8px; padding: 8px;
      box-shadow: 0 8px 24px rgba(1,4,9,0.35);
    }
    .label-filter-search {
      background: var(--apm-input); color: var(--apm-fg); border: 1px solid var(--apm-border);
      border-radius: 6px; padding: 6px 8px; font-size: 12px; width: 100%; margin-bottom: 6px;
    }
    .label-option {
      display: flex; align-items: center; gap: 6px;
      font-size: 12px; padding: 4px 2px; cursor: pointer; color: var(--apm-fg);
    }
    .label-option:hover { background: var(--apm-hover); }
    .label-chips {
      grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 4px; align-items: center;
    }
    .label-chips:empty { display: none; }
    body[data-layout="wide"] .filter-bar {
      display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
    }
    body[data-layout="wide"] .filter-q {
      grid-column: auto; flex: 1 1 240px; width: auto;
    }
    body[data-layout="wide"] .filter-select {
      width: auto; min-width: 9rem; flex: 0 0 auto;
    }
    body[data-layout="wide"] .label-filter { flex: 0 0 auto; min-width: 9rem; }
    body[data-layout="wide"] .label-chips { flex: 1 1 100%; }
    body[data-layout="wide"] .filter-count {
      grid-column: auto; margin-left: auto; text-align: right;
    }
    body[data-layout="wide"] .title-cell {
      max-width: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    body[data-layout="wide"] .item-card,
    body[data-layout="wide"] .pair-row {
      grid-template-columns: minmax(0, 1fr) 11rem;
    }
    body[data-layout="wide"] .item-actions {
      width: 11rem;
      position: sticky;
      right: 0;
      top: 0;
      z-index: 1;
      background: var(--apm-bg);
    }
    body[data-layout="wide"] .pair-row.pr .item-actions {
      background: var(--apm-card);
    }
    tr.pair-start td { border-bottom: none; }
    tr.pair-end td { background: var(--apm-card); }
    .tab-bar {
      display: flex; gap: 0; margin-bottom: 10px; border-bottom: 2px solid var(--apm-border);
      overflow-x: auto; white-space: nowrap;
    }
    .tab-btn {
      padding: 8px 10px; font-size: 12px; font-weight: 600; cursor: pointer;
      background: transparent; color: var(--apm-muted); border: none; border-bottom: 2px solid transparent;
      margin-bottom: -2px; flex: 0 0 auto;
    }
    .tab-btn:hover { color: var(--apm-fg); }
    .tab-btn.active { color: var(--apm-accent); border-bottom-color: var(--apm-accent); }
    .tab-count {
      display: inline-block; background: var(--apm-advice-bg); color: var(--apm-advice-fg);
      padding: 1px 7px; border-radius: 10px; font-size: 11px; margin-left: 6px;
    }
    .tab-btn.active .tab-count { background: var(--apm-accent-bg); color: var(--apm-accent); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--apm-bg); color: var(--apm-fg); }
    th {
      text-align: left; padding: 8px 10px; border-bottom: 2px solid var(--apm-border);
      color: var(--apm-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600;
    }
    td { padding: 8px 10px; border-bottom: 1px solid var(--apm-row-line); vertical-align: middle; }
    tr:hover { background: var(--apm-hover); }
    a { color: var(--apm-link); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .title-cell { max-width: 360px; overflow: hidden; text-overflow: ellipsis; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; white-space: nowrap; margin-right: 4px; }
    .human-accepted { background: #23863630; color: #3fb950; border: 1px solid #238636; }
    .human-deferred { background: var(--apm-advice-bg); color: var(--apm-advice-fg); border: 1px solid var(--apm-border); }
    .human-triage { background: #d2992230; color: #d29922; border: 1px solid #d29922; }
    .human-design { background: #8957e530; color: #bc8cff; border: 1px solid #8957e5; }
    .human-panel { background: #6e40c930; color: #d2a8ff; border: 1px solid #6e40c9; }
    .advice { background: var(--apm-advice-bg); color: var(--apm-advice-fg); }
    .kind-issue { background: #1f6feb30; color: #58a6ff; border: 1px solid #1f6feb; }
    .kind-pr { background: #23863620; color: #3fb950; border: 1px solid #238636; }
    .label-area { background: #1f6feb18; color: #58a6ff; border: 1px solid #1f6feb; }
    .label-theme { background: #d2992218; color: #d29922; border: 1px solid #d29922; }
    .item-card {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 9.5rem;
      column-gap: 10px;
      row-gap: 6px;
      align-items: start;
      width: 100%;
      padding: 10px 8px 12px;
      border-bottom: 1px solid var(--apm-row-line);
      border-radius: 0;
    }
    .item-card.issue { border-left: 2px solid #58a6ff; }
    .item-card.pr { border-left: 2px solid #3fb950; }
    .item-card.pair {
      --pair-nest: 14px;
      display: flex;
      flex-direction: column;
      gap: 0;
      padding: 0;
      border-left: 2px solid #58a6ff;
    }
    .pair-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 9.5rem;
      column-gap: 10px;
      row-gap: 4px;
      align-items: start;
      width: 100%;
      padding: 10px 8px;
      border-radius: 0;
    }
    .pair-row.issue { border-left: none; }
    .pair-row.pr {
      margin-left: var(--pair-nest);
      width: calc(100% - var(--pair-nest));
      border-left: 2px solid #3fb950;
      background: var(--apm-card);
    }
    tr.pair-pr td:first-child { padding-left: 24px; }
    .item-card > .item-head,
    .item-card > .item-status {
      grid-column: 1;
    }
    .item-head { display: flex; gap: 8px; align-items: flex-start; min-width: 0; }
    .item-head > div { min-width: 0; flex: 1; }
    .item-title { font-weight: 600; color: var(--apm-fg); overflow-wrap: anywhere; }
    .item-status { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; margin: 0; }
    .item-status .session-skill { display: inline; margin: 0; }
    .item-actions {
      display: flex; flex-direction: column; flex-wrap: nowrap; gap: 6px;
      margin: 0; width: 9.5rem; grid-column: 2; grid-row: 1 / span 2; align-self: start;
    }
    .item-actions button {
      flex: 0 0 auto; width: 100%; max-width: none; min-height: 28px;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .action-cell {
      display: flex; flex-direction: column; align-items: stretch; gap: 6px;
      width: 9.5rem;
    }
    .action-cell button {
      width: 100%; min-height: 28px;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .btn-sm {
      padding: 4px 12px; font-size: 12px; font-weight: 600; border-radius: 6px;
      cursor: pointer; border: 1px solid var(--apm-border); background: var(--apm-chip); color: var(--apm-fg);
    }
    .btn-sm:hover { background: var(--apm-card); }
    .btn-start {
      background: #238636; color: #fff; border: 1px solid #2ea043;
      border-radius: 6px; padding: 4px 12px; font-size: 12px; font-weight: 600; cursor: pointer;
    }
    .btn-start:hover { background: #2ea043; }
    .btn-open-session {
      padding: 4px 10px; font-size: 11px; border-radius: 6px; cursor: pointer;
      border: 1px solid var(--apm-accent); background: var(--apm-accent-bg); color: var(--apm-accent); font-weight: 600;
    }
    .btn-purple { border-color: #6e40c9; background: #6e40c920; color: #bc8cff; }
    .btn-purple:hover { background: #6e40c940; }
    .btn-warn { border-color: #d29922; background: #d2992220; color: #d29922; }
    .empty { text-align: center; padding: 40px; color: var(--apm-muted); }
    .modal-overlay {
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6);
      z-index: 200; align-items: center; justify-content: center;
    }
    .modal-overlay.open { display: flex; }
    .modal {
      background: var(--apm-modal); border: 1px solid var(--apm-border); border-radius: 12px;
      width: 90%; max-width: 480px; box-shadow: 0 8px 30px rgba(0,0,0,0.5);
    }
    .modal-header { padding: 16px 20px 12px; border-bottom: 1px solid var(--apm-border); }
    .modal-header h2 { font-size: 16px; font-weight: 600; color: var(--apm-fg); }
    .modal-body { padding: 16px 20px; font-size: 13px; color: var(--apm-modal-fg); }
    .modal-actions { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 20px 16px; }
  </style>
</head>
<body>
  <nav class="navbar">
    <div class="navbar-left">
      <svg class="navbar-logo" viewBox="0 0 23 23" xmlns="http://www.w3.org/2000/svg">
        <rect x="1" y="1" width="10" height="10" fill="#f25022"/>
        <rect x="12" y="1" width="10" height="10" fill="#7fba00"/>
        <rect x="1" y="12" width="10" height="10" fill="#00a4ef"/>
        <rect x="12" y="12" width="10" height="10" fill="#ffb900"/>
      </svg>
      <div class="navbar-divider"></div>
      <div class="navbar-title">Autopilot maintainer</div>
    </div>
    <div class="navbar-right">
      <a class="navbar-repo" href="https://github.com/${safeRepo}" target="_blank" rel="noreferrer">
        <svg width="14" height="14" viewBox="0 0 16 16"><path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
        ${safeRepo}
      </a>
      <select id="theme-select" class="theme-select" title="Canvas theme. Auto follows the app or system.">
        <option value="auto" selected>Theme: Auto</option>
        <option value="light">Theme: Light</option>
        <option value="dark">Theme: Dark</option>
      </select>
      <button class="btn-refresh" data-sync>Sync</button>
      <button class="btn-refresh" data-refresh>Refresh</button>
    </div>
  </nav>
  <div class="subtitle"><span class="live-dot"></span><span id="live-text">Connecting to GitHub...</span></div>
  <div id="error" class="error-bar" style="display:none"></div>
  <div class="activity-bar" id="activity"></div>
  <section class="sessions-panel collapsed" id="sessions"></section>
  <div class="tab-bar" id="tabs"></div>
  <div class="filter-bar" id="filter-bar">
    <input class="filter-q" id="filter-q" type="search" placeholder="Search # or title" />
    <select id="filter-kind" class="filter-select">
      <option value="all">All kinds</option>
      <option value="issue">Issues</option>
      <option value="pr">PRs</option>
    </select>
    <select id="filter-advice" class="filter-select">
      <option value="">All advice</option>
      <option value="accept">Advice: accept</option>
      <option value="needs-design">Advice: needs-design</option>
      <option value="defer-later">Advice: defer-later</option>
      <option value="needs-issue">Advice: needs-issue</option>
      <option value="decline-with-reason">Advice: decline</option>
    </select>
    <select id="filter-extra" class="filter-select">
      <option value="">More filters</option>
      <option value="draft">Draft PRs</option>
      <option value="panelReview">Panel-review</option>
      <option value="needsDesign">Needs design</option>
    </select>
    <div class="label-filter" id="label-filter">
      <button type="button" id="label-filter-toggle" class="filter-select">Labels</button>
      <div class="label-filter-menu" id="label-filter-menu" hidden>
        <input id="label-filter-q" class="label-filter-search" type="search" placeholder="Find label" />
        <div id="label-filter-list"></div>
      </div>
    </div>
    <div class="label-chips" id="label-chips"></div>
    <span class="filter-count" id="filter-count"></span>
    <div class="bulk-action-bar" id="sweeps"></div>
  </div>
  <div id="table"></div>
  <div class="modal-overlay" id="modal">
    <div class="modal">
      <div class="modal-header"><h2 id="modal-title">Confirm</h2></div>
      <div class="modal-body" id="modal-body"></div>
      <div class="modal-actions">
        <button class="btn-sm" id="modal-cancel">Cancel</button>
        <button class="btn-start" id="modal-ok">Confirm</button>
      </div>
    </div>
  </div>
  <script>
    const CSRF = ${JSON.stringify(token)};
    const THEME_KEY = "autopilot-maintainer-theme";
    const SESSIONS_OPEN_KEY = "autopilot-maintainer-sessions-open";
    function hostOrSystemTheme() {
      const host = document.documentElement.getAttribute("data-color-mode")
        || (document.body && document.body.getAttribute("data-color-mode"))
        || "";
      if (host === "light" || host === "dark") return host;
      try {
        if (window.matchMedia("(prefers-color-scheme: light)").matches) return "light";
      } catch (_error) {}
      return "dark";
    }
    function themePref() {
      try {
        const stored = localStorage.getItem(THEME_KEY);
        if (stored === "light" || stored === "dark" || stored === "auto") return stored;
      } catch (_error) {}
      return "auto";
    }
    function applyTheme() {
      const pref = themePref();
      const resolved = pref === "auto" ? hostOrSystemTheme() : pref;
      document.documentElement.setAttribute("data-apm-theme", resolved === "light" ? "light" : "dark");
      const select = document.getElementById("theme-select");
      if (select && select.value !== pref) select.value = pref;
    }
    applyTheme();
    try {
      window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
        if (themePref() === "auto") applyTheme();
      });
    } catch (_error) {}
    const themeObserver = new MutationObserver(() => {
      if (themePref() === "auto") applyTheme();
    });
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-color-mode"] });
    if (document.body) themeObserver.observe(document.body, { attributes: true, attributeFilter: ["data-color-mode"] });
    const state = { issues: [], prs: [], occupancy: [], lastError: null, lastUpdated: null, notice: null, noticeKind: "pending" };
    const clickBusy = new Set();
    const filters = { q: "", kind: "all", advice: "", extra: "", labels: [] };
    let active = "new";
    let pending = null;
    const LANES = [
      { id: "new", label: "New", color: "#58a6ff" },
      { id: "decide", label: "Decide", color: "#d29922" },
      { id: "accepted", label: "Accepted", color: "#3fb950" },
      { id: "deferred", label: "Deferred", color: "#8b949e" },
    ];
    const SWEEPS = {
      new: [
        { action: "sweep-issue-triage", target: "scheduler:issue-triage", label: "Sweep issue triage", cls: "btn-open-session" },
        { action: "sweep-pr-triage", target: "scheduler:pr-triage", label: "Sweep PR triage", cls: "btn-open-session" },
      ],
      decide: [],
      accepted: [
        { action: "sweep-issue-delivery", target: "scheduler:issue-delivery", label: "Sweep issue delivery", cls: "btn-open-session" },
        { action: "sweep-pr-review", target: "scheduler:pr-review", label: "Sweep PR review", cls: "btn-purple btn-sm" },
      ],
      deferred: [],
    };
    function escapeHtml(value) {
      return String(value || "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
    }
    async function api(path, options) {
      const res = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", "x-canvas-token": CSRF },
      });
      const body = await res.json().catch(() => ({ ok: false, error: "invalid json" }));
      if (!res.ok || body.ok === false) throw new Error(body.error || res.statusText);
      return body;
    }
    function allItems() {
      const issues = state.issues || [];
      const listed = new Set(issues.map((issue) => issue.number));
      const prs = (state.prs || []).filter((pr) => !(pr.closesIssues || []).some((n) => listed.has(n)));
      return [...issues, ...prs];
    }
    function occupancyFor(target) {
      return (state.occupancy || []).find((row) => row.target === target) || null;
    }
    function occupancyActivity(row) {
      if (!row) return "idle";
      if (row.pending) return "pending";
      const raw = String(row.activity || "").toLowerCase();
      if (raw === "idle") return "idle";
      if (raw === "working" || raw === "busy") return "working";
      if (raw === "pending") return "pending";
      return row.busy === false ? "idle" : "working";
    }
    function occupancyWorking(row) {
      const activity = occupancyActivity(row);
      return activity === "working" || activity === "pending";
    }
    function workerTarget(action, number) {
      if (action === "worker-issue-triage") return "worker:issue-triage:" + number;
      if (action === "worker-issue-delivery") return "worker:issue-delivery:" + number;
      if (action === "worker-pr-triage") return "worker:pr-triage:" + number;
      if (action === "worker-pr-review") return "worker:pr-review:" + number;
      if (action === "worker-pr-merge") return "worker:pr-merge:" + number;
      return "";
    }
    function laneItems(id) {
      return allItems().filter((item) => (item.lanes || []).includes(id));
    }
    function typeShort(item) {
      const found = (item.labels || []).find((name) => name.startsWith("type/"));
      return found ? found.slice(5) : "";
    }
    function classLine(item) {
      const bits = (item.labels || []).filter((name) => name.startsWith("area/") || name.startsWith("theme/"));
      if (!bits.length) return "";
      return bits.map((name) => {
        const cls = name.startsWith("theme/") ? "label-theme" : "label-area";
        return '<span class="badge ' + cls + '">' + escapeHtml(name) + "</span>";
      }).join("");
    }
    function kindBadge(kind, item) {
      const type = typeShort(item || {});
      const base = kind === "pr" ? "PR" : "Issue";
      const text = type ? base + " · " + type : base;
      return '<span class="badge kind-' + kind + '">' + escapeHtml(text) + "</span>";
    }
    function prAsItem(pr) {
      return {
        kind: "pr",
        number: pr.number,
        title: pr.title || "",
        url: pr.url || "",
        accepted: Boolean(pr.accepted),
        deferred: Boolean(pr.deferred),
        panelReview: Boolean(pr.panelReview),
        isDraft: Boolean(pr.isDraft),
        triageRan: Boolean(pr.triageRan),
        triageConclusion: pr.triageConclusion || null,
        labels: pr.labels || [],
        needsTriage: false,
        needsDesign: false,
      };
    }
    function triageCell(item) {
      if (!item.triageRan) {
        return item.accepted ? "" : '<span class="badge advice">Not triaged</span>';
      }
      if (!item.triageConclusion) return '<span class="badge human-panel">Triaged</span>';
      return '<span class="badge human-panel">Triaged: ' + escapeHtml(item.triageConclusion) + "</span>";
    }
    function humanCell(item) {
      const out = [];
      if (item.isDraft) out.push(["draft", "human-triage"]);
      if (item.accepted) out.push(["accepted", "human-accepted"]);
      if (item.deferred) out.push(["deferred", "human-deferred"]);
      if (item.needsTriage) out.push(["needs-triage", "human-triage"]);
      if (item.needsDesign) out.push(["needs-design", "human-design"]);
      if (item.panelReview) out.push(["panel-review", "human-panel"]);
      if (!out.length) return "";
      return out.map(([label, cls]) => '<span class="badge ' + cls + '">' + label + "</span>").join("");
    }
    function statusLine(item) {
      return classLine(item) + triageCell(item) + humanCell(item);
    }
    function entityHead(item) {
      return '<div class="item-head">' + kindBadge(item.kind, item) +
        '<div><a href="' + escapeHtml(item.url) + '" target="_blank" rel="noreferrer">#' + item.number + "</a> " +
        '<span class="item-title">' + escapeHtml(item.title || "") + "</span></div></div>";
    }
    function matchesFilters(item) {
      const linked = item.linkedPrs || [];
      if (filters.kind === "issue" && item.kind !== "issue") return false;
      if (filters.kind === "pr" && item.kind !== "pr" && !linked.length) return false;
      const q = filters.q.trim().toLowerCase();
      if (q) {
        const linkedHay = linked.map((pr) => "#" + pr.number + " " + (pr.title || "")).join(" ");
        const hay = "#" + item.number + " " + (item.title || "") + " " + linkedHay;
        if (!hay.toLowerCase().includes(q)) return false;
      }
      if (filters.advice) {
        const adviceHit = item.triageConclusion === filters.advice
          || linked.some((pr) => pr.triageConclusion === filters.advice);
        if (!adviceHit) return false;
      }
      if (filters.extra === "draft" && !item.isDraft && !linked.some((pr) => pr.isDraft)) return false;
      if (filters.extra === "panelReview" && !item.panelReview && !linked.some((pr) => pr.panelReview)) return false;
      if (filters.extra === "needsDesign" && !item.needsDesign) return false;
      if (filters.labels.length) {
        const have = new Set(item.labels || []);
        linked.forEach((pr) => (pr.labels || []).forEach((name) => have.add(name)));
        if (!filters.labels.every((name) => have.has(name))) return false;
      }
      return true;
    }
    function filteredItems() {
      return laneItems(active).filter(matchesFilters);
    }
    function pushButton(btns, label, cls, payload) {
      let text = label;
      let disabled = "";
      if (payload.spawn) {
        const occ = occupancyFor(workerTarget(payload.spawn.action, payload.spawn.number));
        if (occupancyWorking(occ) || clickBusy.has(payload.spawn.action + ":" + payload.spawn.number)) {
          disabled = " disabled";
          text = occ && occupancyActivity(occ) === "working" ? "Working" : "Spawning...";
        }
      }
      btns.push('<button class="' + cls + '"' + disabled + ' data-act="' + encodeURIComponent(JSON.stringify(payload)) + '">' + text + "</button>");
    }
    function issueActions(item, lane) {
      const n = item.number;
      const btns = [];
      if (lane === "new") {
        pushButton(btns, "Issue triage", "btn-open-session", { spawn: { action: "worker-issue-triage", number: n } });
      }
      if (lane === "decide") {
        pushButton(btns, "Accept", "btn-start", { label: { kind: "issue", number: n, action: "accept" } });
        pushButton(btns, "Defer", "btn-sm", { label: { kind: "issue", number: n, action: "defer", confirmClearAccepted: !!item.accepted } });
        pushButton(btns, "Re-triage issue", "btn-open-session", { spawn: { action: "worker-issue-triage", number: n } });
      }
      if (lane === "accepted") {
        if (!item.hasOpenPr) {
          pushButton(btns, "Delivery worker", "btn-open-session", { spawn: { action: "worker-issue-delivery", number: n } });
        }
        pushButton(btns, "Defer", "btn-sm", { label: { kind: "issue", number: n, action: "defer", confirmClearAccepted: true } });
      }
      if (lane === "deferred") {
        pushButton(btns, "Accept", "btn-start", { label: { kind: "issue", number: n, action: "accept" } });
        pushButton(btns, "Re-triage issue", "btn-open-session", { spawn: { action: "worker-issue-triage", number: n } });
      }
      return btns.join("");
    }
    function prActions(pr, hostIssue, lane) {
      const n = pr.number;
      const btns = [];
      const nested = Boolean(hostIssue);
      if (lane === "deferred") {
        pushButton(btns, "Accept", "btn-start", { label: { kind: "pr", number: n, action: "accept" } });
        pushButton(btns, nested ? "Re-triage" : "Re-triage PR", "btn-open-session", {
          spawn: { action: "worker-pr-triage", number: n },
        });
        return btns.join("");
      }
      if (!pr.accepted) {
        if (!pr.triageRan) {
          pushButton(btns, "PR triage", "btn-open-session", {
            spawn: { action: "worker-pr-triage", number: n },
          });
        } else {
          pushButton(btns, "Accept", "btn-start", { label: { kind: "pr", number: n, action: "accept" } });
          pushButton(btns, "Defer", "btn-sm", { label: { kind: "pr", number: n, action: "defer", confirmClearAccepted: false } });
          pushButton(btns, nested ? "Re-triage" : "Re-triage PR", "btn-open-session", {
            spawn: { action: "worker-pr-triage", number: n },
          });
        }
      }
      if ((hostIssue && hostIssue.accepted) || pr.accepted || lane === "accepted") {
        pushButton(btns, nested ? "Review" : "Review worker", "btn-open-session", {
          spawn: { action: "worker-pr-review", number: n },
        });
        pushButton(btns, "Panel-review", "btn-purple btn-sm", { label: { kind: "pr", number: n, action: "panel-review" } });
        if (pr.accepted && !pr.isDraft) {
          pushButton(btns, nested ? "Merge" : "Spawn merge-worker", "btn-warn btn-sm", {
            spawn: { action: "worker-pr-merge", number: n, confirm: true },
          });
        }
      }
      return btns.join("");
    }
    function actions(item, lane) {
      if (item.kind === "pr") return prActions(item, null, lane);
      return issueActions(item, lane);
    }
    function compactCard(item) {
      const linked = item.linkedPrs || [];
      if (item.kind !== "issue" || !linked.length) {
        return '<div class="item-card ' + item.kind + '">' +
          "<div>" + entityHead(item) +
          '<div class="item-status">' + statusLine(item) + "</div></div>" +
          '<div class="item-actions">' + actions(item, active) + "</div></div>";
      }
      const issueRow = '<div class="pair-row issue">' +
        '<div>' + entityHead(item) +
        '<div class="item-status">' + statusLine(item) + "</div></div>" +
        '<div class="item-actions">' + issueActions(item, active) + "</div></div>";
      const prRows = linked.map((pr) => {
        const asItem = prAsItem(pr);
        return '<div class="pair-row pr">' +
          '<div>' + entityHead(asItem) +
          '<div class="item-status">' + statusLine(asItem) + "</div></div>" +
          '<div class="item-actions">' + prActions(asItem, item, active) + "</div></div>";
      }).join("");
      return '<div class="item-card pair">' + issueRow + prRows + "</div>";
    }
    function renderTabs() {
      document.getElementById("tabs").innerHTML = LANES.map((lane) => {
        const n = laneItems(lane.id).length;
        const cls = active === lane.id ? "tab-btn active" : "tab-btn";
        return '<button class="' + cls + '" data-tab="' + lane.id + '">' + lane.label + '<span class="tab-count">' + n + "</span></button>";
      }).join("");
    }
    function allKnownLabels() {
      const set = new Set();
      allItems().forEach((item) => {
        (item.labels || []).forEach((name) => set.add(name));
        (item.linkedPrs || []).forEach((pr) => (pr.labels || []).forEach((name) => set.add(name)));
      });
      return Array.from(set).sort();
    }
    function renderLabelFilter() {
      const toggle = document.getElementById("label-filter-toggle");
      const list = document.getElementById("label-filter-list");
      const chips = document.getElementById("label-chips");
      const q = (document.getElementById("label-filter-q").value || "").toLowerCase();
      const n = filters.labels.length;
      toggle.textContent = n ? "Labels (" + n + ")" : "Labels";
      toggle.classList.toggle("active", n > 0);
      chips.innerHTML = filters.labels.map((name) =>
        '<button type="button" class="filter-chip active" data-label-remove="' +
        encodeURIComponent(name) + '">' + escapeHtml(name) + "</button>"
      ).join("");
      const known = allKnownLabels().filter((name) => !q || name.toLowerCase().includes(q));
      list.innerHTML = known.length
        ? known.map((name) => {
          const checked = filters.labels.indexOf(name) >= 0 ? " checked" : "";
          return '<label class="label-option"><input type="checkbox" data-label="' +
            encodeURIComponent(name) + '"' + checked + "> " + escapeHtml(name) + "</label>";
        }).join("")
        : '<div class="session-empty">No labels</div>';
    }
    function renderFilterBar() {
      const bar = document.getElementById("filter-bar");
      const kind = document.getElementById("filter-kind");
      const advice = document.getElementById("filter-advice");
      const extra = document.getElementById("filter-extra");
      const labels = document.getElementById("label-filter");
      const chips = document.getElementById("label-chips");
      kind.style.display = "block";
      advice.style.display = active === "decide" ? "block" : "none";
      extra.style.display = "block";
      labels.style.display = "block";
      chips.style.display = "";
      if (kind.value !== filters.kind) kind.value = filters.kind;
      if (advice.value !== filters.advice) advice.value = filters.advice;
      if (extra.value !== filters.extra) extra.value = filters.extra;
      renderLabelFilter();
      const shown = filteredItems().length;
      const total = laneItems(active).length;
      document.getElementById("filter-count").textContent = shown + " of " + total;
      bar.style.display = "";
    }
    function renderTable() {
      const items = filteredItems();
      const root = document.getElementById("table");
      const total = laneItems(active).length;
      if (!total) {
        root.innerHTML = '<div class="empty">None in this lane.</div>';
        return;
      }
      if (!items.length) {
        root.innerHTML = '<div class="empty">No rows match these filters.</div>';
        return;
      }
      root.innerHTML = items.map(compactCard).join("");
    }
    function renderActivity() {
      const el = document.getElementById("activity");
      if (!state.notice) {
        el.style.display = "none";
        el.innerHTML = "";
        return;
      }
      el.style.display = "block";
      el.innerHTML = '<div class="activity-item ' + (state.noticeKind || "pending") + '">[*] ' + escapeHtml(state.notice) + "</div>";
    }
    function sessionLink(row) {
      const id = row.sessionId || row.session_id;
      if (!id) return '<span class="session-skill">no session id yet</span>';
      return '<a class="btn-open-session" href="ghapp://sessions/' + encodeURIComponent(id) + '">Open</a>';
    }
    function sessionTableRowHtml(row) {
      const activity = occupancyActivity(row);
      const status = activity === "pending" ? "Spawning" : (activity === "idle" ? "Idle" : "Working");
      const cls = activity === "pending" ? "human-triage" : (activity === "idle" ? "human-deferred" : "human-accepted");
      const role = row.role || (String(row.target || "").startsWith("scheduler:") ? "scheduler" : "worker");
      const nameClass = row.child ? "child-name" : "";
      return "<tr>" +
        '<td class="' + nameClass + '">' + escapeHtml(role) + "</td>" +
        '<td class="' + nameClass + '"><span class="session-name" title="' + escapeHtml(row.target || "") + '">' +
        escapeHtml(row.name || row.target) + "</span></td>" +
        '<td><span class="badge ' + cls + '">' + status + "</span></td>" +
        '<td class="session-actions">' + sessionLink(row) + "</td>" +
        "</tr>";
    }
    function sessionsOpen() {
      try { return localStorage.getItem(SESSIONS_OPEN_KEY) === "1"; } catch (_e) { return false; }
    }
    function setSessionsOpen(open) {
      try { localStorage.setItem(SESSIONS_OPEN_KEY, open ? "1" : "0"); } catch (_e) {}
    }
    function occupancySummary(rows) {
      const schedulers = rows.filter((row) => String(row.target || "").startsWith("scheduler:")).length;
      const workers = rows.length - schedulers;
      const working = rows.filter((row) => occupancyActivity(row) === "working" || occupancyActivity(row) === "pending").length;
      const idle = rows.length - working;
      if (!rows.length) return "none";
      const bits = [];
      if (schedulers) bits.push(schedulers + " scheduler" + (schedulers === 1 ? "" : "s"));
      if (workers) bits.push(workers + " worker" + (workers === 1 ? "" : "s"));
      if (working) bits.push(working + " working");
      if (idle) bits.push(idle + " idle");
      return bits.join(" · ");
    }
    function renderSessions() {
      const el = document.getElementById("sessions");
      const groups = state.sessions || [];
      const rows = state.occupancy || [];
      const open = sessionsOpen();
      el.style.display = "block";
      el.classList.toggle("collapsed", !open);
      const chevron = open ? "v" : ">";
      const tableRows = [];
      for (const group of groups) {
        if (group.scheduler) {
          tableRows.push(sessionTableRowHtml({
            ...group.scheduler,
            role: "scheduler",
            child: false,
          }));
        }
        for (const worker of group.workers || []) {
          tableRows.push(sessionTableRowHtml({
            ...worker,
            role: "worker",
            child: Boolean(group.scheduler),
          }));
        }
      }
      const body = tableRows.length
        ? '<table class="sessions-table"><thead><tr><th>Role</th><th>Session</th><th>Status</th><th>Actions</th></tr></thead><tbody>' +
          tableRows.join("") + "</tbody></table>"
        : '<div class="session-empty">No scheduler or worker sessions.</div>';
      el.innerHTML =
        '<button type="button" class="sessions-toggle" data-sessions-toggle>' +
        '<span class="sessions-chevron">' + chevron + "</span>" +
        "<span>Active sessions</span>" +
        '<span class="sessions-summary">' + occupancySummary(rows) + "</span></button>" +
        '<div class="sessions-body">' +
        '<div class="sessions-head"><span>Schedulers and workers</span>' +
        '<button type="button" class="btn-refresh" data-sessions-refresh>Refresh status</button></div>' +
        body + "</div>";
    }
    function renderSweeps() {
      const bar = document.getElementById("sweeps");
      const sweeps = SWEEPS[active] || [];
      if (!sweeps.length) {
        bar.style.display = "none";
        bar.innerHTML = "";
        return;
      }
      bar.style.display = "flex";
      bar.innerHTML = '<span class="bulk-count">Spawn sweeps</span>' + sweeps.map((sweep) => {
        const occ = occupancyFor(sweep.target);
        const localBusy = clickBusy.has(sweep.action);
        const busy = occupancyWorking(occ) || localBusy;
        const text = busy ? (occ && occupancyActivity(occ) === "working" ? "Working" : "Spawning...") : sweep.label;
        return '<button class="' + sweep.cls + '"' + (busy ? " disabled" : "") +
          ' data-spawn="' + sweep.action + '" data-target="' + sweep.target + '" data-label="' + sweep.label + '">' +
          text + "</button>";
      }).join("");
    }
    const LAYOUT_MQ = window.matchMedia("(min-width: 720px)");
    function isWide() {
      const canvas = document.documentElement.clientWidth || 0;
      return canvas >= 720 || (canvas === 0 && LAYOUT_MQ.matches);
    }
    function applyLayout() {
      document.body.setAttribute("data-layout", isWide() ? "wide" : "compact");
    }
    function render() {
      applyLayout();
      const err = document.getElementById("error");
      if (state.lastError) { err.style.display = "block"; err.textContent = state.lastError; }
      else { err.style.display = "none"; }
      const occupancy = state.occupancy || [];
      const pendingOcc = occupancy.some((row) => row.pending) || Boolean(state.notice);
      const live = document.querySelector(".live-dot");
      live.classList.toggle("pending", pendingOcc);
      const working = occupancy.filter((row) => occupancyWorking(row)).length;
      live.classList.toggle("running", working > 0 && !pendingOcc);
      document.getElementById("live-text").textContent = pendingOcc
        ? "Working -- waiting on Copilot to spawn or confirm a session."
        : occupancy.length
          ? "Live GitHub -- " + occupancy.length + " autopilot session(s) (" + working + " working)."
          : (state.lastUpdated
            ? "Live GitHub -- " + state.lastUpdated
            : "Connecting to GitHub...");
      renderActivity();
      renderSessions();
      renderSweeps();
      renderTabs();
      renderFilterBar();
      renderTable();
    }
    applyLayout();
    try {
      LAYOUT_MQ.addEventListener("change", () => { applyLayout(); render(); });
    } catch (_error) {
      LAYOUT_MQ.addListener(() => { applyLayout(); render(); });
    }
    function applyLocalItem(item) {
      if (!item || !item.number) return;
      const list = item.kind === "pr" ? (state.prs || (state.prs = [])) : (state.issues || (state.issues = []));
      const index = list.findIndex((row) => row.number === item.number);
      if (index >= 0) list[index] = Object.assign({}, list[index], item);
      else list.push(item);
      if (item.kind !== "pr") return;
      for (const issue of state.issues || []) {
        const linked = issue.linkedPrs || [];
        const linkedIndex = linked.findIndex((pr) => pr.number === item.number);
        if (linkedIndex < 0) continue;
        linked[linkedIndex] = Object.assign({}, linked[linkedIndex], {
          labels: item.labels,
          accepted: item.accepted,
          deferred: item.deferred,
          panelReview: item.panelReview
        });
      }
    }
    async function loadState() {
      const body = await api("/api/state", { method: "GET" });
      Object.assign(state, body);
      render();
      const empty = !(state.issues || []).length && !(state.prs || []).length;
      if (empty && !state.lastError && (loadState.retries || 0) < 8) {
        loadState.retries = (loadState.retries || 0) + 1;
        setTimeout(() => loadState().catch(() => {}), 750);
      }
    }
    function openModal(title, body, onOk) {
      pending = onOk;
      document.getElementById("modal-title").textContent = title;
      document.getElementById("modal-body").textContent = body;
      document.getElementById("modal").classList.add("open");
    }
    function closeModal() {
      pending = null;
      document.getElementById("modal").classList.remove("open");
    }
    document.getElementById("theme-select").addEventListener("change", (event) => {
      const pref = event.target.value === "light" || event.target.value === "dark" ? event.target.value : "auto";
      try { localStorage.setItem(THEME_KEY, pref); } catch (_error) {}
      applyTheme();
    });
    document.getElementById("filter-q").addEventListener("input", (event) => {
      filters.q = event.target.value || "";
      renderFilterBar();
      renderTable();
    });
    document.getElementById("filter-kind").addEventListener("change", (event) => {
      filters.kind = event.target.value || "all";
      renderFilterBar();
      renderTable();
    });
    document.getElementById("filter-advice").addEventListener("change", (event) => {
      filters.advice = event.target.value || "";
      renderFilterBar();
      renderTable();
    });
    document.getElementById("filter-extra").addEventListener("change", (event) => {
      filters.extra = event.target.value || "";
      renderFilterBar();
      renderTable();
    });
    document.getElementById("label-filter-toggle").addEventListener("click", (event) => {
      event.stopPropagation();
      const menu = document.getElementById("label-filter-menu");
      menu.hidden = !menu.hidden;
    });
    document.getElementById("label-filter-q").addEventListener("input", () => {
      renderLabelFilter();
    });
    document.getElementById("label-filter-list").addEventListener("change", (event) => {
      const box = event.target.closest("[data-label]");
      if (!box) return;
      const name = decodeURIComponent(box.getAttribute("data-label"));
      const set = new Set(filters.labels);
      if (box.checked) set.add(name);
      else set.delete(name);
      filters.labels = Array.from(set).sort();
      renderFilterBar();
      renderTable();
    });
    document.getElementById("label-chips").addEventListener("click", (event) => {
      const btn = event.target.closest("[data-label-remove]");
      if (!btn) return;
      const name = decodeURIComponent(btn.getAttribute("data-label-remove"));
      filters.labels = filters.labels.filter((row) => row !== name);
      renderFilterBar();
      renderTable();
    });
    document.addEventListener("click", (event) => {
      const wrap = document.getElementById("label-filter");
      if (!wrap || wrap.contains(event.target)) return;
      document.getElementById("label-filter-menu").hidden = true;
    });
    document.body.addEventListener("click", async (event) => {
      const tab = event.target.closest("[data-tab]");
      const spawn = event.target.closest("[data-spawn]");
      const refresh = event.target.closest("[data-refresh]");
      const sync = event.target.closest("[data-sync]");
      const act = event.target.closest("[data-act]");
      if (event.target.id === "modal-cancel" || event.target.id === "modal") { closeModal(); return; }
      if (event.target.id === "modal-ok" && pending) { const fn = pending; closeModal(); await fn(); return; }
      try {
        const sessionsRefresh = event.target.closest("[data-sessions-refresh]");
        if (sessionsRefresh) {
          event.stopPropagation();
          state.notice = "Refreshing scheduler and worker status...";
          state.noticeKind = "pending";
          render();
          await api("/sync-occupancy", { method: "POST", body: "{}" });
          state.notice = null;
          await loadState();
          return;
        }
        const sessionsToggle = event.target.closest("[data-sessions-toggle]");
        if (sessionsToggle) {
          const next = !sessionsOpen();
          setSessionsOpen(next);
          renderSessions();
          if (next) {
            state.notice = "Refreshing scheduler and worker status...";
            state.noticeKind = "pending";
            render();
            await api("/sync-occupancy", { method: "POST", body: "{}" });
            state.notice = null;
            await loadState();
          }
          return;
        }
        if (tab) { active = tab.getAttribute("data-tab"); render(); return; }
        if (refresh) {
          state.notice = "Refreshing live GitHub...";
          state.noticeKind = "saving";
          render();
          await api("/refresh", { method: "POST", body: "{}" });
          state.notice = null;
          await loadState();
          return;
        }
        if (sync) {
          state.notice = "Syncing Copilot sessions into occupancy...";
          state.noticeKind = "pending";
          render();
          await api("/sync-occupancy", { method: "POST", body: "{}" });
          state.notice = null;
          await loadState();
          return;
        }
        if (spawn) {
          const action = spawn.getAttribute("data-spawn");
          clickBusy.add(action);
          state.notice = "Asked Copilot to spawn " + (spawn.getAttribute("data-label") || action) + ".";
          state.noticeKind = "pending";
          render();
          await api("/spawn", { method: "POST", body: JSON.stringify({ action }) });
          clickBusy.delete(action);
          await loadState();
          return;
        }
        if (act) {
          const payload = JSON.parse(decodeURIComponent(act.getAttribute("data-act")));
          const run = async () => {
            if (payload.label) {
              state.notice = "Updating GitHub labels on #" + payload.label.number + "...";
              state.noticeKind = "saving";
              render();
              const result = await api("/label", { method: "POST", body: JSON.stringify(payload.label) });
              applyLocalItem(result.item);
              state.notice = null;
              render();
              return;
            }
            if (payload.spawn) {
              const key = payload.spawn.action + ":" + payload.spawn.number;
              clickBusy.add(key);
              clickBusy.add(payload.spawn.action);
              state.notice = "Asked Copilot to spawn " + payload.spawn.action + " #" + payload.spawn.number + ".";
              state.noticeKind = "pending";
              render();
              await api("/spawn", { method: "POST", body: JSON.stringify(payload.spawn) });
              clickBusy.delete(key);
              clickBusy.delete(payload.spawn.action);
            }
            state.notice = null;
            await loadState();
          };
          if (payload.label && payload.label.confirmClearAccepted) {
            openModal("Clear acceptance?", "Defer #" + payload.label.number + " and remove status/accepted?", run);
            return;
          }
          if (payload.spawn && payload.spawn.confirm) {
            openModal("Spawn merge-worker?", "Spawn autopilot-pr-merge-worker for PR #" + payload.spawn.number + "? The canvas will not merge.", async () => {
              payload.spawn.confirm = true;
              await run();
            });
            return;
          }
          await run();
        }
      } catch (error) {
        clickBusy.clear();
        state.notice = null;
        const err = document.getElementById("error");
        err.style.display = "block";
        err.textContent = error.message;
        render();
      }
    });
    try {
      const stream = new EventSource("/events");
      stream.onmessage = (event) => {
        const body = JSON.parse(event.data);
        if (body && body.ok !== false) {
          Object.assign(state, body);
          if (!clickBusy.size) state.notice = null;
          render();
        }
      };
    } catch (_error) {
      /* polling via loadState remains */
    }
    loadState().catch((error) => {
      const err = document.getElementById("error");
      err.style.display = "block";
      err.textContent = error.message;
    });
  </script>
</body>
</html>`;
}
