import { Show, For } from "solid-js";

const verdictLabels = { ship: "Ship It", ship_with_followups: "Ship with Follow-ups", do_not_ship: "Do Not Ship" };
const verdictIcons = { ship: "[+]", ship_with_followups: "[!]", do_not_ship: "[x]" };

export default function PrPanelReview(props) {
  const review = () => props.panelReview;

  return (
    <div class="panel-review-container">
      <Show when={review()} fallback={
        <div class="panel-no-review">
          <div class="panel-no-review-icon">[?]</div>
          <div class="panel-no-review-text">No panel review found</div>
          <div class="panel-no-review-sub">Add the "panel-review" label to trigger a review</div>
        </div>
      }>
        {(r) => (
          <>
            <div class={`panel-verdict-banner ${r().verdict}`}>
              <div class="panel-verdict-icon">{verdictIcons[r().verdict] || "[?]"}</div>
              <div class="panel-verdict-text">
                <div class="panel-verdict-label">{verdictLabels[r().verdict] || r().verdict}</div>
                <Show when={r().summary}>
                  <div class="panel-verdict-summary">{r().summary}</div>
                </Show>
              </div>
              <Show when={r().author}>
                <div class="panel-verdict-meta">by {r().author}</div>
              </Show>
            </div>
            <div class="panel-stats-row">
              <div class="panel-stat-card blocking"><div class="stat-num">{r().blocking}</div><div class="stat-label">Blocking</div></div>
              <div class="panel-stat-card recommended"><div class="stat-num">{r().recommended}</div><div class="stat-label">Recommended</div></div>
              <div class="panel-stat-card nit"><div class="stat-num">{r().nit}</div><div class="stat-label">Nit</div></div>
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
                        <td class="panel-persona-takeaway">{p.takeaway || "--"}</td>
                        <td class={`panel-brn-cell ${p.blocking > 0 ? "has-findings" : "clean"}`}>{p.blocking}</td>
                        <td class="panel-brn-cell">{p.recommended}</td>
                        <td class="panel-brn-cell">{p.nit}</td>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
            </Show>
            <Show when={r().sections?.length > 0}>
              <For each={r().sections}>
                {(section) => (
                  <div class="panel-section">
                    <div class="panel-section-title">{section.title}</div>
                    <div class="panel-section-body" innerHTML={section.body} />
                  </div>
                )}
              </For>
            </Show>
          </>
        )}
      </Show>
    </div>
  );
}
