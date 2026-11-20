"""Egress rows for HOST GHES: a GitHub Enterprise Server named by GITHUB_HOST."""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice

_REDIRECTS = ("same-origin", "cross-origin")
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
        # Pairwise EP and E1 rows. GHES serves every operation with every
        # supported credential, so it carries the rows whose host is free.
        *(
            EgressRow(f"EP|CRED={cred}|EGRESS={egress}", cred, egress)
            for cred in ("PAT", "gh", "helper", "none")
            for egress in _E2
        ),
        *(
            EgressRow(f"E1|CRED={cred}|EGRESS={egress}|REDIRECT=none", cred, egress)
            for cred in ("PAT", "gh", "helper")
            for egress in _E2
        ),
        *(EgressRow(f"EP|EGRESS={egress}|HOST=GHES", "PAT", egress) for egress in _E2),
        *(
            EgressRow(f"EP|CRED={cred}|{mode}", cred, "clone")
            for cred in ("PAT", "gh", "helper", "none")
            for mode in ("PROXY_ONLY=0", "REDIRECT=none")
        ),
        *(
            EgressRow(f"EP|EGRESS={egress}|{mode}", "PAT", egress)
            for egress in _E2
            for mode in ("PROXY_ONLY=0", "REDIRECT=none")
        ),
        EgressRow("EP|HOST=GHES|PROXY_ONLY=0", "PAT", "clone"),
        EgressRow("EP|HOST=GHES|REDIRECT=none", "PAT", "clone"),
        EgressRow("EP|PROXY_ONLY=0|REDIRECT=none", "PAT", "clone"),
        # REDIRECT rows: every request to the host's origins answers 307, to an
        # unserved path on the same origin or to a spare host (test_egress._redirect).
        *(
            EgressRow(f"E1|CRED={cred}|EGRESS={egress}|REDIRECT={kind}", cred, egress)
            for cred in ("PAT", "gh", "helper")
            for egress in _E2
            for kind in _REDIRECTS
        ),
        *(
            EgressRow(f"EP|CRED={cred}|REDIRECT={kind}", cred, "clone")
            for cred in ("PAT", "gh", "helper", "none")
            for kind in _REDIRECTS
        ),
        *(
            EgressRow(f"EP|EGRESS={egress}|REDIRECT={kind}", "PAT", egress)
            for egress in _E2
            for kind in _REDIRECTS
        ),
        *(EgressRow(f"EP|HOST=GHES|REDIRECT={kind}", "PAT", "clone") for kind in _REDIRECTS),
        *(EgressRow(f"EP|PROXY_ONLY=0|REDIRECT={kind}", "PAT", "clone") for kind in _REDIRECTS),
    ),
)
