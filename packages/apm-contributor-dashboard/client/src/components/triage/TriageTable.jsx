import { For, Show } from "solid-js";
import ActionDropdown from "../ActionDropdown";
import { startTriageSession, openSession } from "../../services/api";
import { showToast } from "../Toast";

const decisionClasses = {
  accept: "decision-accept",
  "needs-design": "decision-needs-design",
  "decline-with-reason": "decision-decline",
  "defer-later": "decision-defer",
  "auto-handle": "decision-auto-handle",
  "duplicate-of": "decision-duplicate",
};

function decisionClass(decision) {
  for (const key of Object.keys(decisionClasses)) {
    if ((decision || "").startsWith(key)) return decisionClasses[key];
  }
  return "decision-defer";
}

function decisionLabel(decision) {
  if (!decision) return "--";
  if (decision.startsWith("decline-with-reason")) return "Decline";
  if (decision.startsWith("duplicate-of")) return "Duplicate";
  const labels = { accept: "Accept", "needs-design": "Needs Design", "defer-later": "Defer", "auto-handle": "Auto" };
  return labels[decision] || decision;
}

function priorityClass(priority) {
  if (!priority) return "prio-normal";
  if (priority.includes("critical")) return "prio-critical";
  if (priority.includes("high")) return "prio-high";
  if (priority.includes("low")) return "prio-low";
  return "prio-normal";
}

function priorityLabel(priority) {
  if (!priority) return "--";
  if (priority.includes("critical")) return "P0";
  if (priority.includes("high")) return "P1";
  if (priority.includes("medium") || priority.includes("normal")) return "P2";
  if (priority.includes("low")) return "P3";
  return priority.replace("priority/", "");
}

function typeLabel(type) {
  if (!type) return "--";
  return type.replace("type/", "");
}

export default function TriageTable(props) {
  function sortIndicator(col) {
    if (props.sortCol() !== col) return "";
    return props.sortAsc() ? " ^" : " v";
  }

  async function handleStart(item) {
    try {
      await startTriageSession(item.number, item.title);
      showToast(`Session started for #${item.number}`);
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  async function handleOpen(item) {
    try {
      await openSession(item.number, item.title);
      showToast(`Opening session for #${item.number}`);
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  return (
    <table>
      <thead>
        <tr>
          <th class="clickable sortable" onClick={() => props.onSort("number")}>Issue{sortIndicator("number")}</th>
          <th>Title</th>
          <th class="clickable sortable" onClick={() => props.onSort("decision")}>Decision{sortIndicator("decision")}</th>
          <th class="clickable sortable" onClick={() => props.onSort("priority")}>Priority{sortIndicator("priority")}</th>
          <th class="clickable sortable" onClick={() => props.onSort("type")}>Type{sortIndicator("type")}</th>
          <th class="clickable sortable" onClick={() => props.onSort("status")}>Status{sortIndicator("status")}</th>
          <th class="clickable sortable" onClick={() => props.onSort("milestone")}>Milestone{sortIndicator("milestone")}</th>
          <th>Next Action</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        <For each={props.items}>
          {(item) => (
            <tr>
              <td><a href={item.url} target="_blank" rel="noreferrer noopener">#{item.number}</a></td>
              <td class="title-cell" title={item.title}>{item.title}</td>
              <td>
                <span
                  class={`badge ${decisionClass(item.decision)} filterable`}
                  onClick={() => props.onFilter("decision", item.decision)}
                >
                  {decisionLabel(item.decision)}
                </span>
              </td>
              <td>
                <span
                  class={`badge ${priorityClass(item.priority)} filterable`}
                  onClick={() => props.onFilter("priority", item.priority)}
                >
                  {priorityLabel(item.priority)}
                </span>
              </td>
              <td>
                <span
                  class={`badge type-${typeLabel(item.type)} filterable`}
                  onClick={() => props.onFilter("type", item.type)}
                >
                  {typeLabel(item.type)}
                </span>
              </td>
              <td>
                <span class="badge" style={{ background: "#1f6feb20", color: "#58a6ff" }}>
                  {item.status ? item.status.replace("status/", "") : "--"}
                </span>
              </td>
              <td>{item.milestone || <span class="no-pr">--</span>}</td>
              <td class="title-cell" title={item.nextAction}>{item.nextAction || "--"}</td>
              <td class="action-cell">
                <ActionDropdown
                  onDetails={() => props.onDetail(item)}
                  items={[
                    item.hasSession
                      ? { label: "Go to Active Session", class: "dropdown-session", action: () => handleOpen(item) }
                      : { label: "Start Session", class: "dropdown-session", action: () => handleStart(item) },
                  ]}
                />
              </td>
            </tr>
          )}
        </For>
      </tbody>
    </table>
  );
}
