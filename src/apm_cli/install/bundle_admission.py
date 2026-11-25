"""Bundle admission: the one owner every bundle deploy entry point calls.

``apm install <bundle>`` and ``apm unpack`` admit a bundle here before any
file is written: org policy is discovered and enforced through the shared
install preflight (outcome routing, dependency allow/deny, MCP and target
checks), then the bundle's integrity is checked under the policy's
``security.integrity.require_hashes`` setting. A local bundle and the
packages it records meet ``dependencies.deny``; only the packages meet
``dependencies.allow`` (:func:`bundle_policy_subjects`, #4646).

Each bundle format brings its own integrity check; the policy gate and the
``require_hashes`` decision stay here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from functools import partial

    from apm_cli.bundle.unpacker import UnpackCandidate
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.models.dependency.mcp import MCPDependency
    from apm_cli.models.dependency.reference import DependencyReference
    from apm_cli.policy.models import LocalBundleSubjects
    from apm_cli.policy.schema import ApmPolicy

IntegrityCheck = Callable[[bool], None]
"""Format-specific integrity check; receives whether policy requires hashes."""


def admit_bundle(
    *,
    policy_root: Path,
    apm_deps: Iterable[DependencyReference],
    mcp_deps: Sequence[MCPDependency],
    target_decision: EffectiveTargetDecision,
    integrity_check: IntegrityCheck,
    no_policy: bool,
    logger: Any,
    dry_run: bool,
    fetch_result: Any = None,
    registries: dict[str, str] | None = None,
    bundled_deps: LocalBundleSubjects | None = None,
) -> ApmPolicy | None:
    """Admit a bundle for deployment, or raise before anything is written.

    *bundled_deps* (see :func:`bundle_policy_subjects`) meet the org
    ``dependencies.deny`` list as a declared dependency would; under an allow
    list the bundle is judged by its recorded packages alone.

    Raises :class:`~apm_cli.install.errors.PolicyViolationError` when org
    policy blocks the bundle or cannot be verified under
    ``fetch_failure_default: block``, and the integrity check's own error
    when the bundle fails it.
    """
    from ..policy.install_preflight import run_policy_preflight
    from .integrity import require_hashes_enabled

    policy_fetch, _enforcement_active = run_policy_preflight(
        project_root=policy_root,
        apm_deps=list(apm_deps),
        mcp_deps=mcp_deps,
        no_policy=no_policy,
        logger=logger,
        dry_run=dry_run,
        effective_target=target_decision.canonical_targets,
        effective_target_source=target_decision.source,
        fetch_result=fetch_result,
        registries=registries,
        bundled_deps=() if bundled_deps is None else bundled_deps,
    )
    policy = policy_fetch.policy if policy_fetch is not None else None
    integrity_check(require_hashes_enabled(policy.security.integrity if policy else None))
    return policy


def refuse_unmanaged_bundle_files(
    policy: ApmPolicy | None,
    deploy: partial[Mapping[str, Any]],
    *,
    user_scope: bool,
    logger: Any,
    dry_run: bool,
) -> None:
    """Judge ``unmanaged_files.action: deny`` on the claims a bundle install leaves (#4826).

    *deploy* is the bundle's ``integrate_local_bundle`` call, run here as a
    dry run, which adopts or skips each file as the deploy does. Its claims
    join the current lockfile's, so a governed file none of them claims
    refuses the install before the bundle writes anything.
    """
    if policy is None or policy.enforcement == "off":
        return
    if policy.unmanaged_files.effective_action != "deny":
        return
    from ..core.deployment_ledger import DeploymentLedgerCodec
    from ..core.scope import InstallScope
    from ..deps.lockfile import LockFile
    from .commit_gate import refuse_unmanaged_files
    from .local_bundle_handler import _bundle_lock_root, _home_relative_claims

    project_root = deploy.args[1]
    # The dry run applies the deploy's own adopt-or-skip decision to every file.
    result = deploy(dry_run=True, logger=None, create_config=False)
    digests = result.get("deployed_file_hashes", {})
    hashes = {record: digests[record] for record in result.get("deployed_files", [])}
    files = sorted(hashes)
    if user_scope:
        files, hashes = _home_relative_claims(files, hashes, project_root)
    lockfile = _recorded_lockfile(_bundle_lock_root(user_scope)) or LockFile()
    scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    DeploymentLedgerCodec.record_local_bundle_files(lockfile, files, hashes, scope=scope)
    # The project's own entry carries those claims, as it does once read back.
    lockfile.dependencies["."] = lockfile.project_self_entry()
    refuse_unmanaged_files(logger, policy, project_root, lockfile, dry_run=dry_run)


def bundle_policy_subjects(bundle_info: Any) -> LocalBundleSubjects:
    """The package identities a local bundle presents to org dependency policy.

    The bundle itself, named by its ``plugin.json`` id and name as a local
    package (``_local/<name>``, as a local-path dependency is keyed), plus
    every package its embedded ``apm.lock.yaml`` records. A bundle without
    one presents its names only; one it cannot read is refused with
    ``ValueError``, never judged as recording nothing.
    """
    from ..models.dependency.reference import DependencyReference
    from ..policy.models import LocalBundleSubjects

    names = {
        str(value).strip()
        for value in (bundle_info.package_id, bundle_info.plugin_json.get("name"))
        if value and str(value).strip()
    }
    subjects = [
        DependencyReference(repo_url=f"_local/{name}", is_local=True, source="local")
        for name in sorted(names)
    ]
    root = bundle_info.source_dir
    try:
        locked = locked_package_refs(root / "apm.lock.yaml" if root is not None else None)
    except ValueError as exc:
        raise ValueError(
            f"Bundle apm.lock.yaml cannot be read ({exc.__cause__ or exc}), so org policy "
            "cannot check the packages it records. Repack the bundle with 'apm pack' "
            "and retry."
        ) from exc
    return LocalBundleSubjects(tuple(subjects), tuple(locked))


def locked_package_refs(path: Path | None) -> list[DependencyReference]:
    """Every package the lockfile at *path* records; none when it is absent.

    Read without side effects. A lockfile that exists but cannot be read
    raises ``ValueError``: org policy cannot judge packages it cannot list.
    """
    from ..deps.lockfile import LockFile

    if path is None or not (path.exists() or path.is_symlink()):
        return []
    try:
        lockfile = LockFile.from_yaml(path.read_text(encoding="utf-8"))
        return [dep.to_dependency_ref() for dep in lockfile.get_package_dependencies()]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(
            f"Cannot read {path.name} to check its packages against org policy: {exc}"
        ) from exc


def apm_bundle_admission(
    output_dir: Path, logger: Any, *, dry_run: bool
) -> Callable[[UnpackCandidate], None]:
    """Admission for ``apm unpack`` of a ``--format apm`` bundle.

    Org policy resolves from *output_dir*, the project the files deploy
    into, and checks the bundle's locked dependencies and pack target.
    Executable content never reaches here: the unpacker refuses it first.
    """

    def admit(candidate: UnpackCandidate) -> None:
        from ..deps.registry.auth import dependency_ref_with_registry_name_from_lockfile

        prepare_unpack_record(output_dir)
        registries = _project_registries(output_dir)
        admit_bundle(
            policy_root=output_dir,
            apm_deps=(
                dependency_ref_with_registry_name_from_lockfile(
                    dep.to_dependency_ref(), registries or {}, locked_dep=dep
                )
                for dep in candidate.lockfile.get_package_dependencies()
            ),
            mcp_deps=(),
            target_decision=_bundle_target_decision(candidate),
            integrity_check=apm_bundle_integrity(
                candidate.source_dir, candidate.lockfile, candidate.files, logger
            ),
            no_policy=False,
            logger=logger,
            dry_run=dry_run,
            registries=registries,
        )

    return admit


def _bundle_target_decision(candidate: UnpackCandidate) -> EffectiveTargetDecision:
    """The targets a ``--format apm`` bundle deploys to, for target policy.

    A bundle with no recorded pack target is evaluated as deploying to every
    target. A pack target this APM version does not know, or a bundle file
    outside its pack target's paths, is refused: the files would land where
    target policy cannot evaluate them.
    """
    from ..bundle.local_bundle import _extract_pack_targets
    from ..bundle.lockfile_enrichment import pack_target_prefixes
    from ..core.target_detection import EffectiveTargetDecision

    targets = _extract_pack_targets({"pack": candidate.pack_meta})
    if not targets:
        return EffectiveTargetDecision("all", "bundle files (no pack target recorded)")
    decision = EffectiveTargetDecision(targets, "bundle pack target")
    try:
        decision.canonical_targets  # noqa: B018 -- validate the projection
    except KeyError as exc:
        raise ValueError(
            f"Bundle pack target '{','.join(targets)}' is not a target this APM version "
            "supports. Repack the bundle with a supported '--target' and retry."
        ) from exc
    prefixes = pack_target_prefixes(targets)
    outside = [rel for rel in candidate.files if not rel.startswith(prefixes)]
    if outside:
        raise ValueError(
            f"Bundle files are outside its pack target '{','.join(targets)}':\n"
            + "\n".join(f"  - {rel}" for rel in outside)
            + "\nRepack the bundle with 'apm pack --format apm' and retry."
        )
    return decision


def _project_registries(output_dir: Path) -> dict[str, str] | None:
    """The ``registries:`` map of the project at *output_dir*, if any."""
    from ..core.scope import manifest_file
    from ..models.apm_package import APMPackage

    manifest = manifest_file(output_dir)
    if not manifest.is_file():
        return None
    try:
        package = APMPackage.from_apm_yml(manifest)
    except ValueError:
        # Unreadable: a registry policy then reports the registry as unconfigured.
        return None
    return getattr(package, "registries", None) or None


def prepare_unpack_record(output_dir: Path) -> None:
    """Refuse, before any file is written, a lockfile the unpack record cannot update."""
    root = unpack_record_root(output_dir)
    if root is None:
        return
    from ..deps.lockfile import (
        LockFile,
        LockfileRefusal,
        ensure_lockfile_writable,
        get_lockfile_path,
    )

    lockfile_path = get_lockfile_path(root)
    try:
        ensure_lockfile_writable(lockfile_path)
        if lockfile_path.exists():
            LockFile.read(lockfile_path)
    except LockfileRefusal as exc:
        raise ValueError(str(exc)) from exc


def unpack_record_root(output_dir: Path) -> Path | None:
    """The project whose lockfile records an unpack into *output_dir*, or ``None``.

    An APM project root (``apm.yml`` present) records the files, exactly as
    ``apm install <bundle>`` does; any other directory stays untracked.
    """
    from ..core.scope import manifest_file

    return output_dir if manifest_file(output_dir).is_file() else None


def _recorded_lockfile(root: Path) -> Any:
    """Read the project's lockfile without migrating a legacy ``apm.lock``."""
    from ..deps.lockfile import LockFile, resolve_lockfile_path_for_read

    return LockFile.read(resolve_lockfile_path_for_read(root, read_only=True))


def record_unpacked_files(output_dir: Path, files: Sequence[str], logger: Any) -> None:
    """Record the files ``apm unpack`` wrote, or announce that they are untracked.

    The record goes through the shared local-bundle lockfile owner, so audit
    checks the files and a later install reconciles them. A file the lockfile
    already records with the same content hash stays with its recorded owner;
    when no file is left to record, the lockfile is not touched (#4556).
    """
    root = unpack_record_root(output_dir)
    if root is None:
        logger.info(
            "unpack output is not tracked: the output directory has no apm.yml. "
            "Unpack into an APM project to record the deployed files."
        )
        return
    if not files:
        return
    from ..core.deployment_ledger import DeploymentLedgerCodec
    from ..core.scope import InstallScope
    from ..deps.lockfile import LockFile, resolve_lockfile_path_for_read
    from ..utils.content_hash import compute_file_hash

    lockfile = _recorded_lockfile(root)
    claimed = DeploymentLedgerCodec.deployed_file_hashes(lockfile) if lockfile else {}
    hashes = {
        rel: digest
        for rel in files
        if claimed.get(rel) != (digest := compute_file_hash(root / rel))
    }
    if not hashes:
        return
    # Only a write migrates a legacy apm.lock; a no-op restore leaves it as is.
    lockfile_path = resolve_lockfile_path_for_read(root, read_only=False)
    lockfile = lockfile or LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile, list(hashes), hashes, scope=InstallScope.PROJECT
    )
    lockfile.write(lockfile_path)


