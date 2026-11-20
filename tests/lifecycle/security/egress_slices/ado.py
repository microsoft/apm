"""Egress rows for HOST ADO: dev.azure.com.

Anonymous rows run against the world's default Azure DevOps host. Rows with
``auth=True`` first make it refuse anonymous requests (``require_ado_auth``),
so each step of APM's chain (ADO_APM_PAT -> az bearer -> credential fill) is
in play: PAT and helper answer with the world's ADO PAT, ``az`` hands out the
world's bearer.

Unregistered: every ADO ``api-probe`` row (E2, EP and E1 with CRED=az-bearer).
An install from Azure DevOps sends only git requests; APM's ADO REST egress
(``download_ado_file``) serves virtual-file dependencies, which the world does
not author yet. The REDIRECT same-origin / cross-origin and PROXY_ONLY=1 rows
wait for proxy scripting.
"""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice
from tests.lifecycle.world.services import ADO_PAT

SLICE = HostSlice(
    host="ADO",
    git_host="dev.azure.com",
    family=("dev.azure.com",),
    base="https://dev.azure.com/lifecycle/lifecycle/_git",
    spec="https://dev.azure.com/lifecycle/lifecycle/_git/p-skill#v1.0.0",
    pat_env="ADO_APM_PAT",
    helper_password=ADO_PAT,
    rows=(
        EgressRow("EP|CRED=PAT|HOST=ADO", "PAT", "clone"),
        EgressRow("EP|CRED=none|HOST=ADO", "none", "clone"),
        *(
            EgressRow(f"E2|HOST=ADO|CRED={cred}|EGRESS={egress}", cred, egress)
            for cred in ("PAT", "none")
            for egress in ("clone",)
        ),
        EgressRow("EP|EGRESS=clone|HOST=ADO", "PAT", "clone"),
        EgressRow("EP|HOST=ADO|PROXY_ONLY=0", "PAT", "clone"),
        EgressRow("EP|HOST=ADO|REDIRECT=none", "PAT", "clone"),
        # Auth-required world (require_ado_auth): anonymous requests get 401, so
        # each credential of APM's chain (PAT -> az bearer -> credential fill) is in play.
        *(
            EgressRow(f"E2|HOST=ADO|CRED=PAT|EGRESS={egress}", "PAT", egress, auth=True)
            for egress in ("marketplace-fetch", "policy-fetch")
        ),
        *(
            EgressRow(f"E2|HOST=ADO|CRED=az-bearer|EGRESS={egress}", "az-bearer", egress, auth=True)
            for egress in ("clone", "policy-fetch")
        ),
        *(
            EgressRow(f"E2|HOST=ADO|CRED=helper|EGRESS={egress}", "helper", egress, auth=True)
            for egress in ("clone", "marketplace-fetch", "policy-fetch")
        ),
        *(
            EgressRow(f"E2|HOST=ADO|CRED=none|EGRESS={egress}", "none", egress)
            for egress in ("marketplace-fetch", "policy-fetch")
        ),
        *(
            EgressRow(f"EP|CRED=az-bearer|EGRESS={egress}", "az-bearer", egress, auth=True)
            for egress in ("clone", "policy-fetch")
        ),
        EgressRow("EP|CRED=az-bearer|HOST=ADO", "az-bearer", "clone", auth=True),
        EgressRow("EP|CRED=az-bearer|PROXY_ONLY=0", "az-bearer", "clone", auth=True),
        EgressRow("EP|CRED=az-bearer|REDIRECT=none", "az-bearer", "clone", auth=True),
        *(
            EgressRow(
                f"E1|CRED=az-bearer|EGRESS={egress}|REDIRECT=none", "az-bearer", egress, auth=True
            )
            for egress in ("clone", "policy-fetch")
        ),
        EgressRow("EP|CRED=helper|HOST=ADO", "helper", "clone", auth=True),
        *(
            EgressRow(f"EP|EGRESS={egress}|HOST=ADO", "PAT", egress, auth=True)
            for egress in ("marketplace-fetch", "policy-fetch")
        ),
    ),
)
