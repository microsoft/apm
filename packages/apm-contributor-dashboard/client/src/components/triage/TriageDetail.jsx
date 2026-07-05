import { createSignal, Show } from "solid-js";
import Modal from "../Modal";
import { renderMarkdown } from "../../utils/markdown";
import { startTriageSession } from "../../services/api";
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
  const detail = decision.startsWith("decline-with-reason:") ? `: ${decision.slice("decline-with-reason:".length).trim()}` : "";
  const base = decision.startsWith("decline-with-reason") ? "Decline" :
               decision.startsWith("duplicate-of") ? `Duplicate ${decision.slice("duplicate-of:".length || 0).trim()}` :
               { accept: "Accept", "needs-design": "Needs Design", "defer-later": "Defer", "auto-handle": "Auto-handle" }[decision] || decision;
  return base + detail;
}

export default function TriageDetail(props) {
  const item = () => props.item;
  const [subTab, setSubTab] = createSignal("summary");

  const subTabs = [
    { id: "summary", label: "Summary" },
    { id: "comment", label: "Triage Comment" },
    { id: "labels", label: "Labels" },
  ];

  async function handleStart() {
    try {
      await startTriageSession(item().number, item().title);
      showToast(`Session started for #${item().number}`);
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  const footer = () => (
    <div class="modal-actions">
      <button class="btn btn-primary" onClick={handleStart}>Start Session</button>
      <a class="btn btn-secondary" href={item()?.url} target="_blank">View on GitHub</a>
    </div>
  );

  return (
    <Modal
      open={() => item() !== null}
      title={() => item() ? `#${item().number} -- Triage Decision` : ""}
      onClose={props.onClose}
      footer={footer()}
    >
      <Show when={item()}>
        {(d) => (
          <>
            <div class="meta-row">
              <div class="meta-item"><strong>Issue:</strong> #{d().number}</div>
              <div class="meta-item">
                <strong>Decision:</strong>{" "}
                <span class={`badge ${decisionClass(d().decision)}`}>{decisionLabel(d().decision)}</span>
              </div>
              <div class="meta-item"><strong>Triaged by:</strong> <code>{d().triageAuthor}</code></div>
              <div class="meta-item"><strong>Date:</strong> {d().triageCreatedAt ? new Date(d().triageCreatedAt).toLocaleDateString() : "--"}</div>
            </div>

            <div class="activity-body-tabs">
              {subTabs.map((tab) => (
                <button
                  class={`activity-body-tab ${subTab() === tab.id ? "active" : ""}`}
                  onClick={() => setSubTab(tab.id)}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            <Show when={subTab() === "summary"}>
              <div class="triage-summary-grid">
                <div class="triage-summary-item">
                  <div class="ts-label">Priority</div>
                  <div class="ts-value">{d().priority ? d().priority.replace("priority/", "") : "--"}</div>
                </div>
                <div class="triage-summary-item">
                  <div class="ts-label">Type</div>
                  <div class="ts-value">{d().type ? d().type.replace("type/", "") : "--"}</div>
                </div>
                <div class="triage-summary-item">
                  <div class="ts-label">Status</div>
                  <div class="ts-value">{d().status ? d().status.replace("status/", "") : "--"}</div>
                </div>
                <div class="triage-summary-item">
                  <div class="ts-label">Milestone</div>
                  <div class="ts-value">{d().milestone || "--"}</div>
                </div>
                <Show when={d().theme}>
                  <div class="triage-summary-item">
                    <div class="ts-label">Theme</div>
                    <div class="ts-value">{d().theme}</div>
                  </div>
                </Show>
                <Show when={d().decisionDetail}>
                  <div class="triage-summary-item">
                    <div class="ts-label">Detail</div>
                    <div class="ts-value">{d().decisionDetail}</div>
                  </div>
                </Show>
              </div>
              <Show when={d().nextAction}>
                <div class="triage-next-action">
                  <div class="ts-label">Next Action</div>
                  <div class="ts-value">{d().nextAction}</div>
                </div>
              </Show>
            </Show>

            <Show when={subTab() === "comment"}>
              <div class="issue-body" innerHTML={renderMarkdown(d().commentBody)} />
            </Show>

            <Show when={subTab() === "labels"}>
              <div style={{ "margin-top": "12px" }}>
                <Show when={d().theme}>
                  <div style={{ "margin-bottom": "8px" }}>
                    <div style={{ "font-size": "11px", "color": "#8b949e", "margin-bottom": "4px", "text-transform": "uppercase", "letter-spacing": "0.5px" }}>Theme</div>
                    <span class="triage-area-chip">{d().theme}</span>
                  </div>
                </Show>
                <Show when={d().areas?.length > 0}>
                  <div style={{ "margin-bottom": "8px" }}>
                    <div style={{ "font-size": "11px", "color": "#8b949e", "margin-bottom": "4px", "text-transform": "uppercase", "letter-spacing": "0.5px" }}>Areas</div>
                    <div class="triage-areas">
                      {d().areas.map(a => <span class="triage-area-chip">{a}</span>)}
                    </div>
                  </div>
                </Show>
                <Show when={d().preservedLabels?.length > 0}>
                  <div>
                    <div style={{ "font-size": "11px", "color": "#8b949e", "margin-bottom": "4px", "text-transform": "uppercase", "letter-spacing": "0.5px" }}>Preserved Labels</div>
                    <div class="triage-areas">
                      {d().preservedLabels.map(l => <span class="label-tag">{l}</span>)}
                    </div>
                  </div>
                </Show>
                <Show when={!d().theme && !d().areas?.length && !d().preservedLabels?.length}>
                  <div class="empty">No label information available.</div>
                </Show>
              </div>
            </Show>
          </>
        )}
      </Show>
    </Modal>
  );
}
