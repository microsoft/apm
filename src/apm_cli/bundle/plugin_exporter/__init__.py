"""Plugin exporter -- transforms APM packages into plugin-native directories.

Produces a standalone plugin directory that Copilot CLI, Claude Code, or other
plugin hosts can consume directly.  The output contains plugin-spec artefacts
(``agents/``, ``skills/``, ``commands/``, ``plugin.json``) plus an embedded
``apm.lock.yaml`` carrying provenance metadata + a per-file SHA-256 manifest
under ``pack.bundle_files`` (issue #1098).
"""

from ...utils.console import _rich_info
from .collectors import (
    _rename_prompt,  # already private in collectors
)
from .collectors import (
    collect_apm_components as _collect_apm_components,
)
from .collectors import (
    collect_bare_skill as _collect_bare_skill,
)
from .collectors import (
    collect_hooks_from_apm as _collect_hooks_from_apm,
)
from .collectors import (
    collect_hooks_from_root as _collect_hooks_from_root,
)
from .collectors import (
    collect_mcp as _collect_mcp,
)
from .collectors import (
    collect_root_plugin_components as _collect_root_plugin_components,
)
from .exporter import (
    ExportOptions,
    _rich_warning,
    _update_plugin_json_paths,
    export_plugin_bundle,
)
from .hooks_mcp import _MAX_MERGE_DEPTH
from .hooks_mcp import deep_merge as _deep_merge
from .utils import (
    get_dev_dependency_urls as _get_dev_dependency_urls,
)
from .utils import (
    merge_file_map as _merge_file_map,
)
from .utils import (
    validate_output_rel as _validate_output_rel,
)

__all__ = [
    "_MAX_MERGE_DEPTH",
    "ExportOptions",
    # private names re-exported for backward-compat / tests
    "_collect_apm_components",
    "_collect_bare_skill",
    "_collect_hooks_from_apm",
    "_collect_hooks_from_root",
    "_collect_mcp",
    "_collect_root_plugin_components",
    "_deep_merge",
    "_get_dev_dependency_urls",
    "_merge_file_map",
    "_rename_prompt",
    "_rich_info",
    "_rich_warning",
    "_update_plugin_json_paths",
    "_validate_output_rel",
    "export_plugin_bundle",
]
