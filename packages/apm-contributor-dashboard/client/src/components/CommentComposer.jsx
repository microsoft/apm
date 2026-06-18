import { createSignal, createEffect, onCleanup, Show } from "solid-js";
import { submitComment, refineComment, getDraft } from "../services/api";
import { showToast } from "./Toast";

export default function CommentComposer(props) {
  const [text, setText] = createSignal("");
  const [submitting, setSubmitting] = createSignal(false);
  const [waiting, setWaiting] = createSignal(false); // true while Copilot is thinking

  // Poll for draft updates from agent (every 2s while composer is open)
  const poll = setInterval(async () => {
    try {
      const data = await getDraft(props.type, props.number);
      if (data.text && data.text !== text()) {
        setText(data.text);
        setWaiting(false);
        showToast("Draft updated by Copilot");
      }
    } catch (_) {}
  }, 2000);
  onCleanup(() => clearInterval(poll));

  async function handleRefine() {
    if (!text().trim()) return;
    setWaiting(true);
    try {
      await refineComment(props.type, props.number, text(), props.title || "");
    } catch (e) {
      setWaiting(false);
      showToast(`Error: ${e.message}`);
    }
  }

  async function handleSubmit() {
    if (!text().trim()) return;
    setSubmitting(true);
    try {
      await submitComment(props.type, props.number, text());
      showToast(`Comment posted on #${props.number}`);
      setText("");
      props.onSubmitted?.();
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
    setSubmitting(false);
  }

  return (
    <div class="comment-composer">
      <Show when={props.disabled}>
        <div class="permission-notice">{props.disabledReason || "Insufficient permissions"}</div>
      </Show>
      <textarea
        class="comment-textarea"
        placeholder={props.disabled ? "You do not have permission to comment" : "Write a comment... Use 'Refine with Copilot' to collaborate on the draft."}
        value={text()}
        onInput={(e) => setText(e.target.value)}
        rows={5}
        disabled={props.disabled}
      />
      <Show when={waiting()}>
        <div class="copilot-thinking">
          <div class="thinking-bar" />
          <span class="thinking-text">Copilot is thinking...</span>
        </div>
      </Show>
      <div class="comment-actions">
        <button
          class="btn btn-secondary"
          onClick={handleRefine}
          disabled={props.disabled || waiting() || !text().trim()}
        >
          {waiting() ? "Copilot is thinking..." : "Refine with Copilot"}
        </button>
        <button
          class="btn btn-primary"
          onClick={handleSubmit}
          disabled={props.disabled || submitting() || !text().trim()}
        >
          {submitting() ? "Posting..." : "Submit Comment"}
        </button>
      </div>
    </div>
  );
}
