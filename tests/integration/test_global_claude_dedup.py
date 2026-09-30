"""Global install/compile must deliver Claude instructions only once."""

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = pytest.mark.component


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> IsolatedApmEnvironment:
    """Keep in-process CLI calls away from real credentials and configuration."""
    environment = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    for name, value in environment.process_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(environment.work_root)
    return environment


@pytest.mark.parametrize("custom_config", [False, True])
@pytest.mark.parametrize("linked", [False, True])
def test_installed_global_rules_are_not_compiled_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated: IsolatedApmEnvironment,
    custom_config: bool,
    linked: bool,
) -> None:
    """Native Claude rules replace its root output without affecting Codex."""
    from apm_cli.commands.compile.cli import compile as compile_cmd
    from apm_cli.commands.install import install as install_cmd
    from apm_cli.primitives.discovery import clear_discovery_cache

    home = isolated.home
    config = tmp_path / "custom-claude" if custom_config else home / ".claude"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    if custom_config:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    package = tmp_path / "example"
    instructions = package / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (package / "apm.yml").write_text("name: example\nversion: 1.0.0\n", encoding="utf-8")
    body = "Use descriptive variable names.\n"
    if linked:
        (package / "guide.md").write_text("Example package reference.\n", encoding="utf-8")
        body += "Read [guide](../../guide.md).\n"
    (instructions / "style.instructions.md").write_text(
        "---\ndescription: Example global guidance\n---\n" + body, encoding="utf-8"
    )
    clear_discovery_cache()
    try:
        runner = CliRunner()
        installed = runner.invoke(install_cmd, ["-g", str(package), "--target", "claude,codex"])
        assert installed.exit_code == 0, installed.output
        rule = config / "rules" / "style.md"
        native_content = rule.read_bytes()
        assert "Use descriptive variable names." in native_content.decode("utf-8")

        compiled = runner.invoke(compile_cmd, ["-g"])
        assert compiled.exit_code == 0, compiled.output
        assert not (config / "CLAUDE.md").exists(), compiled.output
        assert body.strip() in (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
        assert rule.read_bytes() == native_content

        forced = runner.invoke(compile_cmd, ["-g", "--force-instructions"])
        assert forced.exit_code == 0, forced.output
        assert body.strip() in (config / "CLAUDE.md").read_text(encoding="utf-8")
        assert rule.read_bytes() == native_content
    finally:
        clear_discovery_cache()


def test_global_compile_retains_existing_root_and_rejects_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated: IsolatedApmEnvironment
) -> None:
    """Deduplication never enables global cleanup or silently deletes an existing root."""
    from apm_cli.commands.compile.cli import compile as compile_cmd
    from apm_cli.primitives.discovery import clear_discovery_cache

    home = isolated.home
    config = tmp_path / "external-claude"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    source = home / ".apm"
    instructions = source / "apm_modules" / "example" / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (source / "apm.yml").write_text("targets: [claude, codex]\n", encoding="utf-8")
    body = "Use descriptive variable names.\n"
    (instructions / "style.instructions.md").write_text(
        "---\ndescription: Example\n---\n" + body, encoding="utf-8"
    )
    clear_discovery_cache()
    try:
        runner = CliRunner()
        initial = runner.invoke(compile_cmd, ["-g"])
        assert initial.exit_code == 0, initial.output
        root = config / "CLAUDE.md"
        original = root.read_bytes()
        codex = home / ".codex" / "AGENTS.md"
        codex_original = codex.read_bytes()
        rules = config / "rules"
        rules.mkdir()
        rule = rules / "style.md"
        rule.write_text(body, encoding="utf-8")

        retained = runner.invoke(compile_cmd, ["-g"])
        assert retained.exit_code == 0, retained.output
        assert "Review it manually" in " ".join(retained.output.split())
        assert "--clean --dry-run" not in retained.output
        assert root.read_bytes() == original

        preview = runner.invoke(compile_cmd, ["-g", "--dry-run"])
        assert preview.exit_code == 0, preview.output
        assert "retained" in preview.output.lower()
        assert root.read_bytes() == original

        cleaned = runner.invoke(compile_cmd, ["-g", "--clean"])
        assert cleaned.exit_code == 2, cleaned.output
        assert "--global is not valid with --clean" in cleaned.output
        assert root.read_bytes() == original
        assert codex.read_bytes() == codex_original
        assert rule.read_text(encoding="utf-8") == body

        repeated = runner.invoke(compile_cmd, ["-g"])
        assert repeated.exit_code == 0, repeated.output
        assert root.read_bytes() == original
        assert codex.read_bytes() == codex_original
    finally:
        clear_discovery_cache()
