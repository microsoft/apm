"""Hook-only candidates must pass canonical validation before activation."""

from pathlib import Path
from threading import Lock
from unittest.mock import Mock

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver, DownloadedPackageError
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.drift import should_force_ref_recheck
from apm_cli.install.context import InstallContext
from apm_cli.install.phases.integrate import _resolve_download_strategy
from apm_cli.install.phases.resolve import _activate_validated_candidate
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.install.sources import make_dependency_source
from apm_cli.models.apm_package import GitReferenceType, ResolvedReference
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.validation import PackageType, validate_apm_package
from apm_cli.utils.content_hash import compute_package_hash

pytestmark = pytest.mark.component

_VALID_HOOK = '{"hooks":{"preToolUse":[{"bash":"echo fixture"}]}}'


@pytest.mark.parametrize("layout", ["hooks", ".apm/hooks"])
def test_hook_package_uses_validated_metadata_before_activation(
    tmp_path: Path, layout: str
) -> None:
    candidate = tmp_path / "candidate"
    hooks = candidate / layout
    hooks.mkdir(parents=True)
    (hooks / "lint.json").write_text(_VALID_HOOK, encoding="utf-8")
    ref = DependencyReference(repo_url="acme/hooks", virtual_path="lint", is_virtual=True)
    validation = validate_apm_package(candidate)
    assert validation.is_valid
    assert validation.package_type is PackageType.HOOK_PACKAGE
    activation = Mock(return_value=candidate)
    resolver = APMDependencyResolver(
        apm_modules_dir=tmp_path / "modules",
        download_callback=Mock(return_value=candidate),
        activation_callback=activation,
    )

    package = resolver._try_load_dependency_package(ref)

    assert package is not None
    assert package.description == validation.package.description
    assert package.source == ref.repo_url
    assert package.package_path == candidate
    assert package.source_path == candidate
    activation.assert_called_once_with(candidate)
    assert not (candidate / "apm.yml").exists()
    assert not (candidate / "SKILL.md").exists()


@pytest.mark.parametrize("layout", ["hooks", ".apm/hooks"])
@pytest.mark.parametrize(
    "content",
    ["{", "[]", '{"hooks":[]}', '{"hooks":{"preToolUse":[{"bash":42}]}}'],
)
def test_malformed_hook_candidate_never_activates(
    tmp_path: Path, layout: str, content: str
) -> None:
    candidate = tmp_path / "candidate"
    hooks = candidate / layout
    hooks.mkdir(parents=True)
    (hooks / "lint.json").write_text(content, encoding="utf-8")
    validation = validate_apm_package(candidate)
    assert not validation.is_valid
    assert validation.package is None
    assert "lint.json" in "; ".join(validation.errors)
    activation = Mock()
    resolver = APMDependencyResolver(
        apm_modules_dir=tmp_path / "modules",
        download_callback=Mock(return_value=candidate),
        activation_callback=activation,
    )

    with pytest.raises(DownloadedPackageError, match=r"lint\.json"):
        resolver._try_load_dependency_package(DependencyReference(repo_url="acme/hooks"))

    activation.assert_not_called()


@pytest.mark.parametrize("layout", ["hooks", ".apm/hooks"])
def test_hook_updates_reenter_staged_admission(tmp_path: Path, layout: str) -> None:
    hooks = tmp_path / layout
    hooks.mkdir(parents=True)
    (hooks / "lint.json").write_text(_VALID_HOOK, encoding="utf-8")
    ref = DependencyReference(repo_url="acme/hooks", reference="main")
    locked = LockedDependency(repo_url="acme/hooks", resolved_ref="main", resolved_commit="a" * 40)

    assert should_force_ref_recheck(ref, locked, update_refs=True, package_path=tmp_path)
    assert not should_force_ref_recheck(ref, locked, update_refs=False, package_path=tmp_path)
    assert not should_force_ref_recheck(
        ref, locked, update_refs=True, package_path=tmp_path, update_keys={"other/package"}
    )
    assert should_force_ref_recheck(
        ref, locked, update_refs=True, package_path=tmp_path, update_keys={ref.get_unique_key()}
    )

    (tmp_path / "SKILL.md").write_text("---\nname: control\n---\n# Skill\n", encoding="utf-8")
    assert not should_force_ref_recheck(ref, locked, update_refs=True, package_path=tmp_path)


