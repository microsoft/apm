"""Org policy ``manifest.content_types``: the primitive kinds a package may ship."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .models import CheckResult, PolicySubject

if TYPE_CHECKING:
    from .schema import ManifestPolicy

# manifest.content_types names primitive kinds; singular and plural spellings agree.
_CONTENT_KINDS = {
    **dict.fromkeys(("instruction", "instructions"), "instruction"),
    **dict.fromkeys(("prompt", "prompts"), "prompt"),
    **dict.fromkeys(("command", "commands"), "command"),
    **dict.fromkeys(("skill", "skills"), "skill"),
    **dict.fromkeys(("agent", "agents", "chatmode", "chatmodes"), "agent"),
    **dict.fromkeys(("hook", "hooks"), "hook"),
    **dict.fromkeys(("extension", "extensions", "canvas"), "extension"),
    **dict.fromkeys(("workflow", "workflows"), "workflow"),
    **dict.fromkeys(("context", "contexts", "memory"), "context"),
}
_KIND_SUFFIXES = (
    (".instructions.md", "instruction"),
    (".prompt.md", "prompt"),
    (".agent.md", "agent"),
    (".chatmode.md", "agent"),
    (".context.md", "context"),
    (".memory.md", "context"),
)
_NOT_PACKAGE_CONTENT = frozenset({"apm_modules", ".git", "node_modules"})


def content_kind(entry: str) -> str:
    """The canonical kind an allow entry or source directory names (unknown names stay)."""
    name = entry.strip().lower()
    return _CONTENT_KINDS.get(name, name)


def content_types_allow(policy: ManifestPolicy) -> frozenset[str] | None:
    """The primitive kinds ``manifest.content_types.allow`` admits; None when unrestricted."""
    allow = (policy.content_types or {}).get("allow")
    if allow is None:
        return None
    entries = allow if isinstance(allow, list | tuple) else [allow]
    return frozenset(content_kind(entry) for entry in entries if isinstance(entry, str))


def package_content_kinds(package_path: Path) -> frozenset[str]:
    """The primitive kinds a package directory ships, as ``content_types`` names them.

    Every primitive source directory APM deploys from counts: ``.apm/<dir>/`` and
    the plugin root directories (``bundle/plugin_layout.PLUGIN_ROOT_DIRS``). A
    directory with no ``content_types`` name is its own kind, so an allow list
    that does not name it refuses it.
    """
    from ..bundle.plugin_layout import PLUGIN_HOOKS_CONFIG, PLUGIN_ROOT_DIRS

    kinds: set[str] = set()
    for path in package_path.rglob("*"):
        relative = path.relative_to(package_path).parts
        if _NOT_PACKAGE_CONTENT.intersection(relative) or not path.is_file():
            continue
        name = path.name
        kinds.update(kind for suffix, kind in _KIND_SUFFIXES if name.endswith(suffix))
        if name == "SKILL.md":
            kinds.add("skill")
        if relative == (PLUGIN_HOOKS_CONFIG,):
            kinds.add("hook")
        elif relative[0] == ".apm" and len(relative) > 2:
            kinds.add(content_kind(relative[1]))
        elif relative[0] in PLUGIN_ROOT_DIRS and len(relative) > 1:
            kinds.add(content_kind(relative[0]))
    return frozenset(kinds | _declared_plugin_kinds(package_path))


def _declared_plugin_kinds(package_path: Path) -> set[str]:
    """Kinds a plugin manifest declares, so components at custom paths count (fail closed)."""
    from ..deps.plugin_parser import parse_plugin_manifest
    from ..utils.helpers import find_plugin_json

    plugin_json = find_plugin_json(package_path, include_codex=True)
    if plugin_json is None:
        return set()
    try:
        manifest = parse_plugin_manifest(plugin_json)
    except (OSError, ValueError):
        return {"plugin-manifest"}
    declared = {"agents", "skills", "commands", "hooks"} & {
        key for key, value in manifest.items() if value not in (None, [], {}, "")
    }
    return {content_kind(key) for key in declared}


def package_content_roots(
    deps: Iterable, *, base_dir: Path, modules_dir: Path
) -> dict[str, tuple[Path, ...]]:
    """Dependency key -> the directory ``audit --ci`` judges for it.

    The apm_modules copy (what install deployed), else a local source; ``()``
    when neither is on disk, which the check reports as unverifiable.
    """
    roots: dict[str, tuple[Path, ...]] = {}
    for dep in deps:
        candidates = [dep.get_install_path(modules_dir)]
        if dep.is_local and dep.local_path:
            local = Path(dep.local_path).expanduser()
            candidates.append(local if local.is_absolute() else base_dir / local)
        found = next((path for path in candidates if path.is_dir()), None)
        roots[dep.get_unique_key()] = (found.resolve(),) if found else ()
    return roots


def check_content_types(
    packages: Mapping[str, Iterable[Path]], policy: ManifestPolicy
) -> CheckResult:
    """Each package ships only primitive kinds ``manifest.content_types`` allows.

    A package whose content is not on disk cannot be verified and fails.
    """
    allow = content_types_allow(policy)
    if allow is None:
        return CheckResult(
            name="content-types", passed=True, message="No content type restriction configured"
        )
    allowed = ", ".join(sorted(allow)) or "none"
    subjects = []
    for name, roots in sorted(packages.items()):
        roots = tuple(roots)
        if not roots:
            reason = (
                "its content is not on disk, so org policy manifest.content_types "
                "cannot be verified"
            )
        else:
            blocked = frozenset().union(*map(package_content_kinds, roots)) - allow
            if not blocked:
                continue
            reason = (
                f"ships {', '.join(sorted(blocked))}; org policy manifest.content_types "
                f"allows only: {allowed}"
            )
        subjects.append(PolicySubject(ref=name, reason=reason))
    if not subjects:
        return CheckResult(
            name="content-types", passed=True, message="All packages ship allowed content types"
        )
    return CheckResult(
        name="content-types",
        passed=False,
        message=f"{len(subjects)} package(s) ship content types the policy does not allow",
        details=[f"{subject.ref}: {subject.reason}" for subject in subjects],
        subjects=tuple(subjects),
    )


def audit_content_types(
    lock, policy: ManifestPolicy, project_root: Path, modules_root: Path | None
) -> CheckResult:
    """``audit --ci``: the locked packages' content against ``manifest.content_types``."""
    roots = package_content_roots(
        (locked.to_dependency_ref() for locked in lock.get_package_dependencies()),
        base_dir=project_root,
        modules_dir=modules_root or project_root / "apm_modules",
    )
    return check_content_types(roots, policy)
