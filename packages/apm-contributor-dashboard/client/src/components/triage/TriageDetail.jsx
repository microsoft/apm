import { Show, For } from "solid-js";
import Modal from "../Modal";
import { renderMarkdown } from "../../utils/markdown";
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
  const base = decision.startsWith("decline-with-reason") ? "Decline" :
               decision.startsWith("duplicate-of") ? `Duplicate ${decision.slice(("duplicate-of:").length).trim()}` :
               { accept: "Accept", "needs-design": "Needs Design", "defer-later": "Defer", "auto-handle": "Auto-handle" }[decision] || decision;
  return base;
}

function priorityLabel(p) {
  if (!p) return null;
  if (p.includes("critical")) return "P0";
  if (p.includes("high")) return "P1";
  if (p.includes("medium") || p.includes("normal")) return "P2";
  if (p.includes("low")) return "P3";
  return p.replace("priority/", "");
}

function priorityClass(p) {
  if (!p) return "prio-normal";
  if (p.includes("critical")) return "prio-critical";
  if (p.includes("high")) return "prio-high";
  if (p.includes("low")) return "prio-low";
  return "prio-normal";
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) +
         " at " + d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

// Parse <details><summary>...</summary>...\n</details> blocks from raw markdown.
function parseLensNotes(body) {
  const notes = [];
  const re = /<details>\s*<summary>([\s\S]*?)<\/summary>([\s\S]*?)<\/details>/g;
  let m;
  while ((m = re.exec(body)) !== null) {
    notes.push({ title: m[1].trim(), body: m[2].trim() });
  }
  return notes;
}

function isBotAuthor(login) {
  return login && (login.endsWith("[bot]") || login.includes("copilot") || login === "github-actions");
}

