"""Egress rows for HOST generic: a host that belongs to no git platform.

The world's generic host is its MCP registry. No platform credential belongs
to it, so a row passes when none reaches it. The generic git host
(git.lifecycle.test) carries its own rows in ``generic_git.py``; the APM
package registry carries its rows in ``registry.py``.

PROXY_ONLY=1: the registry the operator configured is the sanctioned route,
not a bypass (src/apm_cli/registry/client.py, _refuse_direct_egress_when_proxy_only;
docs/enterprise/registry-proxy.md). ``forbid_direct`` keeps it reachable, and
the row proves nothing else went direct.
"""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice
from tests.lifecycle.world.services import REGISTRY_HOST

SLICE = HostSlice(
    host="generic",
    git_host=REGISTRY_HOST,
    family=(),
    base=f"https://{REGISTRY_HOST}",
    spec="",
    rows=(
        EgressRow("EP|CRED=PAT|HOST=generic", "PAT", "mcp-registry", presented=False),
        EgressRow("EP|CRED=none|HOST=generic", "none", "mcp-registry"),
        EgressRow("E2|HOST=generic|CRED=none|EGRESS=mcp-registry", "none", "mcp-registry"),
        EgressRow("EP|CRED=none|EGRESS=mcp-registry", "none", "mcp-registry"),
        EgressRow("EP|EGRESS=mcp-registry|HOST=generic", "none", "mcp-registry"),
        EgressRow("EP|EGRESS=mcp-registry|PROXY_ONLY=0", "none", "mcp-registry"),
        EgressRow("EP|EGRESS=mcp-registry|REDIRECT=none", "none", "mcp-registry"),
        EgressRow("EP|HOST=generic|PROXY_ONLY=0", "none", "mcp-registry"),
        EgressRow("EP|HOST=generic|REDIRECT=none", "none", "mcp-registry"),
        EgressRow("EP|HOST=generic|PROXY_ONLY=1", "none", "mcp-registry"),
        EgressRow("EP|EGRESS=mcp-registry|PROXY_ONLY=1", "none", "mcp-registry"),
        *(
            EgressRow(f"EP|HOST=generic|REDIRECT={kind}", "none", "mcp-registry")
            for kind in ("same-origin", "cross-origin")
        ),
        *(
            EgressRow(f"EP|EGRESS=mcp-registry|REDIRECT={kind}", "none", "mcp-registry")
            for kind in ("same-origin", "cross-origin")
        ),
    ),
)
