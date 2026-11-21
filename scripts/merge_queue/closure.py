"""Closure audit: a landing closes exactly the issues its ticket fixes.

Before the merge, compare the PR's ``closingIssuesReferences`` (what GitHub will close)
with the ticket's ``fixes`` (what the author declared). After the merge, close every
fix GitHub left open and reopen every issue it closed that the ticket does not fix
(a quoted "Fixes #n" in a body, for example). Stacked PRs based on the landed branch
are retargeted to main.
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.merge_queue.api import REPO, Api, get_all

CLOSING_QUERY = (
    "query($owner: String!, $name: String!, $pr: Int!) {"
    " repository(owner: $owner, name: $name) { pullRequest(number: $pr) {"
    " closingIssuesReferences(first: 50) { nodes { number } } } } }"
)


@dataclass(frozen=True)
class Audit:
    pr: int
    fixes: tuple[int, ...]
    closing: tuple[int, ...]

    @property
    def missing(self) -> list[int]:
        """Declared fixes GitHub will not close."""
        return sorted(set(self.fixes) - set(self.closing))

    @property
    def extra(self) -> list[int]:
        """Issues GitHub will close that the ticket does not fix."""
        return sorted(set(self.closing) - set(self.fixes))

    @property
    def clean(self) -> bool:
        return not self.missing and not self.extra


def closing_references(api: Api, pr: int) -> tuple[int, ...]:
    owner, name = REPO.split("/")
    response = api.post(
        "graphql",
        {"query": CLOSING_QUERY, "variables": {"owner": owner, "name": name, "pr": pr}},
    )
    body = response.body if isinstance(response.body, dict) else {}
    if response.status != 200 or body.get("errors") or not body.get("data"):
        raise RuntimeError(f"closingIssuesReferences for #{pr} failed: {response.body}")
    nodes = body["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
    return tuple(sorted(int(node["number"]) for node in nodes))


def audit(api: Api, pr: int, fixes: tuple[int, ...]) -> Audit:
    return Audit(pr, tuple(sorted(fixes)), closing_references(api, pr))


def settle(api: Api, result: Audit) -> list[tuple[int, str]]:
    """After the merge: close open fixes, reopen issues closed without a ticket fix."""
    actions: list[tuple[int, str]] = []
    for issue in result.fixes:
        path = f"repos/{REPO}/issues/{issue}"
        if api.get(path).get("state") != "closed":
            api.patch(path, {"state": "closed", "state_reason": "completed"})
            actions.append((issue, "closed"))
    for issue in result.extra:
        path = f"repos/{REPO}/issues/{issue}"
        if api.get(path).get("state") == "closed":
            api.patch(path, {"state": "open"})
            actions.append((issue, "reopened"))
    return actions


def retarget_stacked(api: Api, head_branch: str) -> list[int]:
    """Point open PRs that were stacked on a landed branch at main."""
    children = get_all(api, f"repos/{REPO}/pulls", {"state": "open", "base": head_branch})
    moved = []
    for child in children:
        number = int(child["number"])
        api.patch(f"repos/{REPO}/pulls/{number}", {"base": "main"})
        moved.append(number)
    return moved
