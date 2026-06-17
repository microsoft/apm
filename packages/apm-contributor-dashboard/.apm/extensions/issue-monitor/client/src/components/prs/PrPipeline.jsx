import { Show, For } from "solid-js";

const statusIcons = { success: "[+]", failure: "[x]", pending: "[>]", skipped: "[-]", neutral: "[~]", action_required: "[!]" };

function classifyConclusion(check) {
  const c = (check.conclusion || "").toLowerCase();
  const s = (check.status || "").toLowerCase();
  if (c === "success") return "success";
  if (c === "failure") return "failure";
  if (c === "action_required") return "action_required";
  if (c === "skipped") return "skipped";
  if (c === "neutral") return "neutral";
  if (s === "in_progress" || s === "queued" || s === "pending") return "pending";
  if (c) return c;
  return "pending";
}

export default function PrPipeline(props) {
  const checks = () => props.checks || [];
  const runs = () => props.workflowRuns || [];

  const passed = () => checks().filter(c => classifyConclusion(c) === "success").length;
  const failed = () => checks().filter(c => classifyConclusion(c) === "failure").length;
  const pending = () => checks().filter(c => classifyConclusion(c) === "pending" || classifyConclusion(c) === "action_required").length;
  const total = () => checks().length;

  const sorted = () => {
    const order = { failure: 0, action_required: 1, pending: 2, success: 3, neutral: 4, skipped: 5 };
    return [...checks()].sort((a, b) => (order[classifyConclusion(a)] ?? 9) - (order[classifyConclusion(b)] ?? 9));
  };

  return (
    <div class="pipeline-container">
      <Show when={total() > 0} fallback={
        <div class="pipeline-empty">
          <div class="pipeline-empty-icon">[?]</div>
          <div class="pipeline-empty-text">No checks found</div>
        </div>
      }>
        <div class="pipeline-summary-row">
          <div class="pipeline-summary-card pass"><div class="stat-num">{passed()}</div><div class="stat-label">Passed</div></div>
          <div class="pipeline-summary-card fail"><div class="stat-num">{failed()}</div><div class="stat-label">Failed</div></div>
          <div class="pipeline-summary-card pending"><div class="stat-num">{pending()}</div><div class="stat-label">Pending</div></div>
          <div class="pipeline-summary-card total"><div class="stat-num">{total()}</div><div class="stat-label">Total</div></div>
        </div>
        <div class="pipeline-list">
          <For each={sorted()}>
            {(check) => {
              const cls = classifyConclusion(check);
              return (
                <div class="pipeline-item">
                  <span class={`pipeline-icon ${cls}`}>{statusIcons[cls] || "[?]"}</span>
                  <span class="pipeline-name">
                    {check.url ? <a href={check.url} target="_blank">{check.name}</a> : check.name}
                  </span>
                  <span class={`pipeline-status-badge ${cls}`}>{cls}</span>
                </div>
              );
            }}
          </For>
        </div>
      </Show>
      <Show when={runs().length > 0}>
        <div class="pipeline-section-title">Workflow Runs</div>
        <div class="pipeline-list">
          <For each={runs()}>
            {(run) => {
              const cls = run.conclusion || (run.status === "in_progress" ? "pending" : "pending");
              return (
                <div class="pipeline-item">
                  <span class={`pipeline-icon ${cls}`}>{statusIcons[cls] || "[>]"}</span>
                  <span class="pipeline-name">
                    {run.url ? <a href={run.url} target="_blank">{run.name}</a> : run.name}
                  </span>
                  <span class={`pipeline-status-badge ${cls}`}>{run.conclusion || run.status}</span>
                  <span class="pipeline-time">{run.createdAt ? new Date(run.createdAt).toLocaleString() : ""}</span>
                </div>
              );
            }}
          </For>
        </div>
      </Show>
    </div>
  );
}
