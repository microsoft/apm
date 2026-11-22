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


def content_types_allow(policy: ManifestPolicy) -> frozenset[str] | None:
    """The primitive kinds ``manifest.content_types.allow`` admits; None when unrestricted."""
    allow = (policy.content_types or {}).get("allow")
    if allow is None:
        return None
    entries = allow if isinstance(allow, list | tuple) else [allow]
    return frozenset(
        _CONTENT_KINDS[entry.strip().lower()]
        for entry in entries
        if isinstance(entry, str) and entry.strip().lower() in _CONTENT_KINDS
    )


def _directory_kind(name: str) -> str:
    """The kind a primitive source directory holds; an unnamed one is its own kind."""
    return _CONTENT_KINDS.get(name, name)


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
            kinds.add(_directory_kind(relative[1]))
        elif relative[0] in PLUGIN_ROOT_DIRS and len(relative) > 1:
            kinds.add(_directory_kind(relative[0]))
    return frozenset(kinds)


def package_content_roots(
    deps: Iterable, *, base_dir: Path, modules_dir: Path, source_first: bool
) -> dict[str, tuple[Path, ...]]:
    """Display name -> every directory whose content a package deploys from.

    Install (*source_first*) judges a local package's source, which integrate
    re-copies and deploys, together with any apm_modules copy (fail closed).
    Audit judges the copy and falls back to the source. A package with no
    content on disk maps to ``()``.
    """
    roots: dict[str, tuple[Path, ...]] = {}
    for dep in deps:
        copy = dep.get_install_path(modules_dir)
        source = None
        if dep.is_local and dep.local_path:
            local = Path(dep.local_path).expanduser()
            source = local if local.is_absolute() else base_dir / local
        if source_first:
            found = [path for path in (source, copy) if path is not None and path.is_dir()]
        else:
            found = [next((p for p in (copy, source) if p is not None and p.is_dir()), None)]
        roots[dep.get_display_name()] = tuple(path.resolve() for path in found if path)
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
        source_first=False,
    )
    return check_content_types(roots, policy)
