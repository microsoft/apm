"""Which harnesses load a scope's native plugins, and from which settings files.

Claude Code and GitHub Copilot CLI both read the two plugin activation keys
(see :mod:`.settings`) from their documented settings files (OpenAPM Part A, A1).
At project scope APM writes the committed files, ``.claude/settings.json`` and
``.github/copilot/settings.json``; at user scope it writes each harness's user
``settings.json``.

Copilot CLI also reads a repository's ``.claude/settings.json`` and
``.claude/settings.local.json`` for these keys. Its precedence between those
files and its own is not documented, so APM relies on the order observed on
Copilot CLI 1.0.112 (:data:`COPILOT_PROJECT_READ_ORDER`), pinned by a test.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from .settings import ENABLED_KEY, MARKETPLACES_KEY

CLAUDE = "claude"
COPILOT = "copilot"
NATIVE_HARNESSES = (CLAUDE, COPILOT)

PROJECT_SETTINGS = {
    CLAUDE: ".claude/settings.json",
    COPILOT: ".github/copilot/settings.json",
}
"""The committed settings file APM writes for each harness at project scope."""

CLAUDE_PROJECT_READ_ORDER = (".claude/settings.json", ".claude/settings.local.json")
COPILOT_PROJECT_READ_ORDER = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".github/copilot/settings.json",
    ".github/copilot/settings.local.json",
)
"""Copilot CLI's project settings files for plugin keys, lowest precedence first.

Observed on Copilot CLI 1.0.112: for the same plugin enable key a later
file overrides an earlier one, so ``.github/copilot/`` wins over ``.claude/``.
"""

_AGENT_PLUGIN = "agent_plugin"


def settings_path(harness: str, *, deploy_root: Path, project: bool) -> Path:
    """The settings file APM writes for *harness* at one scope."""
    if project:
        return Path(deploy_root) / PROJECT_SETTINGS[harness]
    from apm_cli.integration.targets import resolve_user_root

    return resolve_user_root(harness) / "settings.json"


def read_order(harness: str, *, deploy_root: Path, project: bool) -> tuple[Path, ...]:
    """Every settings file *harness* reads plugin keys from, lowest precedence first."""
    if not project:
        return (settings_path(harness, deploy_root=deploy_root, project=False),)
    order = COPILOT_PROJECT_READ_ORDER if harness == COPILOT else CLAUDE_PROJECT_READ_ORDER
    return tuple(Path(deploy_root) / relative for relative in order)


def _section(path: Path, key: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    value = document.get(key) if isinstance(document, dict) else None
    return value if isinstance(value, dict) else {}


def effective_enable(files: Sequence[Path], key: str) -> tuple[Any, Path | None]:
    """The enable value for *key* the harness applies, and the file that set it."""
    value: Any = None
    source: Path | None = None
    for path in files:
        section = _section(path, ENABLED_KEY)
        if key in section:
            value, source = section[key], path
    return value, source


def foreign_marketplace(files: Sequence[Path], name: str, expected: Any) -> Path | None:
    """The first file declaring marketplace *name* with a source other than *expected*."""
    for path in files:
        value = _section(path, MARKETPLACES_KEY).get(name)
        if value is not None and value != expected:
            return path
    return None


def entry_harnesses(locked: Any, *, project: bool, configured: Collection[str]) -> frozenset[str]:
    """Harnesses that load one locked native plugin: the entry's targets.

    An Agent Plugin is a Copilot plugin. A Claude plugin loads in Claude at
    user scope, and at project scope in each native harness the project
    targets, narrowed by the dependency's own ``targets:`` subset.
    """
    if getattr(locked, "package_type", None) == _AGENT_PLUGIN:
        return frozenset({COPILOT})
    if not project:
        return frozenset({CLAUDE})
    selected = set(configured) & set(NATIVE_HARNESSES)
    subset = set(getattr(locked, "target_subset", None) or ())
    if subset:
        selected &= subset
    return frozenset(selected)


def configured_harnesses(project_root: Path, *, extra: Collection[Any] = ()) -> frozenset[str]:
    """Native harnesses the project targets: ``apm.yml`` targets or detection, plus *extra*.

    A one-shot ``--target`` run passes its targets as *extra*; it never drops a
    harness the project itself targets, so its declarations survive the run.
    """
    import contextlib

    from apm_cli.integration.targets import resolve_targets
    from apm_cli.models.apm_package import APMPackage, package_target_selection

    declared = None
    manifest = Path(project_root) / "apm.yml"
    with contextlib.suppress(Exception):
        declared = package_target_selection(APMPackage.from_apm_yml(manifest))
    try:
        targets = resolve_targets(
            project_root, user_scope=False, explicit_target=declared, create_config=False
        )
    except Exception:  # target resolution errors are reported by install itself
        targets = []
    names = {getattr(target, "name", target) for target in [*targets, *extra]}
    return frozenset(names & set(NATIVE_HARNESSES))
