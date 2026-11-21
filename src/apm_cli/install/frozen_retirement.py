"""Name executables the gate retired under an unchanged lock (#4043).

Deny wins (#3944): when a deny, a removed approval or org policy withholds a
package's executables, install removes the files it deployed for them, even
under ``--frozen``. ``--frozen`` may not rewrite ``apm.lock.yaml``, so it then
fails, naming each retired file with the decision that retired it.
``--dry-run`` previews the same decisions without removing anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core import remediation

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext
    from apm_cli.install.exec_gate import WithheldExecutables
    from apm_cli.install.request import InstallRequest


def _withheld(
    lockfile: LockFile,
    lockfile_dir: Path,
    deploy_root: Path,
    targets: Sequence[Any],
    *,
    no_policy: bool,
    scope: Any,
) -> dict[str, WithheldExecutables]:
    """Ask the exec gate owner why each locked package's executables are withheld.

    Modules sit beside the lock, but grants come from the manifest the
    install gate reads: the source project's ``apm.yml`` (``--root DIR``
    keeps it outside *lockfile_dir*), or ``$APM_HOME/apm.yml`` at user scope.
    An unscoped install keeps its lock beside its manifest.
    """
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.core.scope import (
        get_source_root,
        is_user_scope,
        manifest_root,
        scope_manifest_path,
    )
    from apm_cli.install.exec_gate import withheld_executables
    from apm_cli.policy.outcome_routing import resolve_trust_policy

    source_root = lockfile_dir if scope is None else get_source_root(scope)
    return withheld_executables(
        lockfile,
        lockfile_dir / APM_MODULES_DIR,
        scope_manifest_path(source_root, user_scope=is_user_scope(scope)),
        policy_provider=lambda: resolve_trust_policy(
            manifest_root(deploy_root, user_scope=is_user_scope(scope)),
            no_policy=no_policy,
            announce=False,
            raise_blocking_errors=False,
        ),
        targets=list(targets),
    )


def graph_claims(
    committed: LockFile, missing: Sequence[str], active_keys: frozenset[str] | None
) -> list[str]:
    """Drop missing files claimed only by packages outside the resolved graph.

    ``--frozen`` keeps an undeclared package's lock claims without deploying
    it (#3662), so its absent files are not this install's to verify.
    """
    if active_keys is None:
        return list(missing)
    from apm_cli.deps.lockfile import _SELF_KEY

    graph_paths = {
        path
        for key, dep in committed.dependencies.items()
        if key in active_keys or key == _SELF_KEY
        for path in dep.deployed_files
    }
    return [path for path in missing if path in graph_paths]


def enforce_frozen_presence(
    request: InstallRequest,
    committed: LockFile,
    withheld: Iterable[str],
    lockfile_dir: Path,
    active_keys: frozenset[str] | None,
) -> None:
    """Fail ``--frozen`` when the lock no longer matches deployed executables.

    Runs the presence verifier ``apm audit`` deployed-files-present uses. A
    hook the gate retired under an unchanged lock fails here, named with its
    decision (deny wins, #3944); a kept undeclared package does not (#3662).
    Then each recorded ``exec_status`` must still be the gate's decision,
    through the staleness owner ``apm update`` and ``apm audit --ci`` use (#4053).
    """
    from apm_cli.policy.ci_checks import find_missing_deployed_files, resolve_audit_scope

    deploy_root, _user_scope, targets = resolve_audit_scope(lockfile_dir, committed)
    missing, _gitignored = find_missing_deployed_files(deploy_root, committed, targets)
    if missing := graph_claims(committed, missing, active_keys):
        raise frozen_missing_files_error(
            request,
            committed,
            withheld,
            missing,
            lockfile_dir=lockfile_dir,
            deploy_root=deploy_root,
            targets=targets,
        )
    if stale := _stale_exec_decisions(request, committed, lockfile_dir, deploy_root, targets):
        # On a merged-config target (claude settings.json) a gated hook leaves
        # no recorded file missing; the recorded decision is the only evidence
        # that the lock is stale (#4053).
        raise _stale_decisions_error(stale)


def refuse_stale_exec_decisions(
    request: InstallRequest | InstallContext, committed: LockFile, lockfile_dir: Path
) -> None:
    """Refuse ``--frozen`` before any write when a recorded executables decision changed.

    The same staleness owner the post-deploy check asks (#4053), run once the
    frozen content, selection, MCP and hook-pin checks pass and before any
    executable deploys or retires, so a deny or revoked approval keeps every
    deployed file until a plain ``apm install`` retires it (RA-024). Only
    *request*'s ``scope`` and ``no_policy`` are read.
    """
    from apm_cli.policy.ci_checks import resolve_audit_scope

    deploy_root, _user_scope, targets = resolve_audit_scope(lockfile_dir, committed)
    if stale := _stale_exec_decisions(request, committed, lockfile_dir, deploy_root, targets):
        raise _stale_decisions_error(stale)


def _stale_decisions_error(stale: Sequence[Any]) -> Exception:
    """The ``--frozen`` refusal for recorded executables decisions that changed."""
    from apm_cli.install.errors import FrozenInstallError

    return FrozenInstallError(
        "--frozen: apm.lock.yaml records an executables decision the gate no longer makes.",
        reasons=[f"  - {decision.describe()}" for decision in stale],
        tip=f"Tip: {remediation.FROZEN_EXEC_DECISIONS_STALE.sentence()}",
    )


def _stale_exec_decisions(
    request: InstallRequest,
    committed: LockFile,
    lockfile_dir: Path,
    deploy_root: Path,
    targets: Sequence[Any],
) -> list[Any]:
    """Locked packages whose recorded ``exec_status`` the gate no longer makes (#4053).

    Indeterminate answers (unknown org policy, unreadable package) are not
    frozen failures: the install gate already applied its own posture.
    """
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.core.scope import (
        get_source_root,
        is_user_scope,
        manifest_root,
        scope_manifest_path,
    )
    from apm_cli.install.exec_gate import discovered_exec_policy, stale_exec_decisions

    scope = request.scope
    source_root = lockfile_dir if scope is None else get_source_root(scope)
    # Discover where the install gate does: APM_HOME at user scope, never the
    # HOME repository's remote (#4087).
    policy_root = manifest_root(deploy_root, user_scope=is_user_scope(scope))

    def _policy() -> Any:
        return None if request.no_policy else discovered_exec_policy(policy_root)

    stale, _indeterminate = stale_exec_decisions(
        committed,
        lockfile_dir / APM_MODULES_DIR,
        scope_manifest_path(source_root, user_scope=is_user_scope(scope)),
        policy_provider=_policy,
        targets=list(targets),
    )
    return stale


def refuse_withdrawn_bundle_lsp(
    request: InstallRequest, committed: LockFile, lockfile_dir: Path
) -> None:
    """Refuse ``--frozen`` before any write when a deny withdrew a deployed bundle LSP server.

    A local bundle is not in apm.yml, so no dependency check reaches it; the
    same rule plain ``apm install`` retires the server by (RA-041).
    """
    from apm_cli.core.scope import get_source_root, is_user_scope
    from apm_cli.install.lsp.integration import withdrawn_bundle_lsp_for_scope

    scope = request.scope
    if withdrawn := withdrawn_bundle_lsp_for_scope(
        committed,
        lockfile_dir,
        lockfile_dir if scope is None else get_source_root(scope),
        user_scope=is_user_scope(scope),
        no_policy=bool(request.no_policy),
    ):
        raise _stale_decisions_error(withdrawn)


def frozen_missing_files_error(
    request: InstallRequest,
    committed: LockFile,
    withheld: Iterable[str],
    missing: Sequence[str],
    *,
    lockfile_dir: Path,
    deploy_root: Path,
    targets: Sequence[Any],
) -> Exception:
    """The ``--frozen`` refusal for recorded files that are no longer on disk.

    A file is reported as retired when the gate now withholds its package's
    executables and the lock this install would have written no longer
    claims it. Any other missing file is reported as missing.
    """
    from apm_cli.core.scope import is_user_scope
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.errors import FrozenInstallError

    retired = _withheld(
        committed,
        lockfile_dir,
        deploy_root,
        targets,
        no_policy=request.no_policy,
        scope=request.scope,
    )
    sources = {key: entry.source for key, entry in retired.items()}
    candidates = [LockFile.from_yaml(payload) for payload in withheld]
    reasons = []
    for path in missing:
        owner = next(
            (key for key, dep in committed.dependencies.items() if path in dep.deployed_files),
            None,
        )
        still_claimed = any(
            path in getattr(candidate.dependencies.get(owner), "deployed_files", ())
            for candidate in candidates
        )
        if owner in sources and not still_claimed:
            reasons.append(f"  - {path}: retired because {sources[owner]}")
        else:
            reasons.append(f"  - {path} is recorded in apm.lock.yaml but missing")
    command = "apm install --global" if is_user_scope(request.scope) else "apm install"
    return FrozenInstallError(
        "--frozen: files recorded in apm.lock.yaml are missing from disk.",
        reasons=reasons,
        tip=(
            f"Tip: run '{command}' without --frozen, retaining --root DIR if specified, "
            "then commit apm.lock.yaml."
        ),
    )


_VERDICT_REASONS = {"denied": "denied", "gated_pending_approval": "not approved"}


def preview_executable_retirements(
    lockfile: LockFile, lockfile_dir: Path, *, no_policy: bool, scope: Any
) -> list[tuple[str, list[str]]]:
    """Packages whose deployed executables an install would retire now (``--dry-run``).

    Each package's deciding source comes with one line per withheld surface:
    its verdict and the deployed items the lock records for it (#4454).
    """
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.policy.ci_checks import resolve_audit_scope

    deploy_root, _user_scope, targets = resolve_audit_scope(lockfile_dir, lockfile)
    withheld = _withheld(
        lockfile, lockfile_dir, deploy_root, targets, no_policy=no_policy, scope=scope
    )
    modules_dir = lockfile_dir / APM_MODULES_DIR
    preview = []
    for key in sorted(withheld):
        dependency = lockfile.dependencies[key]
        surfaces = []
        for exec_type, state in withheld[key].types:
            reason = _VERDICT_REASONS.get(state, state)
            items = _retired_items(lockfile, dependency, exec_type, modules_dir)
            label = f"{exec_type} ({reason})"
            surfaces.append(f"{label}: {', '.join(items)}" if items else label)
        preview.append((withheld[key].source, surfaces))
    return preview


def _retired_items(
    lockfile: LockFile, dependency: Any, exec_type: str, modules_dir: Path
) -> list[str]:
    """The deployed items of *exec_type* the lock records for *dependency*.

    MCP and LSP servers are matched through the provenance token their
    integrators record for the declaring package; hooks and bin/ through the
    package's deployed files.
    """
    if exec_type == "mcp":
        from apm_cli.integration.mcp_config_view import mcp_package_owner

        install_path = dependency.to_dependency_ref().get_install_path(modules_dir)
        owner = mcp_package_owner(dependency.name, package_dir=install_path)
        return _provenance_names(lockfile.mcp_config_provenance, owner)
    if exec_type == "lsp":
        from apm_cli.install.lsp.integration import package_lsp_owner

        owner = package_lsp_owner(dependency.get_unique_key())
        return _provenance_names(lockfile.lsp_config_provenance, owner)
    if exec_type not in ("hooks", "bin"):
        return []
    return sorted(path for path in dependency.deployed_files if exec_type in path.split("/")[:-1])


def _provenance_names(provenance: dict, owner: str) -> list[str]:
    return sorted(
        name
        for name, owners in provenance.items()
        if owner in ([owners] if isinstance(owners, str) else owners)
    )
