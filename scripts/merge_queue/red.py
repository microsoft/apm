"""Act on a red train: rerun, eject, bisect or halt (merge-queue v2 step 5)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from scripts.merge_queue import classify, proof, refs, state, tickets
from scripts.merge_queue.api import REPO, Api, get_all

ReportReader = Callable[[int], dict]
WAIT = "wait"
OWN, NONE = "own", "none"
_REMOVED = (200, 204, 404)


def red_guard(api: Api, item: state.Train) -> tuple[str, int | None]:
    """Guard attribution over every prefix run (the -full head runs the guard too).

    ("own", pr): the earliest guard violation, every earlier prefix guard green;
    ("wait", None): an earlier prefix guard has no completed verdict yet;
    ("infra", None): a prefix guard failed on infrastructure, not a guard check;
    ("none", None): no guard evidence (all green, or a prefix run is missing).
    """
    for member, ref in zip(item.members, item.refs, strict=True):
        result = proof.guard_result(api, ref, member.sha)
        if result == proof.GREEN:
            continue
        if result == proof.VIOLATION:
            return OWN, member.pr
        if result == proof.PENDING:
            return WAIT, None
        if result == proof.INFRA:
            return proof.INFRA, None
        return NONE, None
    return NONE, None


def _members(api: Api, item: state.Train) -> list[tuple[int, tuple[str, ...]]]:
    return [
        (m.pr, tuple(f["filename"] for f in get_all(api, f"repos/{REPO}/pulls/{m.pr}/files")))
        for m in item.members
    ]


def unlabel(writer: Api, current: state.State, pr: int) -> bool:
    """Remove queue:ready; a failed removal is kept in ``State.unlabel`` for retry."""
    path = f"repos/{REPO}/issues/{pr}/labels/{quote(tickets.READY_LABEL)}"
    done = writer.delete(path).status in _REMOVED
    if done and pr in current.unlabel:
        current.unlabel.remove(pr)
    elif not done and pr not in current.unlabel:
        current.unlabel.append(pr)
    return done


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
    removed = unlabel(writer, current, culprit.pr) if writer is not None else False
    log.emit(
        "ejected",
        train=item.train_id,
        pr=culprit.pr,
        klass=decision.klass,
        run=item.run_id,
        evidence=list(decision.evidence[:20]),
        unlabeled=removed,
    )


def abort_dependents(api: Api, current: state.State, item: state.Train, log: Any) -> None:
    """Later speculative trains were built on this red train's head: abort them and
    release their members for a rebuild on a valid base."""
    for later in current.chain():
        if later.train_id <= item.train_id:
            continue
        later.move(state.ABORTED)
        api.delete(f"repos/{REPO}/git/refs/heads/{refs.build_ref(later.train_id)}")
        log.emit("aborted", train=later.train_id, behind=item.train_id)


def _request_rerun(api: Api, item: state.Train, log: Any) -> str:
    run = api.get(f"repos/{REPO}/actions/runs/{item.run_id}")
    response = api.post(f"repos/{REPO}/actions/runs/{item.run_id}/rerun-failed-jobs", {})
    if response.status not in (201, 204):
        log.emit("rerun-refused", train=item.train_id, run=item.run_id, status=response.status)
        return WAIT
    item.reruns += 1
    item.awaiting_attempt = int(run.get("run_attempt", 1)) + 1
    return classify.RERUN


def handle(
    api: Api,
    writer: Api | None,
    current: state.State,
    item: state.Train,
    log: Any,
    read_report: ReportReader = classify.read_report,
) -> str:
    """Classify a red verdict and act. RERUN and WAIT keep the train testing."""
    guard, culprit = red_guard(api, item)
    if guard == WAIT:
        return WAIT
    report = read_report(int(item.run_id or 0))
    if guard == proof.INFRA:
        report = {
            **report,
            "unidentified_jobs": [*report.get("unidentified_jobs", []), "guard (infra)"],
        }
    decision = classify.classify(report, _members(api, item), culprit, item.reruns)
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
        return _request_rerun(api, item, log)
    item.move(state.RED)
    item.red_class, item.verdict_at = decision.klass, log.clock()
    abort_dependents(api, current, item, log)
    if decision.action == classify.EJECT:
        eject(writer, current, item, decision, log)
    elif decision.action == classify.BISECT:
        current.split = list(decision.first_half)
        log.emit("bisect", train=item.train_id, first_half=current.split)
    else:
        current.halted = f"t{item.train_id:04d} red twice on infra: {', '.join(decision.evidence)}"
        log.emit("needs-input", reason=current.halted)
    return decision.action
