"""Codex admission consumes canonical format, target, and executable decisions."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.codex_plugins.admission import (
    CodexNativeDeployment,
    CodexNativePlugin,
    finalize_codex_native,
    plan_native_codex_plugin,
)
from apm_cli.codex_plugins.client import CodexNativeError
from apm_cli.codex_plugins.manifest import CodexCompatibilityPlugin
from apm_cli.core.scope import InstallScope
from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.utils.content_hash import compute_native_bundle_hash

pytestmark = pytest.mark.component


def _admit(root: Path, **overrides):
    package = SimpleNamespace(
        codex_plugin=CodexCompatibilityPlugin("example", "1", True, False, ("server",))
    )
    info = SimpleNamespace(package=package, install_path=root)
    options = {
        "scope": InstallScope.USER,
        "skill_subset": None,
        "approvals": {"mcp": True, "hooks": False, "bin": False, "canvas": False, "lsp": False},
        "ctx": None,
    }
    options.update(overrides)
    return plan_native_codex_plugin(
        info, "owner/example", [SimpleNamespace(name="codex")], **options
    )


def test_native_admission_never_silently_reduces_unapproved_mcp(tmp_path):
    with pytest.raises(CodexNativeError, match="allowExecutables"):
        _admit(tmp_path, approvals={})


def test_native_admission_requires_whole_bundle_and_user_scope(tmp_path):
    with pytest.raises(CodexNativeError, match="complete"):
        _admit(tmp_path, skill_subset=("example",))
    with pytest.raises(CodexNativeError, match="user scope"):
        _admit(tmp_path, scope=InstallScope.PROJECT)


def test_native_admission_gates_hidden_native_root_executables(tmp_path):
    (tmp_path / "hooks").mkdir()
    with pytest.raises(CodexNativeError, match="allowExecutables"):
        _admit(tmp_path)


def test_native_admission_keeps_canonical_identity(tmp_path):
    plan = _admit(tmp_path)
    assert plan.dependency_key == "owner/example"
    assert plan.source_root == tmp_path.resolve()
    assert plan.name == "example"
    assert plan.target_name == "codex"
    assert plan.source_hash == compute_native_bundle_hash(tmp_path)
    assert plan.mcp_server_names == ("server",)
    assert _admit(tmp_path, skill_subset=()) == plan


@pytest.mark.parametrize(
    "fault",
    [
        "cache-directory-bytes",
        pytest.param(
            "executable-mode",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode"),
        ),
    ],
)
def test_native_admission_fingerprints_every_publisher_asset(tmp_path: Path, fault: str) -> None:
    """Native identity includes publisher cache-named assets and executable mode."""
    asset = tmp_path / "resources/__pycache__/asset.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"publisher bytes")
    asset.chmod(0o644)
    original = _admit(tmp_path).source_hash
    if fault == "executable-mode":
        asset.chmod(0o755)
    else:
        asset.write_bytes(b"changed publisher bytes")
    assert _admit(tmp_path).source_hash != original


def test_read_only_projection_does_not_qualify_or_start_native_client(tmp_path, monkeypatch):
    from apm_cli.codex_plugins.client import CodexNativeClient

    def forbidden(*args):
        raise AssertionError("read-only replay must not qualify or launch a native runtime")

    monkeypatch.setattr(CodexNativeClient, "require_scope", forbidden)
    monkeypatch.setattr(CodexNativeClient, "connect", forbidden)
    assert _admit(tmp_path, read_only=True, scope=None).name == "example"


def test_non_codex_package_has_no_native_admission(tmp_path):
    info = SimpleNamespace(package=SimpleNamespace(), install_path=tmp_path)
    assert (
        plan_native_codex_plugin(
            info,
            "owner/example",
            [],
            scope=InstallScope.PROJECT,
            skill_subset=None,
            approvals={},
            ctx=None,
        )
        is None
    )


def _make_plugin(root: Path) -> CodexNativePlugin:
    return CodexNativePlugin("owner/example", "example", root, compute_native_bundle_hash(root))


def _make_source_plan(root: Path) -> DeployableSourcePlan:
    return DeployableSourcePlan(source_root=root, paths=frozenset())


def _base_result():
    return {"native_plugin": False, "deployed_files": []}


def test_finalize_cleanup_error_raises_and_does_not_queue(tmp_path):
    """Reconcile errors must raise before any deployment is queued."""
    plugin = _make_plugin(tmp_path)
    ctx = SimpleNamespace(codex_native_plugins={}, dry_run=False)
    reconcile_result = {"errors": 1, "failed_targets": ["codex"]}
    with pytest.raises(CodexNativeError, match="loose hooks"):
        finalize_codex_native(
            _base_result(), plugin, _make_source_plan(tmp_path), reconcile_result, ctx, None
        )
    assert "owner/example" not in ctx.codex_native_plugins


def test_finalize_read_only_does_not_queue(tmp_path):
    """Scratch/replay (scratch_root is not None) must never queue."""
    plugin = _make_plugin(tmp_path)
    ctx = SimpleNamespace(codex_native_plugins={}, dry_run=False)
    result = finalize_codex_native(
        _base_result(), plugin, _make_source_plan(tmp_path), None, ctx, tmp_path
    )
    assert result["native_plugin"] is True
    assert "owner/example" not in ctx.codex_native_plugins


def test_finalize_dry_run_does_not_queue(tmp_path):
    """Dry-run installs must not queue native deployments."""
    plugin = _make_plugin(tmp_path)
    ctx = SimpleNamespace(codex_native_plugins={}, dry_run=True)
    result = finalize_codex_native(
        _base_result(), plugin, _make_source_plan(tmp_path), None, ctx, None
    )
    assert result["native_plugin"] is True
    assert "owner/example" not in ctx.codex_native_plugins


def test_finalize_queues_immutable_deployment(tmp_path):
    """Real install queues a frozen CodexNativeDeployment on ctx."""
    plugin = _make_plugin(tmp_path)
    plan = _make_source_plan(tmp_path)
    (tmp_path / "changed-after-admission.txt").write_text("Must not replace the admitted hash.")
    ctx = SimpleNamespace(codex_native_plugins={}, dry_run=False)
    result = finalize_codex_native(_base_result(), plugin, plan, None, ctx, None)
    assert result["native_plugin"] is True
    dep = ctx.codex_native_plugins["owner/example"]
    assert isinstance(dep, CodexNativeDeployment)
    assert dep.plugin is plugin
    assert dep.source_plan is plan
    assert dep.source_hash == plugin.source_hash
    assert dep.source_hash != compute_native_bundle_hash(tmp_path)


def test_final_native_phase_refuses_unsaved_candidate_before_native_writes(tmp_path, monkeypatch):
    """A scanned plan cannot register unless the saved lockfile still admits it."""
    from apm_cli.install.phases import codex_plugins

    def forbidden(*args, **kwargs):
        raise AssertionError("unretained candidate must not reach native synchronization")

    monkeypatch.setattr(codex_plugins, "synchronize_codex_plugins", forbidden)
    ctx = SimpleNamespace(
        dry_run=False,
        lockfile_only=False,
        apm_modules_dir=tmp_path / "apm_modules",
        apm_dir=tmp_path,
        codex_native_plugins={"owner/example": object()},
    )
    with pytest.raises(CodexNativeError, match="saved executable/ownership state"):
        codex_plugins.run(ctx)


def test_final_native_phase_refuses_retained_loose_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup-retained user edits must not coexist with a new native registration."""
    from apm_cli.install.phases import codex_plugins

    loose = tmp_path / ".codex/skills/example/SKILL.md"
    loose.parent.mkdir(parents=True)
    loose.write_text("User edited the old loose installation")
    original = loose.read_bytes()
    monkeypatch.setattr(
        codex_plugins,
        "candidates_from_lockfile",
        lambda *args: [SimpleNamespace(dependency_key="owner/example")],
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("retained loose deployment reached native synchronization")

    monkeypatch.setattr(codex_plugins, "synchronize_codex_plugins", forbidden)
    ctx = SimpleNamespace(
        dry_run=False,
        lockfile_only=False,
        apm_modules_dir=tmp_path / "apm_modules",
        apm_dir=tmp_path,
        scope=InstallScope.USER,
        codex_native_plugins={"owner/example": object()},
        package_deployed_files={"owner/example": [".codex/skills/example/SKILL.md"]},
    )
    with pytest.raises(CodexNativeError, match="retained loose") as error:
        codex_plugins.run(ctx)
    assert "owner/example" in str(error.value)
    assert ".codex/skills/example/SKILL.md" in str(error.value)
    assert "apm install -g --target codex" in str(error.value)
    assert loose.read_bytes() == original
