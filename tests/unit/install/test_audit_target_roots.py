"""Current-intent audit resolution is read-only and scope-aware."""

import json
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
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
    config.assert_called_once_with(create_config=False, strict=True)
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
    monkeypatch.setattr(config, "CONFIG_DIR", str(home / ".apm"))
    monkeypatch.setattr(config, "CONFIG_FILE", str(home / ".apm/config.json"))
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


@pytest.mark.parametrize("work", ["owner-probes", "enumeration"])
def test_directory_claim_work_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: str
) -> None:
    """Fixed-depth 50/500 claims must not multiply probes or repeat safe walks."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.install import drift

    measurements = []
    original_rglob = Path.rglob
    for size in (50, 500):
        project = tmp_path / str(size) / "project"
        scratch = tmp_path / str(size) / "scratch"
        scratch.mkdir(parents=True)
        dependencies = {}
        expected = []
        for index in range(size):
            # Half already lie under a governed root; the rest need additional
            # comparison roots. Deep claims deliberately precede their parents.
            top = ".apm" if index % 2 == 0 else "retired"
            parent = f"{top}/bundle-{index:04d}"
            deep = f"{parent}/deep"
            exact = f"{deep}/exact.md"
            for claim, owner in (
                (deep + "/", f"deep/pkg-{index}"),
                (parent, f"parent/pkg-{index}"),
                (exact, f"exact/pkg-{index}"),
            ):
                dependencies[owner] = LockedDependency(repo_url=owner, deployed_files=[claim])
            for relative, owner in (
                (f"{parent}/outer.md", f"parent/pkg-{index}"),
                (f"{deep}/orphan.md", f"deep/pkg-{index}"),
                (exact, f"exact/pkg-{index}"),
                # Similar spelling must not count as ancestor ownership.
                (f".apm/bundle-{index:04d}-neighbor/note.md", None),
                (f".apm/file-{index:04d}.md/neighbor.md", None),
            ):
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("unchanged comparison-only bytes\n", encoding="utf-8")
                if owner is not None:
                    expected.append(drift.DriftFinding(relative, "orphaned", owner))
            file_claim = f".apm/file-{index:04d}.md"
            owner = f"hashed/pkg-{index}"
            dependencies[owner] = LockedDependency(
                repo_url=owner,
                deployed_files=[file_claim],
                deployed_file_hashes={file_claim: "sha256:" + "a" * 64},
            )

        enumerated: Counter[Path] = Counter()
        probes = 0

        def counted_rglob(
            path: Path, pattern: str, counts: Counter[Path] = enumerated
        ) -> Iterator[Path]:
            """Count real yielded filesystem entries, not elapsed time."""
            for entry in original_rglob(path, pattern):
                counts[entry] += 1
                yield entry

        def count_owner_probes(frame: FrameType, event: str, arg: object) -> None:
            """Count old prefix comparisons and replacement dictionary probes."""
            nonlocal probes
            if event != "c_call" or frame.f_code.co_filename != drift.__file__:
                return
            name = getattr(arg, "__name__", "")
            if name == "startswith" or (
                name == "get"
                and getattr(arg, "__self__", None) is frame.f_locals.get("prefix_owners")
            ):
                probes += 1

        before = ArtifactSnapshot.capture(project)
        previous_profile = sys.getprofile()
        with monkeypatch.context() as measured:
            measured.setattr(Path, "rglob", counted_rglob)
            try:
                sys.setprofile(count_owner_probes)
                findings = drift.diff_scratch_against_project(
                    scratch, project, LockFile(dependencies=dependencies), targets=[]
                )
            finally:
                sys.setprofile(previous_profile)
        assert findings == sorted(expected, key=lambda finding: finding.path)
        assert_unchanged(before, ArtifactSnapshot.capture(project))
        assert list(scratch.iterdir()) == [], "Comparison claims must not authorize replay"
        measurements.append(
            {
                "size": size,
                "owner-probes": probes,
                "enumeration": enumerated.total(),
                "max-visits": max(enumerated.values()),
            }
        )

    print(json.dumps({"work": work, "measurements": measurements}, sort_keys=True))
    small, large = measurements
    assert small[work] > 0, "The operation counter must observe the real comparison path"
    assert large[work] < 15 * small[work], f"{work} grew at least 15x: {measurements}"
    if work == "enumeration":
        assert [sample["max-visits"] for sample in measurements] == [1, 1], (
            f"Validated covered roots were walked again: {measurements}"
        )


@pytest.mark.parametrize("unsafe", ["symlink", "escape"])
def test_additional_directory_claims_keep_path_refusals(tmp_path: Path, unsafe: str) -> None:
    """An unsafe old-target root is never eligible for a covered-root shortcut."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.install.drift import diff_scratch_against_project
    from apm_cli.utils.path_security import PathTraversalError

    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    outside = tmp_path / "outside"
    for root in (project, scratch, outside):
        root.mkdir()
    (outside / "note.md").write_text("not managed\n", encoding="utf-8")
    if unsafe == "symlink":
        (project / "retired").symlink_to(outside, target_is_directory=True)
        claim = "retired"
    else:
        claim = "../outside"
    lock = LockFile(
        dependencies={"owner/pkg": LockedDependency(repo_url="owner/pkg", deployed_files=[claim])}
    )
    before = ArtifactSnapshot.capture(tmp_path)
    if unsafe == "escape":
        with pytest.raises(PathTraversalError):
            diff_scratch_against_project(scratch, project, lock, targets=[])
    else:
        assert diff_scratch_against_project(scratch, project, lock, targets=[]) == []
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))


@pytest.mark.parametrize("placement", ["scratch-only", "project-only", "both"])
def test_local_bundle_exclusion_preserves_other_claims(tmp_path: Path, placement: str) -> None:
    """Exclude imperative bundles without suppressing authored orphan findings."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.install.drift import DriftFinding, diff_scratch_against_project
    from apm_cli.integration.targets import KNOWN_TARGETS

    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    bundled = ".claude/skills/bundled/SKILL.md"
    authored = ".claude/skills/authored/SKILL.md"
    for name, root in (("project", project), ("scratch", scratch)):
        root.mkdir()
        if placement in (f"{name}-only", "both"):
            path = root / bundled
            path.parent.mkdir(parents=True)
            path.write_text(name, encoding="utf-8")
    authored_path = project / authored
    authored_path.parent.mkdir(parents=True)
    authored_path.write_text("authored orphan\n", encoding="utf-8")
    lock = LockFile(
        dependencies={
            "owner/pkg": LockedDependency(repo_url="owner/pkg", deployed_files=[authored])
        }
    )
    DeploymentLedgerCodec.record_local_bundle_files(
        lock, [bundled], {bundled: f"sha256:{'b' * 64}"}
    )
    before = ArtifactSnapshot.capture(tmp_path)
    assert diff_scratch_against_project(scratch, project, lock, [KNOWN_TARGETS["claude"]]) == [
        DriftFinding(path=authored, kind="orphaned", package="owner/pkg")
    ]
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))
