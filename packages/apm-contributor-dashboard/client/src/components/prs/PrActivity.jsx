import { For, Show } from "solid-js";
import { renderMarkdown } from "../../utils/markdown";

const kindClasses = {
  comment: "activity-kind-comment",
  review: "activity-kind-review",
  APPROVED: "activity-kind-approved",
  CHANGES_REQUESTED: "activity-kind-changes",
};

export default function PrActivity(props) {
  const items = () => props.activity || [];

  function kindClass(item) {
    if (item.kind === "review" && item.state === "APPROVED") return "activity-kind-approved";
    if (item.kind === "review" && item.state === "CHANGES_REQUESTED") return "activity-kind-changes";
    return kindClasses[item.kind] || "activity-kind-comment";
  }

  function kindLabel(item) {
    if (item.kind === "review") return item.state || "Review";
    return "Comment";
  }

  return (
    <div class="activity-section">
      <h3>Activity ({items().length})</h3>
      <Show when={items().length === 0}>
        <div class="empty">No activity yet</div>
      </Show>
      <For each={items()}>
        {(item) => (
          <div class="activity-item">
            <div class="activity-item-header">
              <span class="activity-author">{item.author}</span>
              <span class={`activity-kind ${kindClass(item)}`}>{kindLabel(item)}</span>
              <span class="activity-date">{item.createdAt ? new Date(item.createdAt).toLocaleString() : ""}</span>
            </div>
            <Show when={item.body}>
              <div class="activity-body" innerHTML={renderMarkdown(item.body)} />
            </Show>
          </div>
        )}
      </For>
    </div>
  );
}
