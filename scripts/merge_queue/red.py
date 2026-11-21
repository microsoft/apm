"""Act on a red train: rerun, eject, bisect or halt (merge-queue v2 step 5)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from scripts.merge_queue import classify, proof, state, tickets
from scripts.merge_queue.api import REPO, Api, get_all

ReportReader = Callable[[int], dict]


def red_guard(api: Api, item: state.Train) -> int | None:
    """The PR whose guard prefix is the earliest red one (prefixes 1..k-1 were green)."""
    for member, ref in zip(item.members, item.refs, strict=True):
        if ref.endswith("-guard") and proof.prefix_verdict(api, ref, member.sha)[0] == proof.RED:
            return member.pr
    return None


def _members(api: Api, item: state.Train) -> list[tuple[int, tuple[str, ...]]]:
    return [
        (m.pr, tuple(f["filename"] for f in get_all(api, f"repos/{REPO}/pulls/{m.pr}/files")))
        for m in item.members
    ]


def eject(
    writer: Api | None,
    current: state.State,
    item: state.Train,
    decision: classify.Decision,
    log: Any,
) -> None:
    """Void the culprit's ticket: it re-enters only with a new head. With landing on,
    ``writer`` also removes its queue:ready label; the shadow only logs it."""
    culprit = next(m for m in item.members if m.pr == decision.culprit)
    item.ejected = culprit.pr
    current.ejected[culprit.pr] = culprit.head
    if writer is not None:
        writer.delete(f"repos/{REPO}/issues/{culprit.pr}/labels/{quote(tickets.READY_LABEL)}")
    log.emit(
        "ejected",
        train=item.train_id,
        pr=culprit.pr,
        klass=decision.klass,
        run=item.run_id,
        evidence=list(decision.evidence[:20]),
        unlabeled=writer is not None,
    )


def handle(
    api: Api,
    writer: Api | None,
    current: state.State,
    item: state.Train,
    log: Any,
    read_report: ReportReader = classify.read_report,
) -> str:
    """Classify a red verdict and act. Returns the action; RERUN keeps the train testing."""
    report = read_report(int(item.run_id or 0))
    decision = classify.classify(report, _members(api, item), red_guard(api, item), item.reruns)
    log.emit(
        "red",
        train=item.train_id,
        klass=decision.klass,
        action=decision.action,
        culprit=decision.culprit,
        run=item.run_id,
        evidence=list(decision.evidence[:20]),
    )
    item.residual = list(decision.evidence)
    if decision.action == classify.RERUN:
        api.post(f"repos/{REPO}/actions/runs/{item.run_id}/rerun-failed-jobs", {})
        item.reruns += 1
        return classify.RERUN
    item.move(state.RED)
    item.red_class, item.verdict_at = decision.klass, log.clock()
    if decision.action == classify.EJECT:
        eject(writer, current, item, decision, log)
    elif decision.action == classify.BISECT:
        current.split = list(decision.first_half)
        log.emit("bisect", train=item.train_id, first_half=current.split)
    else:
        current.halted = f"t{item.train_id:04d} red twice on infra: {', '.join(decision.evidence)}"
        log.emit("needs-input", reason=current.halted)
    return decision.action
