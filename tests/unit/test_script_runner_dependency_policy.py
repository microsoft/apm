"""``apm run`` and ``apm preview`` apply the org dependency policy to the packages they read."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.core.script_runner import ScriptRunner
from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-violation-subjects")]

_RESOLVE = "apm_cli.policy.outcome_routing.resolve_trust_policy"
_DENY = SimpleNamespace(
    enforcement="block",
    dependencies=SimpleNamespace(),
)


def _deny(subjects, _policy):
    return {
        key: "org policy: denied by pattern: **/denied" for key, _ref in subjects if "denied" in key
    }


@pytest.fixture
def project(tmp_path: Path):
    (tmp_path / "apm.yml").write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")
    prompts = tmp_path / "apm_modules" / "acme" / "denied" / ".apm" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "review.prompt.md").write_text("---\ndescription: r\n---\nReview.\n")
    deployed = tmp_path / ".github" / "prompts"
    deployed.mkdir(parents=True)
    (deployed / "audit.prompt.md").write_text("---\ndescription: a\n---\nAudit.\n")
    (tmp_path / "mine.prompt.md").write_text("---\ndescription: m\n---\nMine.\n")
    lock = LockFile()
    lock.dependencies["acme/denied"] = LockedDependency(
        repo_url="acme/denied", deployed_files=[".github/prompts/audit.prompt.md"]
    )
    lock.write(tmp_path / "apm.lock.yaml")
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(old)


@pytest.mark.parametrize(
    "prompt",
    ["apm_modules/acme/denied/.apm/prompts/review.prompt.md", ".github/prompts/audit.prompt.md"],
)
def test_a_denied_package_prompt_is_never_compiled(project: Path, prompt: str) -> None:
    with (
        patch(_RESOLVE, return_value=_DENY),
        patch("apm_cli.install.exec_gate.policy_denied_dependencies", side_effect=_deny),
        pytest.raises(RuntimeError, match=r"acme/denied: org policy"),
    ):
        ScriptRunner()._auto_compile_prompts(f"copilot -p {prompt}", {})

    assert not (project / ".apm" / "compiled").exists(), "nothing was compiled"


def test_a_project_prompt_compiles_without_resolving_policy(project: Path) -> None:
    with patch(_RESOLVE) as resolve:
        _command, compiled, _content = ScriptRunner()._auto_compile_prompts(
            "copilot -p mine.prompt.md", {}
        )

    resolve.assert_not_called()
    assert compiled == ["mine.prompt.md"]
    assert (project / ".apm" / "compiled" / "mine.txt").is_file()


def test_auto_install_refuses_a_denied_package_before_downloading(project: Path) -> None:
    with (
        patch(_RESOLVE, return_value=_DENY),
        patch("apm_cli.install.exec_gate.policy_denied_dependencies", side_effect=_deny),
        patch("apm_cli.deps.github_downloader.GitHubPackageDownloader") as downloader,
        pytest.raises(RuntimeError, match=r"Blocked by org policy"),
    ):
        ScriptRunner()._auto_install_virtual_package("acme/denied/prompts/x.prompt.md")

    downloader.assert_not_called()
    assert "acme/denied" not in (project / "apm.yml").read_text(encoding="utf-8")


def test_warn_enforcement_compiles_and_warns(project: Path, capsys) -> None:
    prompt = "apm_modules/acme/denied/.apm/prompts/review.prompt.md"
    with (
        patch(_RESOLVE, return_value=SimpleNamespace(enforcement="warn")),
        patch("apm_cli.install.exec_gate.policy_denied_dependencies", side_effect=_deny),
    ):
        _command, compiled, _content = ScriptRunner()._auto_compile_prompts(
            f"copilot -p {prompt}", {}
        )

    assert compiled == [prompt]
    assert "acme/denied: org policy" in capsys.readouterr().out
