"""Egress rows for HOST generic: a host that belongs to no git platform.

The world's generic host is its MCP registry. No platform credential belongs
to it, so a row passes when none reaches it. Unregistered, missing world
support: EP|CRED=helper|HOST=generic (the world serves no generic git host).
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
    ),
)
