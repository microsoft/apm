"""Egress rows for EGRESS registry: the world's private APM package registry.

The registry is a generic host (no git platform): ``registry.lifecycle.test``
serves the REST contract of ``src/apm_cli/deps/registry/client.py`` and
answers only ``Authorization: Bearer <token>``. Its credential is the
per-registry token (``APM_REGISTRY_TOKEN_<NAME>``), which APM presents only
to the origin the user config binds the registry name to. No platform PAT
may reach it. Without the token the install is refused (exit 1) and APM
sends no credential at all.

Unregistered until the world proxy can script them: the REDIRECT
same-origin / cross-origin rows and PROXY_ONLY=1.
"""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice
from tests.lifecycle.world import registry

SLICE = HostSlice(
    host="generic",
    git_host=registry.HOST,
    family=(registry.HOST,),
    base=registry.URL,
    spec="",
    pat_env=registry.TOKEN_ENV,
    rows=(
        EgressRow("E2|HOST=generic|CRED=PAT|EGRESS=registry", "PAT", "registry"),
        EgressRow("E2|HOST=generic|CRED=none|EGRESS=registry", "none", "registry", exit=1),
        EgressRow("EP|CRED=PAT|EGRESS=registry", "PAT", "registry"),
        EgressRow("EP|CRED=none|EGRESS=registry", "none", "registry", exit=1),
        EgressRow("EP|EGRESS=registry|HOST=generic", "PAT", "registry"),
        EgressRow("EP|EGRESS=registry|PROXY_ONLY=0", "PAT", "registry"),
        EgressRow("EP|EGRESS=registry|REDIRECT=none", "PAT", "registry"),
        EgressRow("E1|CRED=PAT|EGRESS=registry|REDIRECT=none", "PAT", "registry"),
        *(
            EgressRow(f"E1|CRED=PAT|EGRESS=registry|REDIRECT={kind}", "PAT", "registry")
            for kind in ("same-origin", "cross-origin")
        ),
        *(
            EgressRow(f"EP|EGRESS=registry|REDIRECT={kind}", "PAT", "registry")
            for kind in ("same-origin", "cross-origin")
        ),
    ),
)
