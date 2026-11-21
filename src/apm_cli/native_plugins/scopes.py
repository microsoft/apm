"""Where each scope's native plugins are declared, and under which name.

One table answers, per scope, which harnesses load a natively activated
plugin, which documented settings file each harness reads, the marketplace
path written there, and the marketplace name (OpenAPM req-na-003, req-na-007).

Project scope (#3921) writes committed files with a relative path so a fresh
clone loads the plugins without APM:

* Claude Code reads ``.claude/settings.json``;
* GitHub Copilot CLI reads ``.github/copilot/settings.json``, and also reads
  ``.claude/settings.json``. :data:`COPILOT_PROJECT_SETTINGS_PRECEDENCE`
  records Copilot's file-priority order (later wins), pinned by a test.

User scope keeps per-machine files and an absolute path.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

from .record import PLUGINS_RELATIVE

CLAUDE = "claude"
COPILOT = "copilot"
HARNESSES = (CLAUDE, COPILOT)

PROJECT_MARKETPLACE_PATH = f"./{PLUGINS_RELATIVE}"
"""Marketplace path written at project scope, relative to the project root."""

PROJECT_SETTINGS_RELATIVE = {
    CLAUDE: ".claude/settings.json",
    COPILOT: ".github/copilot/settings.json",
}

COPILOT_PROJECT_SETTINGS_PRECEDENCE = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".github/copilot/settings.json",
    ".github/copilot/settings.local.json",
)
"""Project settings files GitHub Copilot CLI reads, lowest priority first.

