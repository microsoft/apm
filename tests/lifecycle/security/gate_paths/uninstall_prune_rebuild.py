"""G path: Uninstalling another package rebuilds shared integrations, then prune runs;
both must leave nothing the gate no longer allows. (#4107)"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import PathSpec

PATH = PathSpec("uninstall-prune-rebuild", sibling=True, act=("apm uninstall p-skill", "apm prune"))