def plugin_bundle_integrity(bundle_info: Any, logger: Any) -> IntegrityCheck:
    """Integrity check for a plugin bundle: its ``pack.bundle_files`` manifest."""

    def check(require_hashes: bool) -> None:
        from ..bundle.local_bundle import verify_bundle_integrity

        if bundle_info.lockfile is None:
            if require_hashes:
                from .errors import InstallFailureAlreadyRendered

                logger.error(
                    "Bundle has no apm.lock.yaml, but org policy requires "
                    "integrity hashes. Repack it with the current APM version "
                    "and retry."
                )
                raise InstallFailureAlreadyRendered(
                    "Org policy requires integrity hashes for this bundle"
                )
            logger.warning(
                "Bundle has no apm.lock.yaml -- skipping integrity check. "
                "It was produced by an older APM version or by another tool; "
                "repack it with 'apm pack' to embed an integrity manifest."
            )
            return
        errors = verify_bundle_integrity(bundle_info.source_dir, bundle_info.lockfile)
        if errors:
            from ..core.remediation import BUNDLE_INTEGRITY_FAILED
            from .errors import InstallFailureAlreadyRendered

            # One refusal, one stream: headline, reasons and remedy all on stderr.
            logger.refusal(
                "\n".join(
                    (
                        "Bundle integrity check failed:",
                        *(f"  {err}" for err in errors),
                        f"The bundle changed after it was packed. "
                        f"{BUNDLE_INTEGRITY_FAILED.sentence()}",
                    )
                )
            )
            raise InstallFailureAlreadyRendered("Bundle integrity check failed")
        logger.verbose_detail("Bundle integrity verified")

    return check


