"""Helpers for install-time package reference resolution (structured apm.yml entries).

Extracted from ``apm_cli.commands.install`` to keep the command module smaller.
Call sites pass ``dependency_reference_cls`` and GitLab resolver callables so
tests that patch ``apm_cli.commands.install.DependencyReference`` and
``_try_resolve_gitlab_direct_shorthand`` keep working.
"""

from __future__ import annotations

import builtins
import urllib.parse
from collections.abc import Callable
from typing import Any

from apm_cli.install.gitlab_resolver import _GITLAB_DIRECT_SHORTHAND_UNRESOLVED
from apm_cli.models.dependency.subsets import parse_skill_subset

GIT_PARENT_USER_SCOPE_ERROR = (
    "git: parent dependencies are not supported at user scope. "
    "Use project scope or specify explicit git URL."
)


def dependency_reference_to_yaml_entry(dep_ref: Any) -> dict:
    """Serialize a structured dependency reference for ``apm.yml`` storage."""
    entry = {"git": dep_ref.to_github_url()}
    if dep_ref.virtual_path:
        entry["path"] = dep_ref.virtual_path
    if dep_ref.reference:
        entry["ref"] = dep_ref.reference
    if dep_ref.alias:
        entry["alias"] = dep_ref.alias
    return entry


def normalize_github_skill_url_packages(
    packages: Any,
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]], list[tuple[str, str]]]:
    """Normalize GitHub ``SKILL.md`` URLs into repo refs plus implicit skills.

    A URL copied from GitHub's file view (or raw.githubusercontent.com) points at
    a concrete file, but APM installs skill bundles as ``repo`` + ``skills:``.
    Return the normalized package tuple, a per-package skill map, and validation
    failures for GitHub skill URLs that are malformed.
    """
    normalized_packages: list[str] = []
    package_skill_sets: dict[str, set[str]] = {}
    invalid_outcomes: list[tuple[str, str]] = []

    for package in packages:
        if not isinstance(package, str):
            normalized_packages.append(package)
            continue
        try:
            normalized = normalize_github_skill_url_package(package)
        except ValueError as exc:
            invalid_outcomes.append((package, str(exc)))
            continue
        if normalized is None:
            normalized_packages.append(package)
            continue

        package_ref, skill_subset = normalized
        if package_ref not in normalized_packages:
            normalized_packages.append(package_ref)
        package_skill_sets.setdefault(package_ref, set()).update(skill_subset)

    package_skill_subsets = {
        package_ref: tuple(sorted(skills)) for package_ref, skills in package_skill_sets.items()
    }
    return tuple(normalized_packages), package_skill_subsets, invalid_outcomes


