"""Current-intent audit resolution is read-only and scope-aware."""

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.install.audit_target_roots import AuditTargetError, resolve_audit_targets
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = pytest.mark.component


@pytest.mark.parametrize("user_scope", [False, True])
def test_configured_experimental_target_is_read_only(tmp_path: Path, user_scope: bool) -> None:
    """Resolve configured cloud profiles without materializing any target root."""
    project = tmp_path / "project"
    project.mkdir()
    before = ArtifactSnapshot.capture(tmp_path)
    with (
        patch("apm_cli.config.get_install_target", return_value="grok-cloud") as config,
        patch("apm_cli.core.experimental.is_enabled", return_value=True) as enabled,
        patch.object(Path, "home", return_value=tmp_path / "home"),
    ):
        profiles = resolve_audit_targets(project, user_scope=user_scope)
    assert tuple(profile.name for profile in profiles) == ("grok-cloud",)
    assert profiles[0].root_dir == ".grok"
    config.assert_called_once_with(create_config=False)
    enabled.assert_called_once_with("grok_cloud", create_config=False)
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))


@pytest.mark.parametrize(
    "manifest",
    [
        "targets: [grok-cloud]\n",
        "targets: []\n",
        "target: null\n",
        "targets: [claude]\ntarget: copilot\n",
        "- not-a-mapping\n",
        "targets: [\n",
    ],
)
def test_bad_manifest_never_falls_through_to_config(tmp_path: Path, manifest: str) -> None:
    """Invalid declarations cannot silently widen to configuration or detection."""
    (tmp_path / "apm.yml").write_text(manifest, encoding="utf-8")
    with (
        patch("apm_cli.config.get_install_target", return_value="claude") as config,
        pytest.raises(AuditTargetError),
    ):
        resolve_audit_targets(tmp_path)
    config.assert_not_called()


def test_absent_config_retains_legacy_audit_detection(tmp_path: Path) -> None:
    """Do not import install-v2 ambiguity errors into existing audit fallback."""
    (tmp_path / ".github").mkdir()
    (tmp_path / ".claude").mkdir()
    with (
        patch("apm_cli.config.get_install_target", return_value=None),
        patch("apm_cli.core.experimental.is_enabled", return_value=False),
    ):
        targets = resolve_audit_targets(tmp_path)
    assert {target.name for target in targets} == {"copilot", "claude"}


def test_disabled_configured_target_fails_instead_of_empty_success(tmp_path: Path) -> None:
    """Explicit current intent must be eligible for the requested scope."""
    with (
        patch("apm_cli.config.get_install_target", return_value="grok-cloud"),
        patch("apm_cli.core.experimental.is_enabled", return_value=False),
        pytest.raises(AuditTargetError, match="Cannot audit selected target"),
    ):
        resolve_audit_targets(tmp_path)


@pytest.mark.parametrize("configured", ["intellij", "vscode", "agents"])
def test_runtime_alias_uses_canonical_primitive_profile(tmp_path: Path, configured: str) -> None:
    """Use the owner's primitive projection, not an independent alias mapping."""
    with patch("apm_cli.config.get_install_target", return_value=configured):
        targets = resolve_audit_targets(tmp_path)
    assert tuple(target.name for target in targets) == ("copilot",)


def test_missing_config_is_not_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real config reader must not initialize HOME during target discovery."""
    from apm_cli import config

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(config, "_config_cache", None)
    before = ArtifactSnapshot.capture(home)
    assert resolve_audit_targets(tmp_path)[0].name == "copilot"
    assert_unchanged(before, ArtifactSnapshot.capture(home))


@pytest.mark.parametrize("claim_kind", ["file", "directory", "replaced-file"])
def test_removed_target_claims_remain_in_comparison(tmp_path: Path, claim_kind: str) -> None:
    """Legacy directory ownership also preserves old-target drift coverage."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.install.drift import DriftFinding, diff_scratch_against_project
    from apm_cli.integration.targets import KNOWN_TARGETS

    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    relative = ".claude/skills/old/SKILL.md"
    deployed = project / relative
    deployed.parent.mkdir(parents=True)
    deployed.write_text("Previously deployed skill\n", encoding="utf-8")
    claim = relative if claim_kind == "file" else ".claude/skills/old"
    hashes = {claim: "sha256:" + "a" * 64} if claim_kind == "replaced-file" else {}
    lock = LockFile(
        dependencies={
            "owner/pkg": LockedDependency(
                repo_url="owner/pkg", deployed_files=[claim], deployed_file_hashes=hashes
            )
        }
    )
    findings = diff_scratch_against_project(scratch, project, lock, [KNOWN_TARGETS["grok-cloud"]])
    assert findings == (
        []
        if claim_kind == "replaced-file"
        else [DriftFinding(path=relative, kind="orphaned", package="owner/pkg")]
    )
