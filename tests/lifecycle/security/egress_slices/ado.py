"""Egress rows for HOST ADO: dev.azure.com.

Unregistered, missing world support: EP|CRED=helper|HOST=ADO and
EP|CRED=az-bearer|HOST=ADO. The world's Azure DevOps host answers anonymous
requests, so APM never reaches the az and credential-helper steps of its
chain (ADO_APM_PAT -> az bearer -> credential fill).

Also unregistered: E2|HOST=ADO|*|EGRESS=api-probe (an install from the world's
anonymous ADO host sends no API request), marketplace-fetch (no ADO repository
file API route is served) and policy-fetch (policy status with a dev.azure.com
origin sends no request the world answers).
"""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice

SLICE = HostSlice(
    host="ADO",
    git_host="dev.azure.com",
    family=("dev.azure.com",),
    base="https://dev.azure.com/lifecycle/lifecycle/_git",
    spec="https://dev.azure.com/lifecycle/lifecycle/_git/p-skill#v1.0.0",
    pat_env="ADO_APM_PAT",
    rows=(
        EgressRow("EP|CRED=PAT|HOST=ADO", "PAT", "clone"),
        EgressRow("EP|CRED=none|HOST=ADO", "none", "clone"),
        *(
            EgressRow(f"E2|HOST=ADO|CRED={cred}|EGRESS={egress}", cred, egress)
            for cred in ("PAT", "none")
            for egress in ("clone",)
        ),
    ),
)
