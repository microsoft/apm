"""Egress rows for HOST generic: a host that belongs to no git platform.

The world's generic host is its MCP registry. No platform credential belongs
to it, so a row passes when none reaches it. Unregistered, missing world
support: EP|CRED=helper|HOST=generic and the generic clone and marketplace-fetch
rows (the world serves no generic git host), and every registry row (no APM
package registry service).
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
    ),
)
