"""Claude rules a pre-#3939 install deployed, for ``apm pack``.

Claude routing (#3939) compiles always-on and directory instructions into
``AGENTS.md`` instead of deploying ``.claude/rules/`` files. A bundle ships
deployed files and its consumers do not compile, so the pack renders those
rules through the install projection (``InstructionIntegrator``) and both the
APM and plugin bundles keep the bytes a pre-#3939 pack shipped. Claude is
active when the lockfile records a Claude deployment, else when the producer's
install resolves it (``targets:`` in ``apm.yml``, else harness detection),
never from the pack target alone. Ambiguous detection skips the rules with a
warning instead of guessing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path


def _packs_claude(effective_target: object) -> bool:
    targets = effective_target if isinstance(effective_target, list) else [effective_target]
    return any(str(name) in ("claude", "all") for name in targets)


def _recorded_claude(lockfile) -> bool:
    """Return whether the lockfile records any Claude deployment (recorded wins)."""
    from ..core.deployment_ledger import DeploymentLedgerCodec

    ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    if any(record.locator.target == "claude" for record in ledger.records.values()):
        return True
    return any(
        path.startswith(".claude/")
        for dep in lockfile.get_all_dependencies()
        for path in dep.deployed_files
    )


def _producer_deploys_claude(project_root: Path) -> bool | None:
    """Return whether the producer's install resolves Claude; ``None`` if ambiguous."""
    from ..core.errors import AmbiguousHarnessError, NoHarnessError
    from ..core.scope import manifest_file
    from ..core.target_detection import resolve_targets
    from ..models.apm_package import APMPackage, package_target_declaration

    manifest = manifest_file(project_root)
    declared = (
        package_target_declaration(APMPackage.from_apm_yml(manifest)).restriction()
        if manifest.is_file()
        else None
    )
    try:
        return "claude" in resolve_targets(project_root, yaml_targets=declared).targets
    except AmbiguousHarnessError:
        return None
    except NoHarnessError:
        return False


def routed_claude_rules(
    lockfile,
    project_root: Path,
    effective_target: object = None,
    dependency_keys: Collection[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Return ``{dependency key: {rule path: content}}`` the pack must add.

    *effective_target* is the APM-format pack filter; the plugin format,
    which ships every deployed file, passes ``None``. *dependency_keys*
    limits rendering to the dependencies the bundle ships.
    """
    if lockfile is None:
        return {}
    if effective_target is not None and not _packs_claude(effective_target):
        return {}
    active = True if _recorded_claude(lockfile) else _producer_deploys_claude(project_root)
    if active is False:
        return {}
    from ..integration.instruction_integrator import routed_claude_rule_renders

    rules = routed_claude_rule_renders(
        lockfile,
        project_root,
        project_root / "apm_modules",
        include_local=False,
        verified=True,
        dependency_keys=dependency_keys,
    )
    if active is None and rules:
        from ..utils.console import _rich_warning

        _rich_warning(
            f"Skipped {sum(map(len, rules.values()))} Claude rule(s) routed to AGENTS.md: "
            "this project's targets are ambiguous, so pack cannot tell whether install "
            "used Claude. Declare 'targets:' in apm.yml and pack again.",
            symbol="warning",
        )
        return {}
    return rules


def merged_routed_claude_rules(
    rendered: dict[str, dict[str, str]], deployed: Mapping[str, Path] | None = None
) -> dict[str, str]:
    """Flatten per-dependency rules into one bundle tree; refuse divergent same-path renders.

    Before #3939 two dependencies writing the same rule path collided at
    install; the pack must not silently keep only the last one. *deployed*
    maps each bundle path the pack already ships to its file on disk: a
    routed rule at one of those paths must carry the same bytes, and is
    then left out of the returned files (the deployed copy ships).
    """
    merged: dict[str, str] = {}
    owners: dict[str, str] = {}
    for dep_key, rules in rendered.items():
        for path, text in rules.items():
            if path in merged and merged[path] != text:
                raise ValueError(
                    f"Cannot pack {path}: dependencies {owners[path]} and {dep_key} both route "
                    "a different instruction with that name to AGENTS.md, and the bundle can "
                    "ship only one. Rename one of the instruction files."
                )
            merged[path] = text
            owners.setdefault(path, dep_key)
    for path in [path for path in merged if deployed and path in deployed]:
        disk = deployed[path]
        if disk.is_file() and disk.read_bytes() != merged[path].encode("utf-8"):
            raise ValueError(
                f"Cannot pack {path}: dependency {owners[path]} routes an instruction with "
                "that name to AGENTS.md, but another dependency deploys a different file at "
                "that path, and the bundle can ship only one. Rename one of the instruction files."
            )
        del merged[path]
    return merged


def write_routed_claude_rules(bundle_dir: Path, rendered: dict[str, str]) -> None:
    """Write rendered rules inside *bundle_dir* (LF, contained)."""
    from ..utils.atomic_io import write_text_lf

    bundle_root = bundle_dir.resolve()
    for rel_path, text in rendered.items():
        dest = bundle_dir / rel_path
        if not dest.resolve().is_relative_to(bundle_root):
            raise ValueError(f"Refusing to write outside bundle directory: {rel_path!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(dest, text)
