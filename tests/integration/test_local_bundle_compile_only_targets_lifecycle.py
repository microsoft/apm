"""Local bundles installed for compile-only targets audit clean (#3705, #3706).

Codex, Gemini and OpenCode have no native instructions primitive, so
``apm install <bundle>`` stages the bundle's instructions under
``apm_modules/<slug>/.apm/instructions/`` for ``apm compile``. Staging is
compile input, not deployed output: it must never become a deployment claim
that ``apm audit --ci`` then reports as missing or unresolved, and
``apm prune`` still clears it.

A bundle carries plugin-native Markdown agents. Local bundle install cannot
render Codex TOML agents, so it skips them with one warning per agent instead
of writing Markdown into ``.codex/agents/*.toml``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_NO_POLICY = "--no-policy"
_AGENTS = ("rev", "lint")
_SKIP_WARNING = "[!] Skipped packed bundle path"
_STAGED = "apm_modules/bundler/.apm/instructions/glob.instructions.md"
_COMPILE_ONLY_TARGETS = ("codex", "gemini", "opencode")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


class _Bundle:
    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
        self.env = isolated.subprocess_env()
        self.root = isolated.work_root
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=240)

    def apm(self, cwd: Path, *args: str, expect: int | None = 0) -> CommandResult:
        result = self.runner.run(args, cwd=cwd, env=self.env)
        if expect is not None:
            assert result.returncode == expect, (
                f"apm {' '.join(args)} -> {result.returncode} (expected {expect})\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def pack(self) -> Path:
        producer = self.root / "bundler"
        _write(
            producer / "apm.yml",
            "name: bundler\nversion: 1.0.0\ndescription: b\nlicense: MIT\n"
            "dependencies:\n  apm: []\n",
        )
        _write(
            producer / ".apm/instructions/glob.instructions.md",
            '---\ndescription: Global\napplyTo: "**"\n---\nAlways be global.\n',
        )
        for name in _AGENTS:
            _write(
                producer / f".apm/agents/{name}.agent.md",
                f"---\nname: {name}\ndescription: Agent {name}\n---\nDo {name}.\n",
            )
        _write(
            producer / ".apm/skills/helper/SKILL.md",
            "---\nname: helper\ndescription: Helper skill\n---\nHelp.\n",
        )
        self.apm(producer, "install", _NO_POLICY, "--target", "claude")
        self.apm(producer, "pack", "-o", str(self.root / "out"))
        bundle = self.root / "out" / "bundler-1.0.0"
        assert (bundle / "agents/rev.agent.md").is_file()
        assert (bundle / "instructions/glob.instructions.md").is_file()
        return bundle

    def consumer(self, target: str) -> Path:
        project = self.root / f"proj-{target}"
        _write(project / "apm.yml", f"name: proj\nversion: 1.0.0\ntargets: [{target}]\n")
        return project


def _claims(project: Path) -> set[str]:
    lock = load_yaml(project / "apm.lock.yaml") or {}
    return {
        *(lock.get("local_deployed_files") or []),
        *(lock.get("local_deployed_file_hashes") or {}),
        *(row.get("value") for row in lock.get("deployments") or []),
    }


@pytest.mark.parametrize("target", _COMPILE_ONLY_TARGETS)
def test_staged_instructions_are_compile_input_not_deployment_claims(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    world = _Bundle(tmp_path, apm_binary_path)
    bundle = world.pack()
    project = world.consumer(target)

    installed = world.apm(project, "install", str(bundle), "--target", target, _NO_POLICY)
    assert "staged 1 instruction(s) for compile" in _output(installed)
    assert (project / _STAGED).is_file()
    assert (project / ".agents/skills/helper/SKILL.md").is_file()
    claims = _claims(project)
    assert ".agents/skills/helper/SKILL.md" in claims
    assert not any(claim.startswith("apm_modules/") for claim in claims), sorted(claims)
    world.apm(project, "audit", "--ci", _NO_POLICY, "--no-fail-fast")

    # Re-installing the same bundle converges: no lockfile churn.
    lock = (project / "apm.lock.yaml").read_bytes()
    world.apm(project, "install", str(bundle), "--target", target, _NO_POLICY)
    assert (project / "apm.lock.yaml").read_bytes() == lock

    # The staged instruction still feeds compile (GEMINI.md imports AGENTS.md).
    assert load_yaml(project / "apm.lock.yaml")["local_staged_files"] == [_STAGED]
    world.apm(project, "compile")
    assert "Always be global." in (project / "AGENTS.md").read_text(encoding="utf-8")
    world.apm(project, "audit", "--ci", _NO_POLICY, "--no-fail-fast")

    # A declarative install keeps the staging record it does not own.
    world.apm(project, "install", _NO_POLICY)
    assert load_yaml(project / "apm.lock.yaml")["local_staged_files"] == [_STAGED]
    assert (project / _STAGED).is_file()

    # Prune clears the staging and its record; deployed bundle files stay claimed.
    world.apm(project, "prune")
    assert not (project / "apm_modules/bundler").exists()
    assert (project / ".agents/skills/helper/SKILL.md").is_file()
    assert "local_staged_files" not in load_yaml(project / "apm.lock.yaml")
    assert ".agents/skills/helper/SKILL.md" in _claims(project)
    world.apm(project, "audit", "--ci", _NO_POLICY, "--no-fail-fast")


def _move_staging_into_claims(project: Path) -> None:
    """Rewrite the lockfile into its pre-#3705 shape: staging as a bundle claim."""
    lock = load_yaml(project / "apm.lock.yaml")
    staged = lock.pop("local_staged_files")
    template = next(
        row for row in lock["deployments"] if row["value"] == ".agents/skills/helper/SKILL.md"
    )
    for path in staged:
        digest = compute_file_hash(project / path)
        lock["local_deployed_files"] = sorted({*lock["local_deployed_files"], path})
        lock["local_deployed_file_hashes"][path] = digest
        lock["deployments"].append({**template, "value": path, "content_hash": digest})
    dump_yaml(lock, project / "apm.lock.yaml")


