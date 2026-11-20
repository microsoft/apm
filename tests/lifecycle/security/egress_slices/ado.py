"""Egress rows for HOST ADO: dev.azure.com.

Anonymous rows run against the world's default Azure DevOps host. Rows with
``auth=True`` first make it refuse anonymous requests (``require_ado_auth``),
so each step of APM's chain (ADO_APM_PAT -> az bearer -> credential fill) is
in play: PAT and helper answer with the world's ADO PAT, ``az`` hands out the
world's bearer.

``api-probe`` installs an Azure DevOps virtual-file dependency (a prompt in a
repository the row serves), which APM fetches only through the Items REST API
(``download_ado_file``).
The REDIRECT same-origin / cross-origin and PROXY_ONLY=1 rows
wait for proxy scripting.
"""

from __future__ import annotations

import subprocess

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice
from tests.lifecycle.world.services import ADO_PAT, ORG

# An Azure DevOps virtual-file dependency: APM fetches it through the Items REST
# API (download_ado_file), so its install is the ADO api-probe.
_PROMPT_REPO = "p-prompt"
_PROMPT_PATH = "prompts/egress.prompt.md"


def _serve_prompt_repository(world) -> None:
    """Author a one-prompt repository and serve it as ``lifecycle/p-prompt``."""
    source = world.root / "ado-prompt-src"
    (source / "prompts").mkdir(parents=True)
    (source / _PROMPT_PATH).write_text(
        "---\ndescription: Egress probe prompt\n---\n# Egress probe\n", encoding="ascii"
    )
    git = ("git", "-c", "user.name=lifecycle", "-c", "user.email=lifecycle@example.invalid")
    for args in (
        ("init", "-q", "--initial-branch=main", str(source)),
        ("-C", str(source), "add", "."),
        ("-C", str(source), "commit", "-q", "-m", "prompt"),
        ("-C", str(source), "tag", "v1.0.0"),
        ("clone", "-q", "--bare", str(source), str(world.root / "ado-prompt.git")),
    ):
        subprocess.run((*git, *args), env=world.env, check=True)
    world.services.serve(f"{ORG}/{_PROMPT_REPO}", world.root / "ado-prompt.git")


SLICE = HostSlice(
    host="ADO",
    git_host="dev.azure.com",
    family=("dev.azure.com",),
    base="https://dev.azure.com/lifecycle/lifecycle/_git",
    spec="https://dev.azure.com/lifecycle/lifecycle/_git/p-skill#v1.0.0",
    pat_env="ADO_APM_PAT",
    helper_password=ADO_PAT,
    api_spec=f"https://dev.azure.com/{ORG}/{ORG}/_git/{_PROMPT_REPO}/{_PROMPT_PATH}#v1.0.0",
    api_setup=_serve_prompt_repository,
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
            for egress in ("api-probe", "marketplace-fetch", "policy-fetch")
        ),
        *(
            EgressRow(f"E2|HOST=ADO|CRED=az-bearer|EGRESS={egress}", "az-bearer", egress, auth=True)
            for egress in ("api-probe", "clone", "policy-fetch")
        ),
        *(
            EgressRow(f"E2|HOST=ADO|CRED=helper|EGRESS={egress}", "helper", egress, auth=True)
            for egress in ("api-probe", "clone", "marketplace-fetch", "policy-fetch")
        ),
        *(
            EgressRow(f"E2|HOST=ADO|CRED=none|EGRESS={egress}", "none", egress)
            for egress in ("api-probe", "marketplace-fetch", "policy-fetch")
        ),
        *(
            EgressRow(f"EP|CRED=az-bearer|EGRESS={egress}", "az-bearer", egress, auth=True)
            for egress in ("api-probe", "clone", "policy-fetch")
        ),
        EgressRow("EP|CRED=az-bearer|HOST=ADO", "az-bearer", "clone", auth=True),
        EgressRow("EP|CRED=az-bearer|PROXY_ONLY=0", "az-bearer", "clone", auth=True),
        EgressRow("EP|CRED=az-bearer|REDIRECT=none", "az-bearer", "clone", auth=True),
        *(
            EgressRow(
                f"E1|CRED=az-bearer|EGRESS={egress}|REDIRECT=none", "az-bearer", egress, auth=True
            )
            for egress in ("api-probe", "clone", "policy-fetch")
        ),
        EgressRow("EP|CRED=helper|HOST=ADO", "helper", "clone", auth=True),
        *(
            EgressRow(f"EP|EGRESS={egress}|HOST=ADO", "PAT", egress, auth=True)
            for egress in ("api-probe", "marketplace-fetch", "policy-fetch")
        ),
    ),
)
