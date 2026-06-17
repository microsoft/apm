import { For } from "solid-js";
import ActionDropdown from "../ActionDropdown";
import { runPanel, rerunCi, approvePr, approveWorkflowRuns, mergeWhenReady } from "../../services/api";
import { showToast } from "../Toast";

const prStatusLabels = { draft: "Draft", "ci-failing": "CI Failing", "ci-pending": "CI Pending", "changes-requested": "Changes Requested", "review-pending": "Review Pending", "ready-to-merge": "Ready to Merge" };
const prStatusClasses = { draft: "pr-status-draft", "ci-failing": "pr-status-failing", "ci-pending": "pr-status-pending", "changes-requested": "pr-status-changes", "review-pending": "pr-status-review", "ready-to-merge": "pr-status-ready" };
const pipelineClasses = { green: "signal-green", yellow: "signal-yellow", red: "signal-red", orange: "signal-orange", none: "signal-none" };
const panelClasses = { green: "signal-green", yellow: "signal-yellow", red: "signal-red", none: "signal-none" };

export default function PrTable(props) {
  function buildDropdownItems(pr) {
    const items = [];
    if (pr.pipeline?.status === "red" || pr.pipeline?.status === "yellow") {
      items.push({ label: "Re-run CI", class: "dropdown-rerun", action: async () => { await rerunCi(pr.number); showToast("CI re-run triggered"); } });
    }
    if (pr.pipeline?.status === "orange") {
      items.push({ label: "Approve Runs", class: "dropdown-approve", action: async () => { await approveWorkflowRuns(pr.branch || ""); showToast("Workflow runs approved"); } });
    }
    items.push({ label: "Run Panel", class: "dropdown-panel", action: async () => { await runPanel(pr.number); showToast("Panel review triggered"); } });
    if (pr.prStatus === "review-pending" || pr.prStatus === "ready-to-merge") {
      items.push({ label: "Approve", class: "dropdown-approve", action: async () => { await approvePr(pr.number); showToast("PR approved"); } });
    }
    if (pr.prStatus === "ready-to-merge") {
      items.push({ label: "Merge", class: "dropdown-merge", action: async () => { await mergeWhenReady(pr.number); showToast("Merge queued"); } });
    }
    return items;
  }

  function panelLabel(pr) {
    if (pr.panelCounts) {
      return `B:${pr.panelCounts.blocking} R:${pr.panelCounts.review} N:${pr.panelCounts.nit}`;
    }
    return pr.panel?.label || "Not requested";
  }

  return (
    <table>
      <thead>
        <tr>
          <th>PR</th>
          <th>Title</th>
          <th class="clickable">Author</th>
          <th class="clickable">Pipeline</th>
          <th class="clickable">Panel</th>
          <th class="clickable">Review</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        <For each={props.prs}>
          {(pr) => (
            <tr>
              <td><a href={pr.url} target="_blank">#{pr.number}</a></td>
              <td class="title-cell" title={pr.title}>{pr.title}</td>
              <td><code class="filterable" onClick={() => props.onFilter("author", pr.author)}>{pr.author}</code></td>
              <td class="filterable" onClick={() => props.onFilter("pipeline", pr.pipeline?.status)}>
                <span class={`signal ${pipelineClasses[pr.pipeline?.status] || "signal-none"}`}>{pr.pipeline?.label || "Unknown"}</span>
              </td>
              <td class="filterable" onClick={() => props.onFilter("panel", pr.panel?.status)}>
                <span class={`signal ${panelClasses[pr.panel?.status] || "signal-none"}`}>{panelLabel(pr)}</span>
              </td>
              <td>
                <span class={`badge ${prStatusClasses[pr.prStatus] || "pr-status-review"} filterable`} onClick={() => props.onFilter("prstatus", pr.prStatus)}>
                  {prStatusLabels[pr.prStatus] || pr.prStatus}
                </span>
              </td>
              <td class="action-cell">
                <ActionDropdown onDetails={() => props.onDetail(pr)} items={buildDropdownItems(pr)} />
              </td>
            </tr>
          )}
        </For>
      </tbody>
    </table>
  );
}