A plugin enable value in a later file wins over the same key in an earlier
one, so a user's ``false`` in ``.github/copilot/settings.json`` turns a
plugin off for Copilot even when ``.claude/settings.json`` enables it for
Claude. Observed on Copilot CLI 1.0.112; pinned by an integration test.
"""

AGENT_PLUGIN_TYPE = "agent_plugin"
_PROJECT_PREFIX = "apm-p-"
_USER_PREFIX = "apm-u-"


def is_project_path(marketplace_path: str) -> bool:
    """A recorded marketplace path is project scope when it is the relative profile root."""
    return marketplace_path == PROJECT_MARKETPLACE_PATH


def plugin_harnesses(*, agent_plugin: bool, user_scope: bool) -> frozenset[str]:
    """Harnesses that load one native plugin of this kind at this scope.

    A Claude-format plugin (``.claude-plugin/plugin.json``) loads in Claude
    Code at both scopes and in Copilot at project scope, where both read the
    committed profile root. An Agent Plugins v1 package loads in Copilot only.
    The caller intersects the result with the dependency's effective targets.
    """
    if agent_plugin:
        return frozenset({COPILOT})
    return frozenset({CLAUDE}) if user_scope else frozenset({CLAUDE, COPILOT})


def record_harnesses(package_type: str | None, marketplace_path: str) -> frozenset[str]:
    """Harnesses a locked ``native_plugin`` record may be declared to."""
    return plugin_harnesses(
        agent_plugin=package_type == AGENT_PLUGIN_TYPE,
        user_scope=not is_project_path(marketplace_path),
    )


def native_source_current(locked: Any, source_root: Path) -> bool:
    """Whether a local native plugin's source still carries its recorded root digest.

    A changed source is re-copied so the executable gate re-judges it (#4446);
    an unchanged one never is, or its files would be rebuilt loose.
    """
    from apm_cli.claude_plugins.admission import (
        ClaudeNativePluginError,
        fingerprint_native_claude_source,
    )

    record = locked.native_plugin
    try:
        source = (Path(source_root) / locked.to_dependency_ref().local_path).resolve()
        observed = fingerprint_native_claude_source(
            source, portable=is_project_path(record.marketplace_path)
        )
    except (ClaudeNativePluginError, OSError, ValueError, TypeError):
        return False
    return observed == record.fingerprint


def native_harnesses(lockfile: Any, dep_key: str, package_info: Any = None) -> frozenset[str]:
    """Harnesses that load a locked dependency whole through its ``native_plugin`` record.

    A rebuild (uninstall's survivor re-integration) must never also deploy
    the dependency loose to them. *package_info*, rebuilt from the normalized
    ``apm_modules`` copy, takes the package type the lock recorded, as audit
    replay does, so its own deployment plan routes it (parked or native) as
    install did rather than as a loose APM package.
    """
    locked = None if lockfile is None else lockfile.dependencies.get(dep_key)
    if package_info is not None and getattr(locked, "package_type", None):
        from apm_cli.models.apm_package import PackageType

        with contextlib.suppress(ValueError):
            package_info.package_type = PackageType(locked.package_type)
    record = getattr(locked, "native_plugin", None)
    if record is None:
        return frozenset()
    return record_harnesses(locked.package_type, record.marketplace_path)


def carried_native_row(
    existing_lockfile: Any, dep_key: str, target_subset: Collection[str] | None, run_targets
) -> Any:
    """The previous lock row of a native plugin this run's targets never reach, else ``None``.

    The run's targets, narrowed by the dependency's own ``targets:`` and by
    the harnesses its format and scope allow, leave nothing: for example
    ``--target copilot`` on a ``targets: [claude]`` dependency. Such a run
    keeps that row whole and deploys nothing for the dependency, so the lock,
    the deployed root, its declarations and any loose files keep agreeing.
    """
    dependencies = getattr(existing_lockfile, "dependencies", None) or {}
    prior = dependencies.get(dep_key)
    record = getattr(prior, "native_plugin", None)
    if record is None:
        return None
    allowed = record_harnesses(prior.package_type, record.marketplace_path)
    if target_subset:
        allowed &= frozenset(target_subset)
    return None if allowed & frozenset(run_targets or ()) else prior


def settings_path(harness: str, *, user_scope: bool, project_root: Path) -> Path:
    """The settings file *harness* documents for this scope (criterion A1)."""
    if user_scope:
        from apm_cli.integration.targets import resolve_user_root

        return resolve_user_root(harness) / "settings.json"
    return Path(project_root) / PROJECT_SETTINGS_RELATIVE[harness]


def user_marketplace_name(apm_home: Path) -> str:
    """Deterministic user-scope marketplace name, unique per APM home (req-na-007)."""
    digest = hashlib.sha256(str(Path(apm_home).resolve()).encode("utf-8")).hexdigest()
    return f"{_USER_PREFIX}{digest[:8]}"


def project_identity(project_root: Path) -> str:
    """The project identity a project-scope marketplace name derives from.

    The ``origin`` remote as ``host/owner/repo`` (lowercase, no scheme, user or
    ``.git``), so every clone of one repository agrees; without a remote, the
    ``name`` in ``apm.yml``. Never the user, host or checkout path.
    """
    from apm_cli.policy._remote import _git_remote_origin_url, _remote_url_parts

    parts = _remote_url_parts(_git_remote_origin_url(project_root) or "")
    if parts is not None:
        host, segments = parts
        return "/".join([host, *segments]).lower()
    try:
        from apm_cli.core.scope import manifest_file
        from apm_cli.models.apm_package import APMPackage

        return f"apm.yml:{APMPackage.from_apm_yml(manifest_file(Path(project_root))).name}"
    except Exception:
        return "apm.yml:"


def project_marketplace_name(
    project_root: Path, recorded: str | None, catalog_name: str | None
) -> str:
    """Project-scope marketplace name, stable across clones and unique per project.

    The name the committed lock (or, without one, the committed APM catalog)
    already records wins, so a fork or a renamed remote keeps its binding;
    a first activation derives it from :func:`project_identity`.
    """
    for name in (recorded, catalog_name):
        if name and name.startswith(_PROJECT_PREFIX):
            return name
    digest = hashlib.sha256(project_identity(project_root).encode("utf-8")).hexdigest()
    return f"{_PROJECT_PREFIX}{digest[:8]}"


def committed_catalog_name(plugins_root: Path) -> str | None:
    """The marketplace name of an APM-generated catalog already in the profile root."""
    from .layout import CATALOG_OWNER, CATALOG_RELATIVE

    catalog = plugins_root / CATALOG_RELATIVE
    if catalog.is_symlink() or not catalog.is_file():
        return None
    try:
        data = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("owner") != {"name": CATALOG_OWNER}:
        return None
    name = data.get("name")
    return name if isinstance(name, str) else None
