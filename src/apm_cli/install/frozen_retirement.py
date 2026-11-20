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

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.request import InstallRequest


def _withheld_sources(
    lockfile: LockFile,
    lockfile_dir: Path,
    deploy_root: Path,
    targets: Sequence[Any],
    *,
    no_policy: bool,
) -> dict[str, str]:
    """Ask the exec gate owner why each locked package's executables are withheld."""
    from apm_cli.constants import APM_MODULES_DIR, APM_YML_FILENAME
    from apm_cli.install.exec_gate import withheld_executable_sources
    from apm_cli.policy.outcome_routing import resolve_trust_policy

    return withheld_executable_sources(
        lockfile,
        lockfile_dir / APM_MODULES_DIR,
        lockfile_dir / APM_YML_FILENAME,
        policy_provider=lambda: resolve_trust_policy(
            deploy_root, no_policy=no_policy, announce=False, raise_blocking_errors=False
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
    """Fail ``--frozen`` when a file the resolved graph records is missing (#4043).

    Runs the presence verifier ``apm audit`` deployed-files-present uses. A
    hook the gate retired under an unchanged lock fails here, named with its
    decision (deny wins, #3944); a kept undeclared package does not (#3662).
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

    sources = _withheld_sources(
        committed,
        lockfile_dir,
        deploy_root,
        targets,
        no_policy=request.no_policy,
    )
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


def preview_executable_retirements(
    lockfile: LockFile, lockfile_dir: Path, *, no_policy: bool
) -> list[str]:
    """Packages whose deployed executables an install would retire now (``--dry-run``)."""
    from apm_cli.policy.ci_checks import resolve_audit_scope
    from apm_cli.security.executables import TRUST_DEPLOYED

    deploy_root, _user_scope, targets = resolve_audit_scope(lockfile_dir, lockfile)
    sources = _withheld_sources(lockfile, lockfile_dir, deploy_root, targets, no_policy=no_policy)
    return [
        sources[key]
        for key in sorted(sources)
        if lockfile.dependencies[key].exec_status in (None, TRUST_DEPLOYED)
    ]
