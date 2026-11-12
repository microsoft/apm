"""Audit regressions for deployment-ledger owner integrity."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.audit import audit
from apm_cli.core.deployment_ledger import (
    DEPLOYMENT_OWNER_REMEDIATION,
    DeploymentLedgerCodec,
)
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import clear_apm_yml_cache

_GHOST_OWNER = "removed/beta"
_GHOST_PATH = ".claude/rules/beta.md"


@pytest.fixture(autouse=True)
def _clear_manifest_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _record(
    value: str = _GHOST_PATH,
    *,
    owners: tuple[str, ...] = (_GHOST_OWNER,),
    active_owner: str = _GHOST_OWNER,
    target: str = "claude",
    kind: LocatorKind = LocatorKind.PROJECT_RELATIVE,
) -> DeploymentRecord:
    locator = DeploymentLocator(
        kind=kind,
        target=target,
        value=value,
        runtime="vscode" if target == "mcp" else None,
        scope="project",
    )
    return DeploymentRecord(
        locator=locator,
        owners=owners,
        active_owner=active_owner,
        content_hash=f"sha256:{'a' * 64}",
    )


def _setup_project(
    project: Path,
    *,
    records: tuple[DeploymentRecord, ...] | None = None,
    dependencies: dict[str, LockedDependency] | None = None,
) -> Path:
    (project / "apm.yml").write_text(
        "name: audit-owner-test\nversion: 1.0.0\ndependencies:\n  apm: []\n",
        encoding="utf-8",
    )
    lockfile = LockFile(dependencies=dependencies or {})
    selected = records or (_record(),)
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(records={record.locator.key: record for record in selected}),
        lockfile,
    )
    path = project / "apm.lock.yaml"
    lockfile.write(path)
    return path


def _invoke(project: Path, args: list[str]):
    with patch("apm_cli.commands.audit.Path.cwd", return_value=project):
        return CliRunner().invoke(audit, args)


def test_default_json_reports_ghost_before_no_files_exit(tmp_path: Path) -> None:
    _setup_project(tmp_path)
    assert not (tmp_path / _GHOST_PATH).exists()

    result = _invoke(
        tmp_path,
        ["--no-drift", "--format", "json"],
    )

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    finding = report["findings"][0]
    assert report["passed"] is False
    assert report["exit_code"] == 1
    assert finding["category"] == "deployment-owner"
    assert finding["locator"]["value"] == _GHOST_PATH
    assert finding["owners"] == [_GHOST_OWNER]
    assert finding["active_owner"] == _GHOST_OWNER
    assert finding["remediation"] == DEPLOYMENT_OWNER_REMEDIATION
    assert "codepoint" not in finding
    assert "line" not in finding


@pytest.mark.parametrize(
    ("output_format", "suffix"),
    [
        ("json", "json"),
        ("sarif", "sarif"),
        ("markdown", "md"),
    ],
)
def test_default_output_file_is_complete_before_failure(
    tmp_path: Path,
    output_format: str,
    suffix: str,
) -> None:
    _setup_project(tmp_path)
    output = tmp_path / f"report.{suffix}"

    result = _invoke(
        tmp_path,
        [
            "--no-drift",
            "--format",
            output_format,
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert output.is_file()
    if output_format == "json":
        assert json.loads(output.read_text(encoding="utf-8"))["exit_code"] == 1
    elif output_format == "sarif":
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["runs"][0]["results"][0]["ruleId"] == ("apm/lockfile/deployment-owner")
    else:
        content = output.read_text(encoding="utf-8")
        assert "Lockfile integrity" in content
        assert _GHOST_PATH in content


def test_default_sarif_stdout_uses_deployment_owner_rule(tmp_path: Path) -> None:
    _setup_project(tmp_path)

    result = _invoke(tmp_path, ["--no-drift", "--format", "sarif"])

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    finding = report["runs"][0]["results"][0]
    assert finding["ruleId"] == "apm/lockfile/deployment-owner"
    assert finding["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "apm.lock.yaml"
    assert finding["properties"]["owners"] == [_GHOST_OWNER]
    assert DEPLOYMENT_OWNER_REMEDIATION in finding["message"]["text"]


def test_default_text_names_owner_locator_and_remediation(tmp_path: Path) -> None:
    _setup_project(tmp_path)

    result = _invoke(tmp_path, ["--no-drift"])

    assert result.exit_code == 1
    assert _GHOST_OWNER in result.output
    assert _GHOST_PATH in result.output
    assert DEPLOYMENT_OWNER_REMEDIATION in result.output


def test_strip_preserves_content_while_owner_integrity_fails(tmp_path: Path) -> None:
    _setup_project(tmp_path)
    sentinel = tmp_path / _GHOST_PATH
    sentinel.parent.mkdir(parents=True)
    content = "hidden\U000e0001content\n"
    sentinel.write_text(content, encoding="utf-8")

    result = _invoke(tmp_path, ["--strip"])

    assert result.exit_code == 1
    assert sentinel.read_text(encoding="utf-8") == content
    assert "Content was not modified" in result.output


@pytest.mark.parametrize("output_format", ["json", "sarif"])
def test_ci_machine_report_fails_owner_check(
    tmp_path: Path,
    output_format: str,
) -> None:
    _setup_project(tmp_path)

    result = _invoke(
        tmp_path,
        ["--ci", "--no-policy", "--no-drift", "--format", output_format],
    )

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    if output_format == "json":
        assert report["passed"] is False
        owner_check = next(
            check for check in report["checks"] if check["name"] == "deployment-ledger-owners"
        )
        assert owner_check["passed"] is False
        assert _GHOST_PATH in owner_check["details"][0]
        assert _GHOST_OWNER in owner_check["details"][0]
        assert owner_check["message"].endswith(DEPLOYMENT_OWNER_REMEDIATION)
        assert report.get("drift", []) == []
    else:
        assert report["runs"][0]["results"][0]["ruleId"] == ("deployment-ledger-owners")


@pytest.mark.parametrize("output_format", ["json", "sarif"])
def test_ci_output_file_is_complete_before_failure(
    tmp_path: Path,
    output_format: str,
) -> None:
    _setup_project(tmp_path)
    output = tmp_path / f"ci.{output_format}"

    result = _invoke(
        tmp_path,
        [
            "--ci",
            "--no-policy",
            "--no-drift",
            "--format",
            output_format,
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    if output_format == "json":
        assert report["passed"] is False
    else:
        assert report["runs"][0]["results"]


def test_stale_active_owner_is_reported_with_valid_survivor(tmp_path: Path) -> None:
    _setup_project(
        tmp_path,
        dependencies={
            "kept/alpha": LockedDependency(repo_url="kept/alpha"),
        },
        records=(
            _record(
                owners=("kept/alpha", _GHOST_OWNER),
                active_owner=_GHOST_OWNER,
            ),
        ),
    )

    result = _invoke(tmp_path, ["--no-drift", "--format", "json"])

    assert result.exit_code == 1
    finding = json.loads(result.stdout)["findings"][0]
    assert finding["invalid_owners"] == [_GHOST_OWNER]
    assert finding["invalid_active_owner"] == _GHOST_OWNER


def test_valid_owner_missing_file_fails_file_check_not_owner_check(
    tmp_path: Path,
) -> None:
    owner = "kept/alpha"
    _setup_project(
        tmp_path,
        dependencies={
            owner: LockedDependency(repo_url=owner),
        },
        records=(
            _record(
                owners=(owner,),
                active_owner=owner,
            ),
        ),
    )
    assert not (tmp_path / _GHOST_PATH).exists()

    result = _invoke(
        tmp_path,
        ["--ci", "--no-policy", "--no-drift", "--format", "json"],
    )

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["deployment-ledger-owners"]["passed"] is True
    assert checks["deployed-files-present"]["passed"] is False
    assert _GHOST_PATH in checks["deployed-files-present"]["details"]


def test_workspace_local_bundle_and_mcp_owners_are_valid(tmp_path: Path) -> None:
    local = _record(
        ".agents/skills/local/SKILL.md",
        owners=(".", "local-bundle"),
        active_owner="local-bundle",
        target="agents",
    )
    mcp = _record(
        "mcp://server",
        owners=(".",),
        active_owner=".",
        target="mcp",
        kind=LocatorKind.URI,
    )
    _setup_project(tmp_path, records=(local, mcp))
    deployed = tmp_path / ".agents/skills/local/SKILL.md"
    deployed.parent.mkdir(parents=True)
    deployed.write_text("# Local skill\n", encoding="utf-8")

    result = _invoke(tmp_path, ["--no-drift", "--format", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["passed"] is True