def apm_bundle_integrity(
    source_dir: Path, lockfile: LockFile, files: Sequence[str], logger: Any
) -> IntegrityCheck:
    """Integrity check for a ``--format apm`` bundle: its per-file lock hashes.

    *files* are the regular files the bundle would write. A file whose bytes
    differ from its locked hash, or that has no locked hash, is refused when
    policy requires hashes; otherwise an altered file is a warning.
    """

    def check(require_hashes: bool) -> None:
        locked: dict[str, str] = {}
        for dep in lockfile.get_all_dependencies():
            locked.update(dep.deployed_file_hashes)
        locked.update(lockfile.local_deployed_file_hashes)
        altered, unhashed = _unverified_files(source_dir, files, locked)
        if require_hashes and (altered or unhashed):
            details = [f"Hash mismatch: {rel}" for rel in altered]
            details += [f"No locked hash: {rel}" for rel in unhashed]
            raise ValueError(
                "Org policy requires integrity hashes, and the bundle failed its "
                "integrity check:\n" + "\n".join(f"  - {line}" for line in details)
            )
        for rel in altered:
            logger.warning(f"Bundle file differs from its locked hash: {rel}")

    return check


def _unverified_files(
    source_dir: Path, files: Sequence[str], locked: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    from ..utils.content_hash import compute_file_hash

    altered: list[str] = []
    unhashed: list[str] = []
    for rel in files:
        path = source_dir / rel
        if not path.is_file() or path.is_symlink():
            continue
        expected = locked.get(rel)
        if not expected:
            unhashed.append(rel)
        elif compute_file_hash(path) != expected:
            altered.append(rel)
    return altered, unhashed
