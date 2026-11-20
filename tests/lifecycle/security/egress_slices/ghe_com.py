"""Egress rows for HOST ghe.com: a GitHub Enterprise Cloud tenant (*.ghe.com)."""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice

_GHE = "lifecycle.ghe.com"

SLICE = HostSlice(
    host="ghe.com",
    git_host=_GHE,
    family=(_GHE, f"api.{_GHE}"),
    base=f"https://{_GHE}/lifecycle",
    spec=f"https://{_GHE}/lifecycle/p-skill#v1.0.0",
    pat_env="GITHUB_APM_PAT",
    rows=(
        EgressRow("EP|CRED=PAT|HOST=ghe.com", "PAT", "clone"),
        EgressRow("EP|CRED=gh|HOST=ghe.com", "gh", "clone"),
        EgressRow("EP|CRED=none|HOST=ghe.com", "none", "clone"),
    ),
)