def normalize_github_skill_url_package(package: str) -> tuple[str, tuple[str, ...]] | None:
    """Return ``(repo_ref, skills)`` for GitHub ``skills/.../SKILL.md`` URLs."""
    parsed = urllib.parse.urlparse(package.strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    path_parts = _url_path_parts(parsed.path)

    if host == "github.com":
        if len(path_parts) < 3 or path_parts[2] != "blob":
            return None
        owner, repo = path_parts[0], path_parts[1]
        return _normalize_github_skill_url_parts(
            parsed.scheme,
            owner,
            repo,
            path_parts[3:],
            raw=package,
        )

    if host == "raw.githubusercontent.com":
        if len(path_parts) < 3:
            return None
        owner, repo = path_parts[0], path_parts[1]
        return _normalize_github_skill_url_parts(
            parsed.scheme,
            owner,
            repo,
            path_parts[2:],
            raw=package,
        )

    return None


def _url_path_parts(path: str) -> list[str]:
    return [urllib.parse.unquote(part) for part in path.split("/") if part]


def _normalize_github_skill_url_parts(
    scheme: str,
    owner: str,
    repo: str,
    ref_and_path_parts: list[str],
    *,
    raw: str,
) -> tuple[str, tuple[str, ...]]:
    if not ref_and_path_parts or ref_and_path_parts[-1] != "SKILL.md":
        raise ValueError(
            "GitHub skill install URLs must point to a SKILL.md file under "
            "`skills/<skill-name>/SKILL.md`."
        )

    try:
        skills_index = ref_and_path_parts.index("skills")
    except ValueError as exc:
        raise ValueError(
            f"GitHub SKILL.md install URLs must point inside a `skills/` directory. Got: `{raw}`."
        ) from exc

    ref_parts = ref_and_path_parts[:skills_index]
    skill_parts = ref_and_path_parts[skills_index + 1 : -1]
    if not ref_parts:
        raise ValueError(
            "GitHub skill install URLs must include a branch, tag, or commit before `skills/`."
        )
    if not skill_parts:
        raise ValueError(
            "GitHub skill install URLs must include a skill name between `skills/` and `SKILL.md`."
        )

    skill_subset = tuple(parse_skill_subset(["/".join(skill_parts)]))
    repo_ref = f"{scheme}://github.com/{owner}/{repo}#{'/'.join(ref_parts)}"
    return repo_ref, skill_subset


def resolve_parsed_dependency_reference(
    package: str,
    marketplace_dep_ref: Any | None,
    *,
    dependency_reference_cls: Any,
    try_resolve_gitlab_direct_shorthand: Callable[..., Any],
    auth_resolver: Any,
    verbose: bool,
    resolve_artifactory_boundary: Callable[..., Any] | None = None,
    logger: Any = None,
) -> tuple[Any, bool]:
    """Parse or probe *package* into a ``DependencyReference``.

    Returns ``(dep_ref, direct_virtual_resolved)`` where the second flag is
    True when the dep should be persisted as a structured ``git:`` + ``path:``
    entry in ``apm.yml`` (the canonical shorthand cannot round-trip the probed
    boundary).  The two probe paths gate this flag differently:

    * **GitLab shorthand** -- True only when the resolved ref is a virtual
      package (``is_virtual and virtual_path``); a probe that lands on a bare
      repo with no virtual path stays in canonical shorthand form.
    * **Artifactory boundary** -- True whenever the probe rebuilt the ref
      (parse-time guess differed from the proxy-verified split); a probe that
      merely confirms the parse-time boundary keeps the original ref so
      apm.yml stays in its existing shape.

    For Artifactory deps the optional ``resolve_artifactory_boundary`` is
    authoritative: it returns the proxy-verified boundary or raises -- there
    is no silent fallback to the parse-time guess.

    Raises:
        ValueError: When GitLab or Artifactory probing fails to resolve.
    """
    dep_ref = (
        marketplace_dep_ref
        if marketplace_dep_ref is not None
        else dependency_reference_cls.parse(package)
    )
    if (
        marketplace_dep_ref is None
        and dependency_reference_cls.needs_gitlab_direct_shorthand_probing(package, dep_ref)
    ):
        resolved = try_resolve_gitlab_direct_shorthand(
            package,
            auth_resolver,
            verbose=verbose,
        )
        if resolved is None:
            raise ValueError(_GITLAB_DIRECT_SHORTHAND_UNRESOLVED)
        dep_ref = resolved
        direct_virtual_resolved = bool(dep_ref.is_virtual and dep_ref.virtual_path)
        return dep_ref, direct_virtual_resolved
    if marketplace_dep_ref is None and resolve_artifactory_boundary is not None:
        # The resolver decides its own applicability -- it short-circuits for
        # deps that don't route through the Artifactory proxy.  When it rebuilds
        # the dep_ref, the canonical shorthand can't round-trip the verified
        # boundary, so persist as a structured ``git:`` + ``path:`` entry.
        resolved = resolve_artifactory_boundary(
            package,
            auth_resolver,
            verbose=verbose,
            dep_ref=dep_ref,
            logger=logger,
        )
        if resolved is not dep_ref:
            return resolved, True
    return dep_ref, False


def user_scope_rejection_reason(dep_ref: Any, scope: Any) -> str | None:
    """Return a validation-fail reason if *dep_ref* is invalid at user scope.

    Per #937, only relative local paths are rejected at user scope -- absolute
    local paths are unambiguous and flow through the same _copy_local_package
    code path as project scope.
    """
    if scope is None:
        return None
    from pathlib import Path

    from apm_cli.core.scope import InstallScope

    if dep_ref.is_local and scope is InstallScope.USER:
        local_path = dep_ref.local_path or ""
        # Match the rest of the install pipeline (sources.py, phases/resolve.py)
        # which expanduser()s local paths before consuming them: `~/pkg` is
        # absolute after expansion and must NOT be rejected here.
        if not Path(local_path).expanduser().is_absolute():
            return (
                "relative local paths are not supported at user scope (--global). "
                "Use an absolute path or a remote reference (owner/repo) instead"
            )
    if dep_ref.is_parent_repo_inheritance and scope is InstallScope.USER:
        return GIT_PARENT_USER_SCOPE_ERROR
    return None


def get_existing_skill_subset(
    current_deps: builtins.list,
    identity: str,
    *,
    dependency_reference_cls: Any,
) -> builtins.list[str] | None:
    """Return the persisted ``skills:`` list for *identity*, or None."""
    for dep_entry in current_deps:
        try:
            if isinstance(dep_entry, builtins.str):
                existing_ref = dependency_reference_cls.parse(dep_entry)
            elif isinstance(dep_entry, builtins.dict):
                existing_ref = dependency_reference_cls.parse_from_dict(dep_entry)
            else:
                continue
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if existing_ref.get_identity() == identity:
            subset = getattr(existing_ref, "skill_subset", None)
            return list(subset) if subset else None
    return None


def normalize_and_merge_skill_subset(
    cli_subset: builtins.tuple[str, ...],
    current_deps: builtins.list,
    identity: str,
    *,
    dependency_reference_cls: Any,
) -> builtins.list[str]:
    """Normalize CLI ``--skill`` names and merge with existing manifest skills.

    Strips whitespace, drops empty strings, deduplicates, then unions with
    the persisted ``skills:`` list from ``apm.yml`` so that repeated
    ``--skill`` invocations are additive (issue #1771).

    Returns a sorted, deduplicated list ready for ``dep_ref.skill_subset``.
    """
    seen: builtins.set[str] = builtins.set()
    for s in cli_subset:
        s = s.strip()
        if s:
            seen.add(s)
    existing = get_existing_skill_subset(
        current_deps, identity, dependency_reference_cls=dependency_reference_cls
    )
    if existing:
        seen.update(existing)
    return sorted(seen)


def effective_deploy_skill_subset(
    *,
    skill_subset_from_cli: bool,
    cli_subset: builtins.tuple[str, ...] | builtins.list[str] | None,
    persisted_subset: builtins.tuple[str, ...] | builtins.list[str] | None,
) -> builtins.tuple[str, ...] | None:
    """Resolve the skill subset to *deploy* for a SKILL_BUNDLE.

    ``--skill`` is additive (issue #1786): a targeted ``--skill B`` install must
    land on top of skills already pinned for the bundle instead of erasing them.
    The deployment therefore uses the union of the persisted manifest subset and
    the current CLI ``--skill`` values -- not the raw CLI subset alone.

    ``--skill '*'`` (signalled by ``skill_subset_from_cli`` True with an empty
    ``cli_subset``) resets the bundle to its full set, so this returns ``None``
    meaning "deploy all skills".

    Returns a sorted, deduplicated tuple, or ``None`` for "deploy all".
    """
    if skill_subset_from_cli and not cli_subset:
        return None  # --skill '*' -> full bundle
    merged: builtins.set[str] = builtins.set()
    if persisted_subset:
        merged.update(persisted_subset)
    if cli_subset:
        merged.update(cli_subset)
    return builtins.tuple(sorted(merged)) or None


def cli_skill_subset(
    skill_names: builtins.tuple[str, ...],
) -> builtins.tuple[str, ...] | None:
    """Resolve raw CLI ``--skill`` names to a subset, or None for absent / ``'*'``.

    ``--skill '*'`` means "all skills" (same as absent); the resolver still
    learns the flag was present via a separate CLI-origin flag, so this
    collapses both the absent and ``'*'`` cases to None.
    """
    if skill_names and not any(s == "*" for s in skill_names):
        return builtins.tuple(skill_names)
    return None


def apply_cli_skill_pin(
    dep_ref: Any,
    cli_subset: builtins.tuple[str, ...] | None,
    skill_subset_from_cli: bool,
    current_deps: builtins.list,
    apm_yml_entries: dict,
    *,
    dependency_reference_cls: Any,
    logger: Any | None = None,
) -> None:
    """Attach, merge, or reset a CLI ``--skill`` pin on ``dep_ref`` in place.

    With an explicit ``cli_subset``, merge it additively with any persisted
    ``skills:`` so repeated ``--skill`` invocations union rather than replace
    (issue #1771). With ``--skill '*'`` (``skill_subset_from_cli`` True and an
    empty ``cli_subset``), reset the pin back to the full bundle and record the
    refreshed plain-string ``apm.yml`` entry under the reference's canonical key
    so manifest and on-disk state agree on the whole bundle (issue #1786 reset).
    """
    identity = dep_ref.get_identity()
    if cli_subset:
        dep_ref.skill_subset = normalize_and_merge_skill_subset(
            cli_subset,
            current_deps,
            identity,
            dependency_reference_cls=dependency_reference_cls,
        )
        return
    if skill_subset_from_cli:
        dep_ref.skill_subset = None
        apm_yml_entries[dep_ref.to_canonical()] = dep_ref.to_apm_yml_entry()
        if logger:
            logger.verbose_detail(
                f"    [i] {identity}: skill pin reset to full bundle "
                "(--skill '*'); a later bare 'apm install' deploys all skills"
            )


def manifest_has_different_entry_for_identity(
    current_deps: builtins.list,
    identity: str,
    canonical: str,
    *,
    dependency_reference_cls: Any,
) -> bool:
    """Return True when apm.yml already has *identity* but not *canonical*."""
    for dep_entry in current_deps:
        try:
            if isinstance(dep_entry, builtins.str):
                existing_ref = dependency_reference_cls.parse(dep_entry)
            elif isinstance(dep_entry, builtins.dict):
                existing_ref = dependency_reference_cls.parse_from_dict(dep_entry)
            else:
                continue
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if existing_ref.get_identity() == identity:
            return existing_ref.to_canonical() != canonical
    return False


def update_existing_dependency_entry_if_needed(
    current_deps: builtins.list,
    *,
    already_in_deps: bool,
    apm_yml_entries: dict,
    canonical: str,
    dep_ref: Any,
    identity: str,
    dependency_reference_cls: Any,
    logger: Any = None,
) -> bool:
    """Rewrite an existing manifest dep when the requested ref changed."""
    should_update = already_in_deps and (
        canonical in apm_yml_entries
        or (
            dep_ref.reference
            and manifest_has_different_entry_for_identity(
                current_deps,
                identity,
                canonical,
                dependency_reference_cls=dependency_reference_cls,
            )
        )
    )
    if should_update:
        merge_structured_entry_into_current_deps(
            current_deps,
            apm_yml_entries.get(canonical, dep_ref.to_apm_yml_entry()),
            identity,
            canonical,
            dependency_reference_cls=dependency_reference_cls,
            logger=logger,
        )
    return should_update


def merge_structured_entry_into_current_deps(
    current_deps: builtins.list,
    structured_entry: dict,
    identity: str,
    canonical: str,
    *,
    dependency_reference_cls: Any,
    logger: Any = None,
) -> None:
    """Replace or append *structured_entry* in *current_deps* by *identity*."""
    replaced = False
    for idx, dep_entry in enumerate(current_deps):
        try:
            if isinstance(dep_entry, builtins.str):
                existing_ref = dependency_reference_cls.parse(dep_entry)
            elif isinstance(dep_entry, builtins.dict):
                existing_ref = dependency_reference_cls.parse_from_dict(dep_entry)
            else:
                continue
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if existing_ref.get_identity() == identity:
            current_deps[idx] = structured_entry
            replaced = True
            if logger:
                logger.verbose_detail(
                    f"Updated existing dependency entry to structured git+path form: {canonical}"
                )
            break
    if not replaced:
        current_deps.append(structured_entry)


def persist_dependency_list_if_changed(
    *,
    dependencies_changed: bool,
    data: dict,
    dep_section: str,
    current_deps: builtins.list,
    apm_yml_path: Any,
    apm_yml_filename: str,
    logger: Any = None,
    rich_error: Callable[[str], None],
    sys_exit: Callable[[int], None],
) -> None:
    """Write *apm.yml* when *current_deps* was updated without new packages."""
    if not dependencies_changed:
        return
    data[dep_section]["apm"] = current_deps
    try:
        from apm_cli.utils.yaml_io import dump_yaml

        dump_yaml(data, apm_yml_path)
        if logger:
            logger.success(f"Updated {apm_yml_filename} dependency entries")
    except Exception as e:
        if logger:
            logger.error(f"Failed to write {apm_yml_filename}: {e}")
        else:
            rich_error(f"Failed to write {apm_yml_filename}: {e}")
        sys_exit(1)
