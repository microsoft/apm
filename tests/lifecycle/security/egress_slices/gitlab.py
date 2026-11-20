"""Egress rows for HOST GitLab: gitlab.com.

The world serves GitLab git, the /api/v4 repository files (JSON and raw) and
commits, so marketplace-fetch and policy-fetch read their files from the
host's own origin.
"""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice

SLICE = HostSlice(
    host="GitLab",
    git_host="gitlab.com",
    family=("gitlab.com",),
    base="https://gitlab.com/lifecycle/group",
    spec="https://gitlab.com/lifecycle/group/p-skill#v1.0.0",
    pat_env="GITLAB_APM_PAT",
    rows=(
        EgressRow("EP|CRED=PAT|HOST=GitLab", "PAT", "clone", smoke=True),
        EgressRow("EP|CRED=helper|HOST=GitLab", "helper", "clone"),
        EgressRow("EP|CRED=none|HOST=GitLab", "none", "clone"),
        *(
            EgressRow(f"E2|HOST=GitLab|CRED={cred}|EGRESS={egress}", cred, egress)
            for cred in ("PAT", "helper", "none")
            for egress in ("api-probe", "clone", "marketplace-fetch", "policy-fetch")
        ),
        EgressRow("EP|EGRESS=marketplace-fetch|HOST=GitLab", "PAT", "marketplace-fetch"),
        EgressRow("EP|EGRESS=policy-fetch|HOST=GitLab", "PAT", "policy-fetch"),
        EgressRow("EP|EGRESS=api-probe|HOST=GitLab", "PAT", "api-probe"),
        EgressRow("EP|EGRESS=clone|HOST=GitLab", "PAT", "clone"),
        EgressRow("EP|HOST=GitLab|PROXY_ONLY=0", "PAT", "clone"),
        EgressRow("EP|HOST=GitLab|REDIRECT=none", "PAT", "clone"),
        EgressRow("EP|HOST=GitLab|PROXY_ONLY=1", "PAT", "clone"),
        *(
            EgressRow(f"EP|HOST=GitLab|REDIRECT={kind}", "PAT", "clone")
            for kind in ("same-origin", "cross-origin")
        ),
    ),
)