@pytest.mark.parametrize("evidence", ["valid", "missing", "wrong-path", "tampered", "stale-sha"])
@pytest.mark.parametrize("mode", ["update", "refresh", "install"])
def test_published_hook_reuse_requires_bound_integrity(
    tmp_path: Path, evidence: str, mode: str
) -> None:
    ref = DependencyReference(repo_url="acme/hooks", reference="main")
    key = ref.get_unique_key()
    modules = tmp_path / "apm_modules"
    installed = ref.get_install_path(modules)
    hooks = installed / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "lint.json").write_text(_VALID_HOOK, encoding="utf-8")
    locked = LockedDependency(
        repo_url=ref.repo_url,
        resolved_ref="main",
        resolved_commit="a" * 40,
        content_hash=compute_package_hash(installed),
    )
    ctx = InstallContext(
        project_root=tmp_path,
        apm_dir=tmp_path,
        update_refs=mode == "update",
        refresh=mode == "refresh",
    )
    ctx.apm_modules_dir = modules
    ctx.existing_lockfile = LockFile()
    ctx.existing_lockfile.add_dependency(locked)
    ctx.targets = ["claude"]
    ctx.logger = Mock()
    ctx.diagnostics = Mock()
    ctx.downloader = Mock()
    ctx.dependency_graph = Mock()
    ctx.dependency_graph.dependency_tree.get_node.return_value = None
    ctx.downloader.resolve_git_reference.return_value = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="c" * 40 if evidence == "stale-sha" else "b" * 40,
        ref_name="main",
    )
    session = ResolutionStagingSession(modules)
    candidate = session.prepare_replacement(installed)
    (candidate / "hooks").mkdir(parents=True)
    (candidate / "hooks/lint.json").write_text(
        _VALID_HOOK.replace("fixture", "updated"), encoding="utf-8"
    )
    assert validate_apm_package(candidate).is_valid
    _activate_validated_candidate(
        candidate,
        staging_session=session,
        pending_downloads={candidate: (key, "b" * 40)},
        callback_downloaded=ctx.callback_downloaded,
        callback_content_hashes=ctx.callback_content_hashes,
        callback_lock=Lock(),
        tui=None,
    )
    if evidence == "missing":
        ctx.callback_content_hashes.clear()
    elif evidence == "wrong-path":
        ctx.callback_content_hashes[key] = (tmp_path, ctx.callback_content_hashes[key][1])
    elif evidence == "tampered":
        (installed / "hooks/lint.json").write_text(_VALID_HOOK + "\n", encoding="utf-8")

    resolved, skip, locked_check, changed = _resolve_download_strategy(ctx, ref, installed)

    # Ordinary installs check the locked hash, not the fresh callback receipt.
    assert skip is (evidence == "valid" and mode != "install")
    if skip:
        source = make_dependency_source(
            ctx,
            ref,
            installed,
            key,
            resolved_ref=resolved,
            dep_locked_chk=locked_check,
            ref_changed=changed,
            skip_download=skip,
            fetched_this_run=True,
        )
        assert source.acquire() is not None
        ctx.downloader.download_package.assert_not_called()
        ctx.logger.download_complete.assert_called_once_with(
            ref.repo_url, ref="main", sha="bbbbbbbb", cached=False
        )
        ctx.diagnostics.warn.assert_not_called()
    assert session.rollback() == []
    assert compute_package_hash(installed) == locked.content_hash
