"""Egress rows for HOST generic as a git host: git.lifecycle.test (Gitea-style).

The world's generic git host serves git plus /api/v1 (and an /api/v3 fallback)
for contents and commits. No platform PAT belongs to it, so a PAT row passes
when none reaches it. It answers anonymously, so the credential helper is
never asked: helper rows prove only where its answer may go.
"""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice

_HOST = "git.lifecycle.test"

SLICE = HostSlice(
    host="generic",
    git_host=_HOST,
    family=(_HOST,),
    base=f"https://{_HOST}/lifecycle",
    spec=f"https://{_HOST}/lifecycle/p-skill#v1.0.0",
    rows=(
        *(
            EgressRow(f"E2|HOST=generic|CRED={cred}|EGRESS={egress}", cred, egress, presented=False)
            for cred in ("PAT", "helper", "none")
            for egress in ("clone", "marketplace-fetch")
        ),
        EgressRow("EP|CRED=helper|HOST=generic", "helper", "clone", presented=False),
        EgressRow("EP|EGRESS=clone|HOST=generic", "PAT", "clone", presented=False),
        EgressRow(
            "EP|EGRESS=marketplace-fetch|HOST=generic", "PAT", "marketplace-fetch", presented=False
        ),
    ),
)
