import { Show, For } from "solid-js";
import { renderMarkdown } from "../../utils/markdown";
import { createFollowUpIssues } from "../../services/api";
import { showToast } from "../Toast";

const verdictLabels = { ship: "Ship It", ship_with_followups: "Ship with Follow-ups", do_not_ship: "Do Not Ship" };
const verdictIcons = { ship: "[+]", ship_with_followups: "[!]", do_not_ship: "[x]" };

function buildSections(r) {
  const out = [];
  if (r.recommendation) out.push({ title: "Recommendation", body: r.recommendation });
  if (r.deferred) out.push({ title: "Deferred", body: r.deferred });
  if (r.folded) out.push({ title: "Folded in this run", body: r.folded });
  if (r.ci) out.push({ title: "CI", body: r.ci });
  if (r.convergence) out.push({ title: "Convergence", body: r.convergence });
  return out;
}

export default function PrPanelReview(props) {
  const review = () => props.panelReview;

  async function handleCreateFollowUps() {
    const r = review();
    if (!r || !props.prNumber) return;
    showToast("Creating follow-up issues...");
    try {
      const result = await createFollowUpIssues(props.prNumber, r);
      if (result.created && result.created.length > 0) {
        showToast(`Created ${result.created.length} follow-up issue(s)`);
      } else {
        showToast(result.message || "No follow-up items found");
      }
    } catch (e) {
      showToast("Failed to create issues: " + (e.message || e));
    }
  }

  return (
    <div class="panel-review-container">
      <Show when={review()} fallback={
        <div class="panel-no-review">
          <div class="panel-no-review-icon">[?]</div>
          <div class="panel-no-review-text">No panel review found</div>
          <div class="panel-no-review-sub">Add the "panel-review" label to trigger a review</div>
        </div>
      }>
        {(r) => {
          const sections = () => buildSections(r());
          return (
            <>
              <div class={`panel-verdict-banner ${r().verdict}`}>
                <div class="panel-verdict-icon">{verdictIcons[r().verdict] || "[?]"}</div>
                <div class="panel-verdict-text">
                  <div class="panel-verdict-label">{verdictLabels[r().verdict] || r().verdict}</div>
                  <Show when={r().summary}>
                    <div class="panel-verdict-summary" innerHTML={renderMarkdown(r().summary)} />
                  </Show>
                </div>
                <Show when={r().author}>
                  <div class="panel-verdict-meta">by {r().author}</div>
                </Show>
              </div>
              <div class="panel-stats-row">
                <div class="panel-stat-card blocking"><div class="stat-num">{r().totals?.b ?? 0}</div><div class="stat-label">Blocking</div></div>
                <div class="panel-stat-card recommended"><div class="stat-num">{r().totals?.r ?? 0}</div><div class="stat-label">Recommended</div></div>
                <div class="panel-stat-card nit"><div class="stat-num">{r().totals?.n ?? 0}</div><div class="stat-label">Nit</div></div>
                <div class="panel-stat-card personas"><div class="stat-num">{r().personas?.length || 0}</div><div class="stat-label">Personas</div></div>
              </div>
              <Show when={r().personas?.length > 0}>
                <table class="panel-persona-table">
                  <thead><tr><th>Persona</th><th>Takeaway</th><th>B</th><th>R</th><th>N</th></tr></thead>
                  <tbody>
                    <For each={r().personas}>
                      {(p) => (
                        <tr>
                          <td class="panel-persona-name">{p.name}</td>
                          <td class="panel-persona-takeaway" innerHTML={renderMarkdown(p.takeaway || "--")} />
                          <td class={`panel-brn-cell ${p.b > 0 ? "has-findings" : "clean"}`}>{p.b}</td>
                          <td class="panel-brn-cell">{p.r}</td>
                          <td class="panel-brn-cell">{p.n}</td>
                        </tr>
                      )}
                    </For>
                  </tbody>
                </table>
              </Show>
              <Show when={sections().length > 0}>
                <For each={sections()}>
                  {(section) => (
                    <div class="panel-section">
                      <div class="panel-section-title">{section.title}</div>
                      <div class="panel-section-body" innerHTML={renderMarkdown(section.body)} />
                    </div>
                  )}
                </For>
              </Show>
              <div class="panel-actions-row">
                <button class="btn-follow-up" onClick={handleCreateFollowUps}>
                  Create follow-up issues
                </button>
              </div>
            </>
          );
        }}
      </Show>
    </div>
  );
}
