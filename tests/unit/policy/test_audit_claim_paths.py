"""Historical audit claims retain canonical path containment."""

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.targets import (
    KNOWN_TARGETS,
    AuditTargetDisabledError,
    resolve_audit_targets,
)
from apm_cli.policy.ci_checks import _resolve_deployed_file_path, run_baseline_checks

pytestmark = pytest.mark.component


def test_disabled_audit_target_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "apm.yml").write_text("target: copilot\n", encoding="ascii")
    monkeypatch.setattr(
        "apm_cli.core.target_detection.read_manifest_target", lambda _: ["grok-cloud"]
    )
    monkeypatch.setattr("apm_cli.integration.targets._flag_gated", lambda *a, **k: False)
    with pytest.raises(AuditTargetDisabledError, match="apm experimental enable grok-cloud") as exc:
        resolve_audit_targets(tmp_path, lockfile=LockFile())
    assert exc.value.exit_code == 3


def test_baseline_shares_one_lockfile_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "apm.yml").write_text(
        "name: example\nversion: 1.0.0\ndependencies:\n  apm:\n    - example/package\n",
        encoding="ascii",
    )
    lock_path = tmp_path / "apm.lock.yaml"
    LockFile().write(lock_path)
    read = LockFile.read
    reads: list[Path] = []

    def counted_read(path: Path) -> LockFile | None:
        reads.append(path)
        return read(path)

    monkeypatch.setattr(LockFile, "read", staticmethod(counted_read))
    result = run_baseline_checks(tmp_path, ci_mode=True)
    assert not result.passed
    assert result.checks[-1].name == "ref-consistency"
    assert reads == [lock_path]


@pytest.mark.parametrize(
    "value",
    (
        "../outside.md",
        ".grok/../../outside.md",
        ".grok/skills/../../../outside.md",
        "unmanaged/secret.md",
    ),
)
def test_historical_claim_cannot_escape_registered_roots(tmp_path: Path, value: str) -> None:
    assert _resolve_deployed_file_path(value, tmp_path, [KNOWN_TARGETS["copilot"]]) is None


def test_historical_claim_is_not_limited_to_current_replay_target(tmp_path: Path) -> None:
    path = ".grok/skills/example/SKILL.md"
    assert (
        _resolve_deployed_file_path(path, tmp_path, [KNOWN_TARGETS["copilot"]]) == tmp_path / path
    )


@pytest.mark.parametrize("directory_link", (False, True))
def test_historical_claim_cannot_follow_symlink_escape(
    tmp_path: Path, directory_link: bool
) -> None:
    project = tmp_path / "project"
    skill_root = project / ".grok/skills"
    skill_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / "SKILL.md"
    external_file.write_text("not managed\n", encoding="ascii")
    if directory_link:
        (skill_root / "example").symlink_to(outside, target_is_directory=True)
    else:
        (skill_root / "example").mkdir()
        (skill_root / "example/SKILL.md").symlink_to(external_file)
    assert (
        _resolve_deployed_file_path(
            ".grok/skills/example/SKILL.md", project, [KNOWN_TARGETS["copilot"]]
        )
        is None
    )
    assert external_file.read_text(encoding="ascii") == "not managed\n"
