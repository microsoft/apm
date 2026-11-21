"""Egress rows for HOST ghe.com: a GitHub Enterprise Cloud tenant (*.ghe.com).

The world answers the tenant's git host, its ``api.`` subdomain and
``https://<host>/api/v3`` (contents and commits), so api-probe, policy-fetch
and marketplace-fetch all reach the tenant's own origins. The REDIRECT
same-origin / cross-origin and PROXY_ONLY=1 rows wait for proxy scripting.
"""

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
        *(
            EgressRow(f"E2|HOST=ghe.com|CRED={cred}|EGRESS={egress}", cred, egress)
            for cred in ("PAT", "gh", "none")
            for egress in ("api-probe", "clone", "marketplace-fetch", "policy-fetch")
        ),
        *(
            EgressRow(f"EP|EGRESS={egress}|HOST=ghe.com", "PAT", egress)
            for egress in ("api-probe", "clone", "marketplace-fetch", "policy-fetch")
        ),
        EgressRow("EP|HOST=ghe.com|PROXY_ONLY=0", "PAT", "clone"),
        EgressRow("EP|HOST=ghe.com|REDIRECT=none", "PAT", "clone"),
        EgressRow("EP|HOST=ghe.com|PROXY_ONLY=1", "PAT", "clone"),
        *(
            EgressRow(f"EP|HOST=ghe.com|REDIRECT={kind}", "PAT", "clone")
            for kind in ("same-origin", "cross-origin")
        ),
    ),
)
