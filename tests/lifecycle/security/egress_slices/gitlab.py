"""Egress rows for HOST GitLab: gitlab.com.

Unregistered, missing world support: E2|HOST=GitLab|*|EGRESS=marketplace-fetch
(no GitLab repository-file API route is served, so marketplace add finds no
marketplace.json) and E2|HOST=GitLab|*|EGRESS=policy-fetch (policy status with a
gitlab.com origin sends no request the world answers).
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
            for egress in ("api-probe", "clone")
        ),
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
