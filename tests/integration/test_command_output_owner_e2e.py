"""Installed-CLI contract: one owner renders command output, one line per event (#4089).

Each test snapshots the output of the installed ``apm`` binary for a path that
used to print a duplicate line, a raw adapter line, a third-party tip or an
absolute path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]


def _project(tmp_path: Path, apm_binary_path: Path, name: str, manifest: dict | None):
    isolated = IsolatedApmEnvironment.create(tmp_path / name, base_env=dict(os.environ))
    env = {**isolated.subprocess_env(), "COLUMNS": "400"}
    root = isolated.work_root / "app"
    root.mkdir(parents=True)
    if manifest is not None:
        dump_yaml(manifest, root / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    def expect_ok(*args: str) -> str:
        result = runner.run(args, cwd=root, env=env)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        return output

    return isolated, root, expect_ok


def _manifest(targets: list[str], **extra) -> dict:
    return {"name": "app", "version": "1.0.0", "targets": targets, **extra}


def test_frozen_install_announces_mcp_targets_once(tmp_path, apm_binary_path):
    """--frozen resolves MCP targets once, so the target line prints once."""
    _isolated, _root, expect_ok = _project(
        tmp_path, apm_binary_path, "frozen", _manifest(["copilot"])
    )
    expect_ok("install", "--no-policy", "--mcp", "fetch", "--", "echo", "ready")

    output = expect_ok("install", "--frozen", "--no-policy")

    assert output.count("Targeting declared target from apm.yml: copilot") == 1, output


def test_skill_bundle_reports_one_line_per_package(tmp_path, apm_binary_path):
    """A skills/ bundle reports its skills in one tree line, not two."""
    isolated, root, expect_ok = _project(
        tmp_path,
        apm_binary_path,
        "bundle",
        _manifest(["claude"], dependencies={"apm": ["../bundle"]}),
    )
    (root / ".claude").mkdir()
    bundle = isolated.work_root / "bundle"
    for skill in ("alpha", "beta"):
        skill_dir = bundle / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: {skill} skill\n---\n# {skill}\n",
            encoding="utf-8",
        )
    dump_yaml({"name": "bundle", "version": "1.0.0"}, bundle / "apm.yml")

    output = expect_ok("install", "--no-policy")

    skill_lines = [line for line in output.splitlines() if "integrated" in line.lower()]
    assert skill_lines == ["  |-- 2 skill(s) integrated -> .claude/skills/"], output


@pytest.mark.parametrize(
    ("target", "config_dir", "harness"),
    [
        pytest.param("codex", ".codex", "Codex CLI", id="codex"),
        pytest.param("gemini", ".gemini", "Gemini CLI", id="gemini"),
        pytest.param("kiro", ".kiro", "Kiro", id="kiro"),
    ],
)
def test_mcp_adapter_success_is_reported_once(
    tmp_path, apm_binary_path, target, config_dir, harness
):
    """Client adapters do not echo their own success line next to the install tree."""
    _isolated, root, expect_ok = _project(tmp_path, apm_binary_path, target, _manifest([target]))
    (root / config_dir).mkdir()

    output = expect_ok("install", "--no-policy", "--mcp", "fetch", "--", "echo", "ready")

    assert output.count(f"fetch -> {harness} (configured)") == 1, output
    assert "Configured MCP server 'fetch'" not in output, output


def test_default_init_prints_no_third_party_tip(tmp_path, apm_binary_path):
    """The default init path only names APM next steps."""
    _isolated, _root, expect_ok = _project(tmp_path, apm_binary_path, "init", None)

    output = expect_ok("init", "--yes")

    assert "APM project initialized" in output, output
    assert "agentrc" not in output, output


def test_install_mcp_names_the_manifest_relatively(tmp_path, apm_binary_path):
    """install --mcp names the manifest it changed without the absolute path."""
    _isolated, root, expect_ok = _project(
        tmp_path, apm_binary_path, "mcp-path", _manifest(["copilot"])
    )

    output = expect_ok("install", "--no-policy", "--mcp", "fetch", "--", "echo", "ready")

    assert "  manifest: apm.yml" in output, output
    assert str(root) not in output, output
    assert root.as_posix() not in output, output


def test_retired_experimental_flag_in_config_is_ignored(tmp_path, apm_binary_path):
    """A flag a user enabled before it was retired is ignored with one verbose note."""
    isolated, _root, expect_ok = _project(
        tmp_path, apm_binary_path, "retired", _manifest(["copilot"])
    )
    isolated.config_root.mkdir(parents=True, exist_ok=True)
    (isolated.config_root / "config.json").write_text(
        json.dumps({"experimental": {"marketplace_authoring": True}}), encoding="utf-8"
    )

    quiet = expect_ok("experimental", "list")
    verbose = expect_ok("experimental", "list", "--verbose")

    assert "marketplace-authoring" not in quiet, quiet
    assert verbose.count("Ignoring 1 unknown or retired flag(s) in config") == 1, verbose
    expect_ok("install", "--no-policy")


def test_json_audit_after_adapter_install_keeps_stdout_json(tmp_path, apm_binary_path):
    """Adapter-backed state never leaks status lines into a JSON-mode stdout."""
    isolated, root, _apm = _project(tmp_path, apm_binary_path, "json", _manifest(["codex"]))
    (root / ".codex").mkdir()
    env = {**isolated.subprocess_env(), "COLUMNS": "400"}
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    installed = runner.run(
        ("install", "--no-policy", "--mcp", "fetch", "--", "echo", "ready"), cwd=root, env=env
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    audited = runner.run(("audit", "--ci", "--no-policy", "--format", "json"), cwd=root, env=env)

    assert audited.returncode == 0, audited.stdout + audited.stderr
    assert json.loads(audited.stdout)["passed"] is True
