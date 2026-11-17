"""Installed-CLI contract: deprecated ``target:`` warning and ``apm manifest migrate`` (#3762)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]

_WARNING = (
    "[!] apm.yml uses the deprecated 'target:' key. Use 'targets:' (a YAML list). "
    "Run 'apm manifest migrate' to update it. 'target:' will be removed in v1.0."
)
_PROJECT_KEEP = {"apm.yml", "packages"}
_HOME_SKIP = {".apm", ".apm-local", ".cache"}


class _Scenario:
    def __init__(self, tmp_path: Path, apm_binary_path: Path, target_line: str) -> None:
        self.isolated = IsolatedApmEnvironment.create(tmp_path / "env", base_env=dict(os.environ))
        env = dict(self.isolated.subprocess_env())
        env.pop("CI", None)
        self.env = env
        self.runner = ApmLifecycleRunner((str(apm_binary_path),))
        self.home = Path(env["HOME"])
        self.project = self.isolated.work_root / "consumer"
        package = self.project / "packages" / "fixture"
        primitives = package / ".apm"
        (primitives / "instructions").mkdir(parents=True)
        (primitives / "instructions" / "style.instructions.md").write_text(
            "---\napplyTo: '**'\ndescription: Style\n---\nUse tabs.\n", encoding="utf-8"
        )
        (primitives / "prompts").mkdir()
        (primitives / "prompts" / "review.prompt.md").write_text(
            "---\ndescription: Review\n---\nReview it.\n", encoding="utf-8"
        )
        (primitives / "agents").mkdir()
        (primitives / "agents" / "helper.agent.md").write_text(
            "---\ndescription: Helper\n---\nHelp.\n", encoding="utf-8"
        )
        skill = primitives / "skills" / "fixture-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: fixture-skill\ndescription: Fixture\n---\n# Skill\n", encoding="utf-8"
        )
        (package / "apm.yml").write_text("name: fixture\nversion: 1.0.0\n", encoding="utf-8")
        self.manifest = self.project / "apm.yml"
        self.manifest.write_text(
            "name: consumer\n"
            "version: 1.0.0\n"
            "# deployment targets\n"
            f"{target_line}\n"
            "dependencies:\n"
            "  apm:\n"
            "    - ./packages/fixture\n"
            "  mcp:\n"
            "    - name: fixture-server\n"
            "      registry: false\n"
            "      transport: stdio\n"
            "      command: inert-fixture-command\n",
            encoding="utf-8",
        )

    def apm(self, *args: str, env: dict[str, str] | None = None) -> CommandResult:
        return self.runner.run(args, cwd=self.project, env=env or self.env)

    def install(self, *extra: str) -> CommandResult:
        result = self.apm("install", "--no-policy", "--parallel-downloads", "0", *extra)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    def deployed(self) -> dict[str, bytes]:
        """Snapshot every byte APM deployed into the project and the user home."""
        files: dict[str, bytes] = {}
        for root, label, skip in (
            (self.project, "project", _PROJECT_KEEP),
            (self.home, "home", _HOME_SKIP),
        ):
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root)
                if relative.parts[0] in skip or relative.parts[0] == ".apm-local":
                    continue
                if path.is_file():
                    files[f"{label}/{relative.as_posix()}"] = path.read_bytes()
        return files

    def reset_deployment(self) -> None:
        for root, keep in ((self.project, _PROJECT_KEEP), (self.home, _HOME_SKIP | {".gitconfig"})):
            for child in root.iterdir():
                if child.name in keep:
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize(
    ("target_line", "migrated_line"),
    [
        ("target: vscode", "targets: [vscode]"),
        ("target: claude,agents", "targets: [claude, copilot]"),
        ("target: [agy, cursor]", "targets: [antigravity, cursor]"),
        ("target: all", "targets:"),
    ],
)
def test_migrated_manifest_deploys_byte_identical_files_and_mcp(
    tmp_path: Path, apm_binary_path: Path, target_line: str, migrated_line: str
) -> None:
    scenario = _Scenario(tmp_path, apm_binary_path, target_line)
    legacy_install = scenario.install()
    legacy = scenario.deployed()
    scenario.reset_deployment()

    migrate = scenario.apm("manifest", "migrate", "--yes")
    assert migrate.returncode == 0, migrate.stdout + migrate.stderr
    assert migrated_line in scenario.manifest.read_text(encoding="utf-8").splitlines()
    assert "# deployment targets" in scenario.manifest.read_text(encoding="utf-8")
    migrated_install = scenario.install()

    assert scenario.deployed() == legacy
    assert _WARNING in _flat(legacy_install.stderr)
    assert "deprecated 'target:' key" not in migrated_install.stderr
    if target_line == "target: vscode":
        assert "project/.vscode/mcp.json" in legacy
        assert "project/.github/mcp.json" not in legacy
    if target_line == "target: all":
        assert {"project/.claude/rules/style.md", "project/.cursor/mcp.json"} <= set(legacy)


def test_migrate_dry_run_ci_write_and_idempotence(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = _Scenario(tmp_path, apm_binary_path, "target: claude,vscode")
    original = scenario.manifest.read_text(encoding="utf-8")

    dry_run = scenario.apm("manifest", "migrate", "--dry-run")
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    assert "+targets: [claude, vscode]" in dry_run.stdout
    assert scenario.manifest.read_text(encoding="utf-8") == original

    in_ci = scenario.apm("manifest", "migrate", "--yes", env={**scenario.env, "CI": "true"})
    assert in_ci.returncode == 1
    assert "+targets: [claude, vscode]" in in_ci.stdout
    assert "CI never writes the manifest." in _flat(in_ci.stdout)
    assert scenario.manifest.read_text(encoding="utf-8") == original

    written = scenario.apm("manifest", "migrate", "--yes")
    assert written.returncode == 0, written.stdout + written.stderr
    migrated = scenario.manifest.read_text(encoding="utf-8")
    assert "targets: [claude, vscode]" in migrated.splitlines()

    again = scenario.apm("manifest", "migrate", "--yes", env={**scenario.env, "CI": "true"})
    assert again.returncode == 0, again.stdout + again.stderr
    assert "nothing to migrate" in again.stdout
    assert scenario.manifest.read_text(encoding="utf-8") == migrated


def test_migrate_refuses_unmappable_target(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = _Scenario(tmp_path, apm_binary_path, "target: intellij")
    original = scenario.manifest.read_text(encoding="utf-8")

    result = scenario.apm("manifest", "migrate", "--yes")

    assert result.returncode == 1
    assert (
        "[x] 'target: intellij' has no 'targets:' equivalent; remove it, or pass "
        "'--target intellij' on the command line"
    ) in _flat(result.stdout)
    assert scenario.manifest.read_text(encoding="utf-8") == original


def test_warning_is_stderr_only_once_and_keeps_exit_codes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _Scenario(tmp_path, apm_binary_path, "target: claude")

    install = scenario.install()
    frozen = scenario.install("--frozen")
    compile_result = scenario.apm("compile")

    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    for result in (install, frozen, compile_result):
        assert _flat(result.stderr).count(_WARNING) == 1
        assert "deprecated 'target:' key" not in result.stdout
    assert "target: claude" in scenario.manifest.read_text(encoding="utf-8").splitlines()


def test_dependency_manifest_with_target_does_not_warn(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _Scenario(tmp_path, apm_binary_path, "targets: [claude]")
    (scenario.project / "packages" / "fixture" / "apm.yml").write_text(
        "name: fixture\nversion: 1.0.0\ntarget: claude\ncompilation:\n  target: claude\n",
        encoding="utf-8",
    )

    result = scenario.install()

    assert "deprecated 'target:' key" not in result.stdout + result.stderr
    assert "compilation.target" not in result.stdout + result.stderr
    assert (scenario.project / ".claude" / "rules" / "style.md").is_file()
