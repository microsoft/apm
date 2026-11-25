"""The identities a local bundle presents to org dependency policy, and how they are judged."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.core.command_logger import InstallLogger
from apm_cli.core.remediation import BUNDLE_PACKAGES_UNRECORDED
from apm_cli.core.target_detection import EffectiveTargetDecision
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.bundle_admission import admit_bundle, bundle_policy_subjects
from apm_cli.install.errors import PolicyViolationError
from apm_cli.install.local_bundle_pending import BundleRecordStage, recorded_bundle
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-violation-subjects")]


def _bundle(root: Path, lock: str | None) -> SimpleNamespace:
    root.mkdir()
    if lock is not None:
        (root / "apm.lock.yaml").write_text(lock, encoding="utf-8")
    return SimpleNamespace(
        source_dir=root, package_id="tool-id", plugin_json={"name": "tool"}, lockfile=None
    )


@pytest.mark.parametrize(
    "lock",
    ["dependencies: [unclosed\n", "lockfile_version: '1'\ndependencies: not-a-list\n"],
)
def test_unreadable_bundle_lockfile_refuses_the_bundle(tmp_path: Path, lock: str) -> None:
    bundle = _bundle(tmp_path / "b", lock)

    with pytest.raises(ValueError, match=r"apm\.lock\.yaml"):
        bundle_policy_subjects(bundle)


# -- dependencies.allow for a local bundle (#4646) -----------------------------
# Ruling: allow matches only the packages the bundle's embedded lockfile
# records, never the author-chosen ``_local/<name>``; under an allow list a
# bundle that records no package is refused; deny matches name and packages.


def _bundle_with_packages(root: Path, name: str, packages: list[str] | None) -> SimpleNamespace:
    root.mkdir()
    if packages is not None:
        lock = LockFile()
        for repo in packages:
            lock.add_dependency(LockedDependency(repo_url=repo, resolved_commit="a" * 40))
        (root / "apm.lock.yaml").write_text(lock.to_yaml(), encoding="utf-8")
    return SimpleNamespace(source_dir=root, package_id=name, plugin_json={"name": name})


def test_pending_record_preserves_bundle_policy_subjects(
    tmp_path: Path,
) -> None:
    bundle = _bundle_with_packages(tmp_path / "bundle", "tool", ["acme/lib"])
    subjects = bundle_policy_subjects(bundle)
    scope = tmp_path / "scope"
    key = "tool#1.0@sha256:abc123"

    BundleRecordStage(scope, "tool", key, ("hooks",), subjects).commit()

    record_path = scope / "apm_modules" / ".local-bundle-pending.json"
    assert record_path.is_file()
    assert (
        json.loads(record_path.read_text(encoding="utf-8"))["bundles"]["tool"]["policy_subjects"][
            "packages"
        ][0]["canonical_ref"]
        == subjects.packages[0].get_canonical_dependency_string()
    )
    stored = recorded_bundle(scope, key)
    assert stored is not None
    assert stored.policy_subjects is not None
    assert [
        subject.get_canonical_dependency_string() for subject in stored.policy_subjects.packages
    ] == [subject.get_canonical_dependency_string() for subject in subjects.packages]


def _admit(bundle: SimpleNamespace, project: Path, **dependencies: object) -> None:
    policy = ApmPolicy(enforcement="block", dependencies=DependencyPolicy(**dependencies))
    fetch = PolicyFetchResult(policy=policy, source="org:acme/.github", outcome="found")
    project.mkdir(exist_ok=True)
    admit_bundle(
        policy_root=project,
        apm_deps=(),
        mcp_deps=(),
        target_decision=EffectiveTargetDecision(None, "test"),
        integrity_check=lambda _require_hashes: None,
        no_policy=False,
        logger=InstallLogger(),
        dry_run=False,
        fetch_result=fetch,
        bundled_deps=bundle_policy_subjects(bundle),
    )


def _violations(capsys: pytest.CaptureFixture[str]) -> list[str]:
    captured = capsys.readouterr()
    return re.findall(r"Policy violation: (.+)", f"{captured.out}\n{captured.err}")


def _refused(
    capsys: pytest.CaptureFixture[str], bundle: SimpleNamespace, project: Path, **deps: object
) -> list[str]:
    with pytest.raises(PolicyViolationError):
        _admit(bundle, project, **deps)
    return _violations(capsys)


def test_bundle_whose_packages_the_allow_list_names_is_admitted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle_with_packages(tmp_path / "b", "tool", ["acme/lib", "acme/other"])

    _admit(bundle, tmp_path / "p", allow=("acme/**",))

    assert _violations(capsys) == []


def test_bundle_package_outside_the_allow_list_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle_with_packages(tmp_path / "b", "tool", ["acme/lib", "evil/pkg"])

    refused = _refused(capsys, bundle, tmp_path / "p", allow=("acme/**",))

    assert refused == ["github.com/evil/pkg -- not in allowed sources"]


@pytest.mark.parametrize("packages", [[], None], ids=["empty-lockfile", "no-lockfile"])
def test_bundle_recording_no_package_is_refused_under_an_allow_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], packages: list[str] | None
) -> None:
    # The allow list names the bundle; its name still never admits it.
    bundle = _bundle_with_packages(tmp_path / "b", "tool", packages)

    with pytest.raises(PolicyViolationError):
        _admit(bundle, tmp_path / "p", allow=("**/tool", "acme/**"))

    captured = capsys.readouterr()
    text = " ".join(f"{captured.out} {captured.err}".split())
    assert (
        "Policy violation: _local/tool -- the bundle records no packages for "
        "dependencies.allow to match, and a bundle name never satisfies the allow list. "
        f"{BUNDLE_PACKAGES_UNRECORDED.sentence()}"
    ) in text, text
    assert text.count("Policy violation:") == 1, text


def test_bundle_named_like_an_allowed_package_is_judged_by_its_packages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle_with_packages(tmp_path / "b", "lib", ["evil/pkg"])

    refused = _refused(capsys, bundle, tmp_path / "p", allow=("**/lib", "acme/lib"))

    assert refused == ["github.com/evil/pkg -- not in allowed sources"]


def test_deny_still_matches_the_bundle_name_under_an_allow_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle_with_packages(tmp_path / "b", "tool", ["acme/lib"])

    refused = _refused(capsys, bundle, tmp_path / "p", allow=("acme/**",), deny=("**/tool",))

    assert refused == ["_local/tool -- denied by pattern: **/tool"]


def test_bundle_recording_no_package_is_admitted_without_an_allow_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle_with_packages(tmp_path / "b", "tool", [])

    _admit(bundle, tmp_path / "p", deny=("evil/**",))

    assert _violations(capsys) == []
