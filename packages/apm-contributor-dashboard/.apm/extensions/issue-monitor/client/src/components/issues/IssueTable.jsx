import { For } from "solid-js";
import ActionDropdown from "../ActionDropdown";
import { startSession, openSession } from "../../services/api";
import { showToast } from "../Toast";

const prioClasses = { P0: "prio-critical", P1: "prio-high", P2: "prio-normal", P3: "prio-low" };
const statusClasses = { available: "status-available", planning: "status-planning", implementing: "status-implementing", "in-review": "status-review", merged: "status-merged" };
const statusLabels = { available: "Available", planning: "Planning", implementing: "Implementing", "in-review": "In Review", merged: "Merged" };
const prStatusLabels = { draft: "Draft", "ci-failing": "CI Failing", "ci-pending": "CI Pending", "changes-requested": "Changes Requested", "review-pending": "Review Pending", "ready-to-merge": "Ready to Merge" };
const prStatusClasses = { draft: "pr-status-draft", "ci-failing": "pr-status-failing", "ci-pending": "pr-status-pending", "changes-requested": "pr-status-changes", "review-pending": "pr-status-review", "ready-to-merge": "pr-status-ready" };

export default function IssueTable(props) {
  function sortIndicator(col) {
    if (props.sortCol() !== col) return "";
    return props.sortAsc() ? " ▲" : " ▼";
  }

  async function handleStart(issue) {
    try {
      await startSession(issue.number, issue.title);
      showToast(`Session started for #${issue.number}`);
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  async function handleOpen(issue) {
    try {
      await openSession(issue.number, issue.title);
      showToast(`Opening session for #${issue.number}`);
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Issue</th>
          <th>Title</th>
          <th class="clickable sortable" onClick={() => props.onSort("priority")}>Priority{sortIndicator("priority")}</th>
          <th class="clickable sortable" onClick={() => props.onSort("type")}>Type{sortIndicator("type")}</th>
          <th class="clickable sortable" onClick={() => props.onSort("author")}>Author{sortIndicator("author")}</th>
          <th class="clickable sortable" onClick={() => props.onSort("status")}>Status{sortIndicator("status")}</th>
          <th>PR</th>
          <th>PR Status</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        <For each={props.issues}>
          {(issue) => {
            const dropdownItems = [];
            if (issue.hasSession) {
              dropdownItems.push({ label: "Open Session", class: "dropdown-session", action: () => handleOpen(issue) });
            } else if (issue.status === "available") {
              dropdownItems.push({ label: "Start Session", class: "dropdown-session", action: () => handleStart(issue) });
            }
            return (
              <tr>
                <td><a href={issue.url} target="_blank">#{issue.number}</a></td>
                <td class="title-cell" title={issue.title}>{issue.title}</td>
                <td><span class={`badge ${prioClasses[issue.priority] || "prio-normal"} filterable`} onClick={() => props.onFilter("priority", issue.priority)}>{issue.priority}</span></td>
                <td><span class={`badge type-${issue.type} filterable`} onClick={() => props.onFilter("type", issue.type)}>{issue.type}</span></td>
                <td><code class="filterable" onClick={() => props.onFilter("author", issue.author)}>{issue.author}</code></td>
                <td><span class={`badge ${statusClasses[issue.status] || "status-available"} filterable`} onClick={() => props.onFilter("status", issue.status)}>{statusLabels[issue.status] || issue.status}</span></td>
                <td class="pr-cell">
                  {issue.pr
                    ? <a href={issue.pr.url} target="_blank" class="pr-link">#{issue.pr.number}</a>
                    : <span class="no-pr">--</span>
                  }
                </td>
                <td class="pr-cell">
                  {issue.pr
                    ? <span class={`badge ${prStatusClasses[issue.pr.prStatus] || "pr-status-review"}`}>{prStatusLabels[issue.pr.prStatus] || issue.pr.prStatus}</span>
                    : <span class="no-pr">--</span>
                  }
                </td>
                <td class="action-cell">
                  <ActionDropdown onDetails={() => props.onDetail(issue)} items={dropdownItems} />
                </td>
              </tr>
            );
          }}
        </For>
      </tbody>
    </table>
  );
}
