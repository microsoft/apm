import { For, Show } from "solid-js";
import { renderMarkdown } from "../../utils/markdown";

function commentKindClass(comment) {
  if (comment.isTriagePanel) return "comment-kind-triage";
  if (comment.isBot) return "comment-kind-bot";
  return "comment-kind-human";
}

function commentKindLabel(comment) {
  if (comment.isTriagePanel) return "Triage";
  if (comment.isBot) return "Bot";
  return "Comment";
}

export default function IssueComments(props) {
  const comments = () => props.commentList || [];

  return (
    <div class="comment-thread">
      <Show when={comments().length === 0}>
        <div class="comment-empty">No comments yet.</div>
      </Show>
      <For each={comments()}>
        {(comment) => (
          <div class="comment-item">
            <div class="comment-item-header">
              <span class="comment-author">{comment.author}</span>
              <span class={`comment-kind ${commentKindClass(comment)}`}>{commentKindLabel(comment)}</span>
              <span class="comment-date">
                {comment.createdAt ? new Date(comment.createdAt).toLocaleString() : ""}
              </span>
            </div>
            <div class="comment-body" innerHTML={renderMarkdown(comment.body)} />
          </div>
        )}
      </For>
    </div>
  );
}
