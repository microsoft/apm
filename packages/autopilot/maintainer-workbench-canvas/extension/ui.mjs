export function renderHtml(token) {
  return `<!doctype html>
<!--
THESIS: Explain the decision while keeping the project's queues in sight, not a wall of status cards.
OWN-WORLD: Host semantic neutrals and Primer functional state colors; blue selection and recommendation, amber attention, system sans.
STORY: Reconcile a decision, accept scope, plan priorities, then explicitly confirm bounded work.
FIRST VIEWPORT: Compact workspace; queue-local filters beside status, one action and related PRs.
FORM: User-pinned co-visible queue/workbench; responsive vertical stack. No concept seed: structure already approved.
-->
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="canvas-token" content="${token}">
  <title>APM maintainer workbench</title>
  <link rel="stylesheet" href="/style.css">
  <script type="module" src="/app.js"></script>
</head>
<body>
  <a class="skip" href="#decision">Skip to selected decision</a>
  <header class="topbar">
    <div><h1>APM maintainer workbench</h1></div>
    <div class="header-actions"><a id="project-link" href="https://github.com/microsoft/apm" target="_blank" rel="noopener noreferrer">microsoft/apm</a><button id="refresh" type="button" aria-describedby="refresh-help" title="Get the latest issue, pull request, check and roadmap status. Does not change GitHub or start work.">Check GitHub for updates</button><span id="refresh-help" hidden>Get the latest issue, pull request, check and roadmap status. Does not change GitHub or start work.</span></div>
  </header>
  <div id="status" class="status" role="status" aria-live="polite">Loading the local snapshot. No work is being dispatched.</div>
  <p id="update-schedule" class="update-schedule">Auto-update every 4 min while open. Updates pause in a hidden tab.</p>
  <div id="view-notice" class="view-notice" role="status" aria-live="polite" hidden></div>
  <div id="error" class="error" role="alert" hidden></div>
  <nav id="workflow-nav" class="workflow-nav" aria-label="Maintainer workflow">
    <button id="attention-home" type="button">Attention</button><button data-view="triage" type="button">Triage</button>
    <button data-view="roadmap" type="button">Roadmap</button><button data-view="delivery" type="button">Delivery</button>
    <span class="secondary-nav"><button data-view="deferred" type="button">Deferred</button><button data-view="history" type="button">History</button></span>
  </nav>
  <nav id="attention-nav" class="attention-nav" aria-label="Attention categories">
    <div id="attention-groups" class="segments"></div>
    <p>Scope decisions count issues; permissions and reviews count PRs. A PR can appear in both. Counts are not additive.</p>
  </nav>
  <main class="workspace">
    <aside class="queue-pane" aria-label="Work queues">
  <section class="filters" aria-label="Queue filters">
    <div class="scope-header"><h2 id="scope-heading">Scope decisions</h2><span id="scope-total" class="counter">Not loaded</span><span id="count-unit" class="muted">Issues</span></div>
    <p id="attention-purpose" class="attention-purpose"></p>
    <div id="planning-filter" class="horizon-row" hidden><strong>Planning</strong><div id="horizons" class="segments" role="group" aria-label="Roadmap planning"></div><span class="muted">Priority, not permission</span></div>
    <label class="search-label">Find work<input id="search" type="search" placeholder="Issue, PR, title or label" maxlength="200"></label>
    <details class="secondary-filters"><summary>Filters and view details</summary><div class="filter-row">
      <label>Person<select id="person"><option value="">Anyone</option></select></label>
      <label>Area / label<select id="area"><option value="">All areas</option></select></label>
    </div>
    <p id="scope-basis" class="scope-basis">Decision evidence is being reconciled.</p>
    <p id="count-basis" class="count-explanation"></p><p id="queue-help" class="muted queue-help">Each issue appears once in this view.</p></details>
  </section>
      <div id="batch-controls" class="batch-controls" hidden><button id="triage-batch" type="button">Triage selected issues</button><span id="batch-count" class="muted">None selected</span></div>
      <div id="queues"><p class="empty">Evidence has not loaded yet. Refresh will read GitHub without making changes.</p></div>
      <div id="unlinked"></div>
    </aside>
    <div class="detail-pane">
    <button id="back-to-list" class="back-to-list" type="button">Back to issues</button>
    <section id="action-composer" class="action-composer" tabindex="-1" aria-label="Action preview" hidden></section>
    <article id="decision" class="decision" tabindex="-1" aria-label="Selected work item">
      <h2>Your next decision, in context</h2><p>Select an issue or pull request to see its problem, linked delivery, recommendation and evidence.</p>
    </article>
    </div>
  </main>
  <details id="request-panel" class="request-panel"><summary>Requests and existing work</summary><p id="feed-status" class="muted">Execution not observed.</p><div id="request-feedback" role="status" aria-live="polite"></div><div id="request-list"></div></details>
  <footer><details id="coverage"><summary>Sources, coverage and limits</summary><div id="source-health"></div></details>
    <details><summary>Connection diagnostics</summary><p>Private session-local control plane. Scope records and transport acknowledgements are not run permission. Workflow permission can be approved here only after reviewing and explicitly confirming the exact account, commit and workflows. Other operational requests retain their parent confirmation gates.</p>
    <button id="bridge-smoke" type="button">Test parent connection (fixture only)</button></details></footer>
</body></html>`;
}
