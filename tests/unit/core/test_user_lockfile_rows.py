"""User-lockfile deployment rows: persisted form and read-back (#4248)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.yaml_io import load_yaml

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-portability.byte_deterministic")]

_OWNER = "owner/skills"


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    apm_home = tmp_path / "apm-home"
    claude = tmp_path / "outside" / "claude"
    hermes = tmp_path / "outside" / "hermes"
    for path in (home, apm_home, claude, hermes):
        path.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APM_HOME", str(apm_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    return {
        "home": home.resolve(),
        "apm_home": apm_home,
        "claude": claude.resolve(),
        "hermes": hermes.resolve(),
    }


def _round_trip(
    apm_home: Path, files: list[tuple[str, str]]
) -> tuple[LockFile, list[dict], LockFile]:
    """Record ``(target, path)`` claims for one owner, write the user lock, read it back."""
    lockfile = LockFile()
    lockfile.dependencies[_OWNER] = LockedDependency(repo_url=_OWNER)
    records = {}
    for target, value in files:
        locator = DeploymentLocator(
            kind=LocatorKind.PROJECT_RELATIVE,
            target=target,
            value=value,
            runtime=None,
            scope=DeploymentLedgerCodec.legacy_scope(value),
        )
        records[locator.key] = DeploymentRecord(
            locator=locator, owners=(_OWNER,), active_owner=_OWNER, content_hash=None
        )
    DeploymentLedgerCodec.apply_to_lockfile(DeploymentLedger(records=records), lockfile)
    path = apm_home / "apm.lock.yaml"
    assert lockfile.write(path)
    rows = (load_yaml(path) or {})["deployments"]
    read = LockFile.read(path)
    assert read is not None
    return lockfile, rows, read


def _forms(rows: list[dict]) -> set[tuple[str, str, str]]:
    return {(row["kind"], row["scope"], row["value"]) for row in rows}


def test_external_targets_sharing_a_suffix_are_target_relative(roots: dict[str, Path]) -> None:
    files = [
        ("claude", f"{roots['claude'].as_posix()}/skills/demo/SKILL.md"),
        ("hermes", f"{roots['hermes'].as_posix()}/skills/demo/SKILL.md"),
    ]
    written, rows, read = _round_trip(roots["apm_home"], files)

    assert {(row["target"], row["kind"], row["scope"], row["value"]) for row in rows} == {
        ("claude", "target-relative", "user", "skills/demo/SKILL.md"),
        ("hermes", "target-relative", "user", "skills/demo/SKILL.md"),
    }
    assert read.deployment_ledger.records == written.deployment_ledger.records


def test_home_root_profile_beside_an_external_profile_stays_user_scoped(
    roots: dict[str, Path],
) -> None:
    """A HOME-root Claude row and an external-profile row keep distinct user rows.

    The external row's target-relative form would take the HOME-root row's
    key, so it stays project-relative; both rows record scope user.
    """
    home_row = "skills/demo/SKILL.md"
    external = f"{roots['claude'].as_posix()}/skills/demo/SKILL.md"
    written, rows, read = _round_trip(
        roots["apm_home"], [("claude", home_row), ("claude", external)]
    )

    assert {row["scope"] for row in rows} == {"user"}
    assert _forms(rows) == {
        ("project-relative", "user", home_row),
        ("project-relative", "user", external),
    }
    assert read.deployment_ledger.records == written.deployment_ledger.records


def test_reading_under_another_profile_maps_rows_back(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [
        ("claude", f"{roots['claude'].as_posix()}/skills/demo/SKILL.md"),
        ("hermes", f"{roots['hermes'].as_posix()}/skills/demo/SKILL.md"),
    ]
    written, _rows, _read = _round_trip(roots["apm_home"], files)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(roots["home"] / "elsewhere"))

    read = LockFile.read(roots["apm_home"] / "apm.lock.yaml")

    assert read is not None
    assert read.deployment_ledger.records == written.deployment_ledger.records
