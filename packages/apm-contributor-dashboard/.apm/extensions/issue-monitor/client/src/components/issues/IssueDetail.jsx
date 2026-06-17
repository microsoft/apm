import { createResource, Show } from "solid-js";
import Modal from "../Modal";
import { getIssueDetail, startSession, openSession } from "../../services/api";
import { renderMarkdown } from "../../utils/markdown";
import { showToast } from "../Toast";
import CommentComposer from "../CommentComposer";
import { canTriage } from "../../stores/permissions";

export default function IssueDetail(props) {
  const issue = () => props.issue;
  const [detail, { refetch }] = createResource(() => issue()?.number, getIssueDetail);

  async function handleStart() {
    try {
      await startSession(issue().number, issue().title);
      showToast(`Session started for #${issue().number}`);
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  async function handleOpen() {
    try {
      await openSession(issue().number, issue().title);
      showToast(`Opening session for #${issue().number}`);
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  const footer = () => (
    <div class="modal-actions">
      <a class="btn btn-secondary" href={issue()?.url} target="_blank">View on GitHub</a>
      <Show when={issue()?.hasSession}>
        <button class="btn btn-primary" onClick={handleOpen}>Open Session</button>
      </Show>
      <Show when={!issue()?.hasSession && issue()?.status === "available"}>
        <button class="btn btn-primary" onClick={handleStart}>Start Session</button>
      </Show>
    </div>
  );

  return (
    <Modal
      open={() => issue() !== null}
      title={() => detail()?.title || `#${issue()?.number}`}
      onClose={props.onClose}
      onRefresh={refetch}
      footer={footer()}
    >
      <Show when={detail()} fallback={<div class="modal-loading">Loading issue details...</div>}>
        {(d) => (
          <>
            <div class="meta-row">
              <div class="meta-item"><strong>Author:</strong> {d().author}</div>
              <div class="meta-item"><strong>State:</strong> {d().state}</div>
              <div class="meta-item"><strong>Comments:</strong> {d().comments}</div>
              <div class="meta-item"><strong>Created:</strong> {new Date(d().createdAt).toLocaleDateString()}</div>
            </div>
            <Show when={d().labels?.length > 0}>
              <div class="modal-body issue-labels">
                {d().labels.map(l => <span class="label-tag">{l}</span>)}
              </div>
            </Show>
            <div class="issue-body" innerHTML={renderMarkdown(d().body)} />
            <CommentComposer
              type="issue"
              number={issue().number}
              title={issue().title}
              onSubmitted={refetch}
              disabled={!canTriage()}
              disabledReason="You need triage or write access to comment"
            />
          </>
        )}
      </Show>
    </Modal>
  );
}
