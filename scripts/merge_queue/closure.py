"""Closure audit: a landing closes exactly the issues its ticket fixes.

Before the merge, what GitHub will close (the PR's ``closingIssuesReferences`` plus
closing keywords in its commit messages) must equal the ticket's ``fixes``; a
mismatch ejects the train before anything merges. After the merge, the runner closes
any fix GitHub left open and retargets PRs stacked on the landed branch. Failed
writes stay pending and are retried on the next tick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from scripts.merge_queue.api import REPO, Api, get_all

CLOSING_QUERY = (
    "query($owner: String!, $name: String!, $pr: Int!) {"
    " repository(owner: $owner, name: $name) { pullRequest(number: $pr) {"
    " closingIssuesReferences(first: 50) { nodes { number repository { nameWithOwner } } } } } }"
)
# GitHub's closing keywords, with a bare "#n" or a qualified "owner/repo#n".
_KEYWORD = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+([\w.-]+/[\w.-]+)?#(\d+)\b",
    re.IGNORECASE,
)
_OK = (200, 201, 204)


@dataclass(frozen=True)
class Audit:
    pr: int
    fixes: tuple[int, ...]
    closing: tuple[int, ...]
    # Closures in other repositories ("owner/repo#n"): a ticket never declares them.
    foreign: tuple[str, ...] = ()

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
        return not self.missing and not self.extra and not self.foreign


def closing_references(api: Api, pr: int) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Issues the merge will close, from linked or body references and commit keywords:
    this repository's numbers, and any other repository's issues fully qualified."""
    owner, name = REPO.split("/")
    response = api.post(
        "graphql",
        {"query": CLOSING_QUERY, "variables": {"owner": owner, "name": name, "pr": pr}},
    )
    body = response.body if isinstance(response.body, dict) else {}
    if response.status != 200 or body.get("errors") or not body.get("data"):
        raise RuntimeError(f"closingIssuesReferences for #{pr} failed: {response.body}")
    nodes = body["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
    refs = {(node["repository"]["nameWithOwner"], int(node["number"])) for node in nodes}
    for commit in get_all(api, f"repos/{REPO}/pulls/{pr}/commits"):
        for repo, number in _KEYWORD.findall(commit["commit"]["message"]):
            refs.add((repo or REPO, int(number)))
    local = tuple(sorted(n for repo, n in refs if repo.lower() == REPO.lower()))
    foreign = tuple(sorted(f"{repo}#{n}" for repo, n in refs if repo.lower() != REPO.lower()))
    return local, foreign


def audit(api: Api, pr: int, fixes: tuple[int, ...]) -> Audit:
    closing, foreign = closing_references(api, pr)
    return Audit(pr, tuple(sorted(fixes)), closing, foreign)


def settle(api: Api, fixes: tuple[int, ...], head_branch: str) -> tuple[list[str], bool]:
    """After a clean merge: close fixes GitHub left open and retarget stacked PRs.

    Returns the actions taken and whether every write succeeded.
    """
    actions: list[str] = []
    ok = True
    for issue in fixes:
        path = f"repos/{REPO}/issues/{issue}"
        if api.get(path).get("state") == "closed":
            continue
        done = api.patch(path, {"state": "closed", "state_reason": "completed"}).status in _OK
        actions.append(f"close #{issue}: {'ok' if done else 'failed'}")
        ok = ok and done
    for child in get_all(api, f"repos/{REPO}/pulls", {"state": "open", "base": head_branch}):
        number = int(child["number"])
        done = api.patch(f"repos/{REPO}/pulls/{number}", {"base": "main"}).status in _OK
        actions.append(f"retarget #{number}: {'ok' if done else 'failed'}")
        ok = ok and done
    return actions, ok
