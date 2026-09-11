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
          <th class="clickable sortable" onClick={() => props.onSort("decision")}>Recommendation{sortIndicator("decision")}</th>
          <th class="clickable sortable" onClick={() => props.onSort("type")}>Type{sortIndicator("type")}</th>
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
                  {item.legacy ? "Legacy advice: " : "Recommend: "}{decisionLabel(item.decision)}
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
