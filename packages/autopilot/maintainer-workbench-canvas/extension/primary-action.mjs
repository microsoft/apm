// Live evidence owns the next action. Advisory prose cannot supply action fields.
export function primaryAction(item, linked = [], workflowApprovals = [], context = null) {
  const action = (type, label, status, consequence, extra = {}) => ({ type, label, status, consequence, targetId: item.id, ...extra });
  const navigate = (pr, label, status, path = "/checks") => action("navigate", label, status,
    "Opens GitHub only. No approval, review or merge is submitted.", { url: `${pr.url}${path}`, targetId: pr.id,
      warning: pr.autoMerge === "ON" ? "Auto-merge is on: approving checks or review in GitHub may allow this PR to merge." : null });
  const prepare = (kind, label, status) => action("prepare", label, status, "Opens a draft for you to review. Nothing is accepted, published or started.", { kind });
  if (item.state !== "OPEN") return action("navigate", "Open recorded outcome", "This item is closed. Related work may still have remaining scope.",
    "Opens the source on GitHub.", { url: item.url });
  const prs = (item.kind === "pr" ? [item] : linked).filter(p => p.state === "OPEN");
  if (["reviews", "review-followup"].includes(context) && item.kind === "pr") {
    return navigate(item, item.isDraft ? `Inspect draft PR #${item.number}` : item.reviewDecision === "CHANGES_REQUESTED" ? `Inspect review follow-up for PR #${item.number}` :
      item.reviewDecision === "APPROVED" ? `Inspect recorded review for PR #${item.number}` : `Open PR #${item.number} for review`,
    `${item.attention?.reviewReason || "Inspect the contribution and its review history."}${!item.metadataFresh ? " Metadata is last known; GitHub opens the current source." : ""} Review is independent of workflow permission and check results.`, "/files");
  }
  if (context === "decisions" && item.kind === "issue" && item.lifecycle?.scopeDecision) {
    if (!item.metadataFresh || !item.lifecycle.current) return action("refresh", "Update this issue from GitHub",
      "The recorded scope recommendation needs a fresh read before a decision.", "Rechecks this issue's discussion and canonical human records; does not approve or start work.");
    return prepare("scope-draft", "Prepare scope decision", "A scope recommendation needs your judgment. Workflow permission and PR review are separate.");
  }
  const stale = !item.metadataFresh || prs.some(p => !p.metadataFresh || !p.evidence?.fresh || p.evidence.status === "outdated-head");
  if (stale) return action("refresh", `Update this ${item.kind === "pr" ? "PR" : "issue"} from GitHub`,
    "Current information needs refreshing before choosing the next step.", "Reads this item and its linked evidence. Makes no GitHub changes.");
  const failed = prs.find(p => p.evidence.status === "failed");
  if (failed && context !== "permissions") return navigate(failed, `Open failed checks for PR #${failed.number}`,
    `PR #${failed.number} has reported check failures.${prs.some(p => p.evidence.actionRequired) ? " Some workflows also need a GitHub permission decision." : ""} The cause is not diagnosed here.`);
  const blocked = prs.find(p => p.evidence.actionRequired);
  const approval = workflowApprovals.find(r => prs.some(pr => pr.id === r.targetId && pr.head === r.head) &&
    (["approving", "uncertain"].includes(r.status) || prs.find(pr => pr.id === r.targetId)?.evidence.runs.some(run =>
      run.conclusion === "action_required" && r.runs.some(saved => saved.id === run.id && saved.attempt === run.attempt) &&
      r.receipts.some(receipt => receipt.runId === run.id && receipt.outcome === "approved"))));
  if (approval) return action("observe-workflows", "View workflow approval progress",
    "A workflow permission request is recorded. Current execution and results are checked separately.",
    "Shows persisted permission receipts and refreshes the selected checks. Does not send another approval.", { targetId: approval.targetId });
  if (blocked) return action("approve-workflows", `Review workflow approval for PR #${blocked.number}`,
    `GitHub reports a permission blocker on PR #${blocked.number}.${failed ? " Failed checks are also recorded; their cause is not diagnosed here." : " Other checks may already have results; review can still proceed."}`,
    "Reads eligibility and opens the exact workflow list for confirmation. Nothing is approved by this button.", {
      targetId: blocked.id, url: blocked.url,
      warning: blocked.autoMerge === "ON" ? "Auto-merge is on: successful workflows may allow this PR to merge." : null,
    });
  const lc = item.lifecycle || {};
  const ongoing = lc.runs?.find(r => !["completed", "cancelled"].includes(r.state));
  if (ongoing) {
    if (["waiting-human", "blocked"].includes(ongoing.state)) return { ...prepare("resume", "Prepare continuation of existing work", ongoing.blocker || "Existing work is waiting for your input."), runId: ongoing.id };
    return action("observe", "View existing work", ongoing.state === "plan-required" ? "Existing work needs plan approval in its original conversation." : "Existing work is in progress; avoid starting it again.",
      "Shows the existing work record. Any plan approval remains in its original conversation.");
  }
  if (lc.queuedRequests?.length) return action("observe", "View pending request", "Wait for the confirmed request to have capacity.", "Shows the recorded request; does not send it again.");
  const running = prs.find(p => p.evidence.pending);
  if (running) return navigate(running, `Open running checks for PR #${running.number}`, `Checks on PR #${running.number} are still running.`);
  if (prs.length) {
    const pr = prs[0];
    return navigate(pr, pr.isDraft ? `Open draft PR #${pr.number}` : `Open PR #${pr.number} for your review`,
      pr.isDraft ? "The contributor's PR is still a draft." : pr.reviewDecision === "CHANGES_REQUESTED" ? "Review feedback needs contributor follow-up." :
        "A contribution is available to inspect. Check outcomes alone do not establish acceptance or merge readiness.", "/files");
  }
  if (!lc.current) return action("refresh", "Update this issue from GitHub", "The recorded decision needs a fresh read, not a second approval.",
    "Rechecks the discussion and existing decision. Makes no GitHub changes.");
  if (lc.deferred || lc.design) return action("navigate", "Open the recorded discussion", lc.deferred ? "This issue is deferred." : "A design question is recorded.",
    "Opens the existing discussion without changing its disposition.", { url: item.url });
  if (lc.accepted) return prepare(lc.planned ? "implement" : "horizon", lc.planned ? "Prepare bounded implementation" : "Prepare scheduling decision",
    lc.planned ? "Scope is agreed and planned. Starting work needs a separate confirmation." : "Scope is agreed; scheduling remains to be decided.");
  if (lc.record?.recommendation?.status === "unresolved") return prepare("scope-draft", "Prepare scope decision", "An assessment is available. Decide the proposed scope, not implementation permission.");
  return prepare("triage", "Prepare agent assessment", "This issue needs an assessment before a scope decision.");
}
