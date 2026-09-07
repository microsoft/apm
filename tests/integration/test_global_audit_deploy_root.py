"""User-scope audit resolves deployment paths outside ``~/.apm``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.install.audit_target_roots import claims_for_root
from apm_cli.integration.targets import KNOWN_TARGETS
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.component]


def test_cowork_uri_claim_is_rebased_through_target_adapter(tmp_path: Path) -> None:
    root = tmp_path / "cowork"
    claims = claims_for_root(
        {"cowork://skills/demo/SKILL.md": "demo"},
        root,
        absolute_only=True,
        targets=(KNOWN_TARGETS["copilot-cowork"],),
    )

    assert claims == {"demo/SKILL.md": "demo"}


def _tree_snapshot(
    root: Path,
) -> tuple[tuple[tuple[str, int], ...], dict[str, tuple[bytes, int]]]:
    directories = tuple(
        sorted(
            (path.relative_to(root).as_posix(), path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_dir()
        )
    )
    files = {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }
    return directories, files


def test_global_install_audit_reads_home_deployment_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "global-audit", base_env=os.environ)
    environment = isolated.subprocess_env()
    environment["APM_NO_CACHE"] = "1"
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    package = isolated.package_root / "demo-package"
    skill = package / ".apm" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    (package / "apm.yml").write_text("name: demo-package\nversion: 1.0.0\n", encoding="utf-8")
    skill.write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# Demo\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(isolated.work_root)
    install = CliRunner().invoke(
        cli,
        [
            "install",
            "--global",
            str(package),
            "--target",
            "codex",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ],
        env=environment,
    )
    assert install.exit_code == 0, install.output
    assert (isolated.home / ".agents" / "skills" / "demo" / "SKILL.md").is_file()

    monkeypatch.chdir(isolated.config_root)
    audit = CliRunner().invoke(
        cli,
        ["audit", "--ci", "--no-policy", "--format", "json"],
        env=environment,
    )
    assert audit.exit_code == 0, audit.output
    payload = json.loads(audit.output[audit.output.index("{") :])
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["deployed-files-present"]["passed"] is True
    assert checks["content-integrity"]["passed"] is True
    assert checks["drift"]["passed"] is True
    assert payload["drift"]["drift"] == []


@pytest.mark.parametrize(
    ("target", "root_env"),
    (("hermes", "HERMES_HOME"), ("claude", "CLAUDE_CONFIG_DIR")),
)
@pytest.mark.parametrize("mutation", ("modified", "missing"))
def test_global_audit_external_target_is_read_only_and_detects_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    root_env: str,
    mutation: str,
) -> None:
    isolated = IsolatedApmEnvironment.create(
        tmp_path / f"global-audit-{target}-{mutation}",
        base_env=os.environ,
    )
    external_root = isolated.root / f"external-{target}"
    external_root.mkdir()
    environment = isolated.subprocess_env()
    environment.update(
        {
            "APM_NO_CACHE": "1",
            "APM_EXPERIMENTAL_HERMES": "1",
            root_env: str(external_root),
        }
    )
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    package = isolated.package_root / "demo-package"
    source_skill = package / ".apm" / "skills" / "demo" / "SKILL.md"
    source_skill.parent.mkdir(parents=True)
    (package / "apm.yml").write_text("name: demo-package\nversion: 1.0.0\n", encoding="utf-8")
    source_skill.write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# Original\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(isolated.work_root)
    install = CliRunner().invoke(
        cli,
        [
            "install",
            "--global",
            str(package),
            "--target",
            target,
            "--no-policy",
            "--parallel-downloads",
            "0",
        ],
        env=environment,
    )
    assert install.exit_code == 0, install.output
    deployed_skill = external_root / "skills" / "demo" / "SKILL.md"
    assert deployed_skill.is_file()
    if mutation == "modified":
        deployed_skill.write_text("tampered\n", encoding="utf-8")
    else:
        deployed_skill.unlink()
    before_audit = _tree_snapshot(external_root)

    monkeypatch.chdir(isolated.config_root)
    audit = CliRunner().invoke(
        cli,
        [
            "audit",
            "--ci",
            "--no-policy",
            "--no-fail-fast",
            "--format",
            "json",
        ],
        env=environment,
    )
    assert audit.exit_code == 1, audit.output
    payload = json.loads(audit.output[audit.output.index("{") :])
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["drift"]["passed"] is False
    if mutation == "modified":
        assert checks["content-integrity"]["passed"] is False
    else:
        assert checks["deployed-files-present"]["passed"] is False
    assert _tree_snapshot(external_root) == before_audit


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires elevated Windows rights")
@pytest.mark.parametrize(
    ("target", "root_env"),
    (("hermes", "HERMES_HOME"), ("claude", "CLAUDE_CONFIG_DIR")),
)
def test_global_audit_external_target_fails_closed_on_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    root_env: str,
) -> None:
    """A managed target file replaced by an escaping symlink fails audit."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / f"global-audit-{target}-symlink",
        base_env=os.environ,
    )
    external_root = isolated.root / f"external-{target}"
    external_root.mkdir()
    environment = isolated.subprocess_env()
    environment.update({"APM_NO_CACHE": "1", root_env: str(external_root)})
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    package = isolated.package_root / "demo-package"
    source_skill = package / ".apm" / "skills" / "demo" / "SKILL.md"
    source_skill.parent.mkdir(parents=True)
    (package / "apm.yml").write_text("name: demo-package\nversion: 1.0.0\n", encoding="utf-8")
    source_skill.write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# Original\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(isolated.work_root)
    install = CliRunner().invoke(
        cli,
        [
            "install",
            "--global",
            str(package),
            "--target",
            target,
            "--no-policy",
            "--parallel-downloads",
            "0",
        ],
        env=environment,
    )
    assert install.exit_code == 0, install.output
    deployed_skill = external_root / "skills" / "demo" / "SKILL.md"
    assert deployed_skill.is_file()
    escaped_target = isolated.root / "outside.md"
    escaped_target.write_text("outside\n", encoding="utf-8")
    deployed_skill.unlink()
    deployed_skill.symlink_to(escaped_target)
    before_audit = _tree_snapshot(external_root)

    monkeypatch.chdir(isolated.config_root)
    audit = CliRunner().invoke(
        cli,
        ["audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json"],
        env=environment,
    )
    assert audit.exit_code == 1, audit.output
    payload = json.loads(audit.output[audit.output.index("{") :])
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["deployed-files-present"]["passed"] is False
    assert checks["content-integrity"]["passed"] is False
    assert _tree_snapshot(external_root) == before_audit
    assert escaped_target.read_text(encoding="utf-8") == "outside\n"


