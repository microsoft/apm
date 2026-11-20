"""``apm update`` with nothing to change still applies org policy (#4458).

When the installed set is already current, ``apm update`` must judge it
against org policy as ``apm install`` does: a dependency the policy denies
under ``enforcement: block`` exits 1, names the denial, and writes nothing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.parser import load_policy

pytestmark = [pytest.mark.integration, pytest.mark.component, pytest.mark.promise("req-pl-002")]

_DENY = textwrap.dedent("""\
    name: deny-local
    version: '1.0'
    enforcement: block
    dependencies:
      deny:
        - "**/denied-skill"
""")


def _project(root: Path) -> None:
    package = root / "denied-skill"
    (package / "skills" / "denied-skill").mkdir(parents=True)
    (package / "apm.yml").write_text("name: denied-skill\nversion: '1.0.0'\n", encoding="utf-8")
    (package / "skills" / "denied-skill" / "SKILL.md").write_text(
        "---\nname: denied-skill\ndescription: d\n---\n# Denied\n", encoding="utf-8"
    )
    (root / "apm.yml").write_text(
        textwrap.dedent("""\
            name: consumer
            version: '1.0.0'
            targets:
              - copilot
            dependencies:
              apm:
                - ./denied-skill
        """),
        encoding="utf-8",
    )


def test_update_with_nothing_to_change_refuses_a_policy_denied_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apm_cli.cli import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APM_DISABLE_UPDATE_CHECK", "1")
    _project(tmp_path)
    runner = CliRunner()
    clear_apm_yml_cache()
    monkeypatch.setenv("APM_POLICY_DISABLE", "1")
    installed = runner.invoke(cli, ["install"], catch_exceptions=False)
    assert installed.exit_code == 0, installed.output
    monkeypatch.delenv("APM_POLICY_DISABLE")
    lock_before = (tmp_path / "apm.lock.yaml").read_bytes()
    deployed = sorted(p for p in tmp_path.rglob("SKILL.md") if "apm_modules" not in p.parts)
    assert deployed

    policy, _warnings = load_policy(_DENY)
    found = PolicyFetchResult(policy=policy, source="org:acme/.github", outcome="found")
    clear_apm_yml_cache()
    with patch("apm_cli.policy.install_preflight.discover_policy_with_chain", return_value=found):
        result = runner.invoke(cli, ["update", "--yes"], catch_exceptions=False)

    output = " ".join(((result.stdout or "") + (result.stderr or "")).split())
    assert result.exit_code == 1, output
    assert "denied by pattern" in output, output
    assert "Nothing was written." in output, output
    assert (tmp_path / "apm.lock.yaml").read_bytes() == lock_before
    assert all(p.exists() for p in deployed)
