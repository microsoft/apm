import { createSignal, createResource, Show, For } from "solid-js";
import Modal from "../Modal";
import { getPrDetail, approvePr, mergeWhenReady, rerunCi, runPanel } from "../../services/api";
import { renderMarkdown } from "../../utils/markdown";
import { showToast } from "../Toast";
import PrPipeline from "./PrPipeline";
import PrPanelReview from "./PrPanelReview";
import PrActivity from "./PrActivity";
import CommentComposer from "../CommentComposer";
import { canWrite, canTriage } from "../../stores/permissions";

export default function PrDetail(props) {
  const pr = () => props.pr;
  const [detail, { refetch }] = createResource(() => pr()?.number, getPrDetail);
  const [subTab, setSubTab] = createSignal("description");

  const tabs = [
    { id: "description", label: "Description" },
    { id: "pipeline", label: "Pipeline" },
    { id: "panel", label: "Panel Review" },
    { id: "activity", label: "Activity" },
  ];

  async function action(fn, label) {
    try {
      await fn(pr().number);
      showToast(`${label} for #${pr().number}`);
      refetch();
    } catch (e) {
      showToast(`Error: ${e.message}`);
    }
  }

  const footer = () => (
    <div class="modal-actions">
      <a class="btn btn-secondary" href={pr()?.url} target="_blank">View on GitHub</a>
      <button
        class="btn btn-secondary"
        onClick={() => action(rerunCi, "Re-running CI")}
        disabled={!canWrite()}
        title={!canWrite() ? "Requires write access" : ""}
      >Re-run CI</button>
      <button class="btn btn-secondary" onClick={() => action(runPanel, "Running panel")}>Run Panel</button>
      <button
        class="btn btn-secondary"
        onClick={() => action(approvePr, "Approved")}
        disabled={!canWrite()}
        title={!canWrite() ? "Requires write access" : ""}
      >Approve</button>
      <button
        class="btn btn-primary"
        onClick={() => action(mergeWhenReady, "Merge queued")}
        disabled={!canWrite()}
        title={!canWrite() ? "Requires write access" : ""}
      >Merge</button>
    </div>
  );

  return (
    <Modal
      open={() => pr() !== null}
      title={() => detail()?.title ? `#${detail().number} ${detail().title}` : `#${pr()?.number}`}
      onClose={props.onClose}
      onRefresh={refetch}
      footer={footer()}
    >
      <Show when={detail()} fallback={<div class="modal-loading">Loading PR details...</div>}>
        {(d) => (
          <>
            <div class="meta-row">
              <div class="meta-item"><strong>Author:</strong> {d().author}</div>
              <div class="meta-item"><strong>Branch:</strong> <code>{d().branch}</code></div>
              <div class="meta-item"><strong>Review:</strong> {d().reviewDecision || "PENDING"}</div>
              <div class="meta-item"><strong>Draft:</strong> {d().isDraft ? "Yes" : "No"}</div>
            </div>
            <Show when={d().labels?.length > 0}>
              <div class="issue-labels" style={{ "margin-bottom": "12px" }}>
                {d().labels.map(l => <span class="label-tag">{l}</span>)}
              </div>
            </Show>
            <div class="activity-body-tabs">
              <For each={tabs}>
                {(tab) => (
                  <button
                    class={`activity-body-tab ${subTab() === tab.id ? "active" : ""} ${tab.id === "panel" && d().panelReview ? "panel-tab-has-data" : ""} ${tab.id === "pipeline" && d().checks?.some(c => (c.conclusion || "").toLowerCase() === "failure") ? "pipeline-tab-failing" : ""}`}
                    onClick={() => setSubTab(tab.id)}
                  >
                    {tab.label}
                  </button>
                )}
              </For>
            </div>
            <Show when={subTab() === "description"}>
              <div class="issue-body" innerHTML={renderMarkdown(d().body)} />
            </Show>
            <Show when={subTab() === "pipeline"}>
              <PrPipeline checks={d().checks || []} workflowRuns={d().workflowRuns || []} />
            </Show>
            <Show when={subTab() === "panel"}>
              <PrPanelReview panelReview={d().panelReview} />
            </Show>
            <Show when={subTab() === "activity"}>
              <PrActivity activity={d().activity || []} />
            </Show>
            <CommentComposer
              type="pr"
              number={pr().number}
              title={pr().title || d().title}
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