def test_global_audit_scans_unrecorded_external_target_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical hidden Unicode under a governed external root cannot evade audit."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "global-audit-unicode", base_env=os.environ)
    external_root = isolated.root / "external-hermes"
    environment = isolated.subprocess_env()
    environment.update({"APM_NO_CACHE": "1", "HERMES_HOME": str(external_root)})
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    package = isolated.package_root / "demo-package"
    source_skill = package / ".apm" / "skills" / "demo" / "SKILL.md"
    source_skill.parent.mkdir(parents=True)
    (package / "apm.yml").write_text("name: demo-package\nversion: 1.0.0\n", encoding="utf-8")
    source_skill.write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# Original\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(isolated.work_root)
    install = CliRunner().invoke(
        cli,
        [
            "install",
            "--global",
            str(package),
            "--target",
            "hermes",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ],
        env=environment,
    )
    assert install.exit_code == 0, install.output
    unmanaged = external_root / "notes" / "user-content.md"
    unmanaged.parent.mkdir()
    unmanaged.write_text(f"# Hidden {chr(0x202E)} marker\n", encoding="utf-8")
    before_unmanaged_audit = _tree_snapshot(external_root)

    monkeypatch.chdir(isolated.config_root)
    unmanaged_audit = CliRunner().invoke(
        cli,
        ["audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json"],
        env=environment,
    )
    assert unmanaged_audit.exit_code == 0, unmanaged_audit.output
    unmanaged_payload = json.loads(unmanaged_audit.output[unmanaged_audit.output.index("{") :])
    unmanaged_checks = {check["name"]: check for check in unmanaged_payload["checks"]}
    assert unmanaged_checks["content-integrity"]["passed"] is True
    assert _tree_snapshot(external_root) == before_unmanaged_audit

    unrecorded = external_root / "skills" / "unrecorded" / "SKILL.md"
    unrecorded.parent.mkdir()
    unrecorded.write_text(f"# Hidden {chr(0x202E)} marker\n", encoding="utf-8")
    before_audit = _tree_snapshot(external_root)

    audit = CliRunner().invoke(
        cli,
        ["audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json"],
        env=environment,
    )
    assert audit.exit_code == 1, audit.output
    payload = json.loads(audit.output[audit.output.index("{") :])
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["content-integrity"]["passed"] is False
    assert _tree_snapshot(external_root) == before_audit
