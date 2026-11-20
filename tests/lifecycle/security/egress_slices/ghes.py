"""Egress rows for HOST GHES: a GitHub Enterprise Server named by GITHUB_HOST."""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice

_E2 = ("api-probe", "clone", "marketplace-fetch", "policy-fetch")
_GHES = "ghes.lifecycle.test"

SLICE = HostSlice(
    host="GHES",
    git_host=_GHES,
    family=(_GHES,),
    base=f"https://{_GHES}/lifecycle",
    spec=f"https://{_GHES}/lifecycle/p-skill#v1.0.0",
    pat_env="GITHUB_APM_PAT",
    env={"GITHUB_HOST": _GHES},
    rows=(
        EgressRow("EP|CRED=PAT|HOST=GHES", "PAT", "clone"),
        EgressRow("EP|CRED=gh|HOST=GHES", "gh", "clone"),
        EgressRow("EP|CRED=helper|HOST=GHES", "helper", "clone"),
        EgressRow("EP|CRED=none|HOST=GHES", "none", "clone"),
        *(
            EgressRow(f"E2|HOST=GHES|CRED={cred}|EGRESS={egress}", cred, egress)
            for cred in ("PAT", "gh", "helper", "none")
            for egress in _E2
        ),
    ),
)