export default function TriageDetail(props) {
  const item = () => props.item;

  async function handleStart() {
    try {
      await startTriageSession(item().number, item().title);
      showToast(`Session started for #${item().number}`);
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  async function handleOpen() {
    try {
      await openSession(item().number, item().title);
      showToast(`Opening session for #${item().number}`);
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  const footer = () => (
    <div class="modal-actions">
      <a class="btn btn-secondary" href={item()?.url} target="_blank" rel="noreferrer noopener">View on GitHub</a>
      <Show when={item()?.hasSession}>
        <button class="btn btn-primary btn-open-session" onClick={handleOpen}>Go to Active Session</button>
      </Show>
      <Show when={!item()?.hasSession}>
        <button class="btn btn-primary" onClick={handleStart}>Start Session</button>
      </Show>
    </div>
  );

  return (
    <Modal
      open={() => item() !== null}
      title={() => item() ? `#${item().number} -- ${item().title}` : ""}
      onClose={props.onClose}
      footer={footer()}
    >
      <Show when={item()}>
        {(d) => {
          const lensNotes = () => parseLensNotes(d().commentBody || "");
          const hasDiscussion = () => (d().nonTriageComments || []).length > 0;
          return (
            <>
              {/* Decision metadata chips row */}
              <div class="td-meta-bar">
                <span class={`badge ${decisionClass(d().decision)}`}>{decisionLabel(d().decision)}</span>
                <Show when={priorityLabel(d().priority)}>
                  <span class={`badge ${priorityClass(d().priority)}`}>{priorityLabel(d().priority)}</span>
                </Show>
                <Show when={d().type}>
                  <span class="badge" style={{ background: "#1f2328", border: "1px solid #30363d", color: "#e6edf3" }}>
                    {d().type.replace("type/", "")}
                  </span>
                </Show>
                <Show when={d().status}>
                  <span class="badge" style={{ background: "#1f6feb20", color: "#58a6ff" }}>
                    {d().status.replace("status/", "")}
                  </span>
                </Show>
                <Show when={d().milestone}>
                  <span class="badge" style={{ background: "#30363d", color: "#e6edf3" }}>{d().milestone}</span>
                </Show>
                <span class="td-triaged-by">triaged by <code>{d().triageAuthor}</code></span>
              </div>

              {/* Classification labels row (theme + areas + preserved) */}
              <Show when={d().theme || d().areas?.length > 0 || d().preservedLabels?.length > 0}>
                <div class="td-labels-row">
                  <Show when={d().theme}>
                    <span class="triage-area-chip td-chip-theme">{d().theme}</span>
                  </Show>
                  <For each={d().areas || []}>
                    {(a) => <span class="triage-area-chip">{a}</span>}
                  </For>
                  <For each={d().preservedLabels || []}>
                    {(l) => <span class="label-tag">{l}</span>}
                  </For>
                </div>
              </Show>

              {/* Next Action callout */}
              <Show when={d().nextAction}>
                <div class="td-next-action">
                  <div class="td-section-label">Next Action</div>
                  <div class="td-next-action-body">{d().nextAction}</div>
                </div>
              </Show>

              {/* Original issue body -- expandable */}
              <Show when={d().issueBody}>
                <details class="td-expandable">
                  <summary class="td-expandable-summary">Original Issue</summary>
                  <div class="td-expandable-body issue-body"
                    innerHTML={renderMarkdown(d().issueBody)} />
                </details>
              </Show>

              {/* Discussion (non-triage comments) -- expandable */}
              <Show when={hasDiscussion()}>
                <details class="td-expandable">
                  <summary class="td-expandable-summary">
                    Discussion
                    <span class="td-count-badge">{(d().nonTriageComments || []).length}</span>
                  </summary>
                  <div class="td-expandable-body td-comments-thread">
                    <For each={d().nonTriageComments || []}>
                      {(c) => (
                        <div class={`td-comment ${isBotAuthor(c.author) ? "td-comment-bot" : "td-comment-human"}`}>
                          <div class="td-comment-header">
                            <span class="td-comment-author">{c.author}</span>
                            <Show when={isBotAuthor(c.author)}>
                              <span class="td-comment-role-badge">bot</span>
                            </Show>
                            <span class="td-comment-date">{fmtDate(c.createdAt)}</span>
                          </div>
                          <div class="td-comment-body issue-body" innerHTML={renderMarkdown(c.body)} />
                        </div>
                      )}
                    </For>
                  </div>
                </details>
              </Show>

              {/* Suggested comment -- expandable */}
              <Show when={d().commentMarkdown}>
                <details class="td-expandable">
                  <summary class="td-expandable-summary">Suggested Comment</summary>
                  <div class="td-expandable-body issue-body"
                    innerHTML={renderMarkdown(d().commentMarkdown)} />
                </details>
              </Show>

              {/* Per-lens notes -- one expandable per lens */}
              <Show when={lensNotes().length > 0}>
                <div class="td-section-label" style={{ "margin-top": "16px", "margin-bottom": "4px" }}>
                  Per-lens Notes
                </div>
                <For each={lensNotes()}>
                  {(note) => (
                    <details class="td-expandable">
                      <summary class="td-expandable-summary">{note.title}</summary>
                      <div class="td-expandable-body issue-body"
                        innerHTML={renderMarkdown(note.body)} />
                    </details>
                  )}
                </For>
              </Show>

              {/* Labels -- expandable */}
              <Show when={d().theme || d().areas?.length > 0 || d().preservedLabels?.length > 0}>
                <details class="td-expandable">
                  <summary class="td-expandable-summary">Labels</summary>
                  <div class="td-expandable-body">
                    <Show when={d().theme}>
                      <div class="td-label-group">
                        <div class="td-chip-label">Theme</div>
                        <span class="triage-area-chip">{d().theme}</span>
                      </div>
                    </Show>
                    <Show when={d().areas?.length > 0}>
                      <div class="td-label-group">
                        <div class="td-chip-label">Areas</div>
                        <div class="triage-areas">
                          {d().areas.map(a => <span class="triage-area-chip">{a}</span>)}
                        </div>
                      </div>
                    </Show>
                    <Show when={d().preservedLabels?.length > 0}>
                      <div class="td-label-group">
                        <div class="td-chip-label">Preserved</div>
                        <div class="triage-areas">
                          {d().preservedLabels.map(l => <span class="label-tag">{l}</span>)}
                        </div>
                      </div>
                    </Show>
                  </div>
                </details>
              </Show>
            </>
          );
        }}
      </Show>
    </Modal>
  );
}
