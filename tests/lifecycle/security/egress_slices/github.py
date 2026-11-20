"""Egress rows for HOST github: github.com and its API origin api.github.com."""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice

_E2 = ("api-probe", "clone", "marketplace-fetch", "policy-fetch")
# The world serves p-skill anonymously: APM reads github.com without sending a
# credential first, so clone and api-probe rows prove only where it may go.
_ANONYMOUS = {"api-probe", "clone"}

SLICE = HostSlice(
    host="github",
    git_host="github.com",
    family=("github.com", "api.github.com"),
    base="https://github.com/lifecycle",
    spec="lifecycle/p-skill#v1.0.0",
    pat_env="GITHUB_APM_PAT",
    rows=(
        EgressRow("EP|CRED=PAT|HOST=github", "PAT", "policy-fetch", smoke=True),
        EgressRow("EP|CRED=gh|HOST=github", "gh", "policy-fetch"),
        EgressRow("EP|CRED=none|HOST=github", "none", "policy-fetch"),
        *(
            EgressRow(
                f"E2|HOST=github|CRED={cred}|EGRESS={egress}",
                cred,
                egress,
                presented=egress not in _ANONYMOUS,
            )
            for cred in ("PAT", "gh", "none")
            for egress in _E2
        ),
        # github.com serves p-skill anonymously: the clone and api-probe rows
        # run without a credential and prove none is sent.
        EgressRow("EP|EGRESS=api-probe|HOST=github", "none", "api-probe"),
        EgressRow("EP|EGRESS=clone|HOST=github", "none", "clone"),
        EgressRow("EP|EGRESS=marketplace-fetch|HOST=github", "PAT", "marketplace-fetch"),
        EgressRow("EP|EGRESS=policy-fetch|HOST=github", "PAT", "policy-fetch"),
        EgressRow("EP|HOST=github|PROXY_ONLY=0", "PAT", "policy-fetch"),
        EgressRow("EP|HOST=github|REDIRECT=none", "PAT", "policy-fetch"),
    ),
)