def test_bundle_reinstall_migrates_pre_3705_staging_claims(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _Bundle(tmp_path, apm_binary_path)
    bundle = world.pack()
    project = world.consumer("opencode")
    install = ("install", str(bundle), "--target", "opencode", _NO_POLICY)
    world.apm(project, *install)
    _move_staging_into_claims(project)
    assert _STAGED in _claims(project)
    # The old shape reproduces #3705: audit verifies staging no target reads.
    world.apm(project, "audit", "--ci", _NO_POLICY, "--no-fail-fast", expect=1)

    world.apm(project, *install)
    lock = load_yaml(project / "apm.lock.yaml")
    assert not any(claim.startswith("apm_modules/") for claim in _claims(project))
    assert lock["local_staged_files"] == [_STAGED]
    assert ".agents/skills/helper/SKILL.md" in _claims(project)
    world.apm(project, "compile")
    assert "Always be global." in (project / "AGENTS.md").read_text(encoding="utf-8")
    world.apm(project, "audit", "--ci", _NO_POLICY, "--no-fail-fast")


def test_codex_bundle_install_skips_markdown_agents_with_one_warning_each(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _Bundle(tmp_path, apm_binary_path)
    bundle = world.pack()
    project = world.consumer("codex")

    installed = world.apm(project, "install", str(bundle), "--target", "codex", _NO_POLICY)
    text = _output(installed)
    for name in _AGENTS:
        assert text.count(f"'agents/{name}.agent.md' for target codex") == 1, text
    # Exactly one [!] skip warning per skipped agent, and nothing else skipped.
    assert text.count(_SKIP_WARNING) == len(_AGENTS), text
    assert f"({len(_AGENTS)} skipped)" in text, text
    assert not (project / ".codex/agents").exists()
    assert not any(claim.startswith(".codex/") for claim in _claims(project))
    world.apm(project, "audit", "--ci", _NO_POLICY, "--no-fail-fast")

    # Targets that deploy Markdown agents natively still receive them.
    other = world.consumer("opencode")
    world.apm(other, "install", str(bundle), "--target", "opencode", _NO_POLICY)
    assert (other / ".opencode/agents/rev.md").is_file()
