"""Real CLI engine regeneration keeps Codex MCP ownership and user settings."""

import os
from pathlib import Path

import pytest
import tomlkit

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("user_scope", [False, True])
def test_codex_install_repairs_owned_legacy_ids_and_converges(
    tmp_path: Path, apm_engine_command: tuple[str, ...], user_scope: bool
) -> None:
    """The same install command repairs a prior release's output in either scope."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["CODEX_HOME"] = str(isolated.home / "custom-codex")
    project = isolated.config_root if user_scope else isolated.work_root
    project.joinpath("apm.yml").write_text(
        "name: codex-schema-test\nversion: 1.0.0\ntargets: [codex]\n"
        "dependencies:\n  mcp:\n"
        "    - name: managed-stdio\n      registry: false\n      transport: stdio\n"
        "      command: echo\n      args: [hello]\n"
        "    - name: managed-http\n      registry: false\n      transport: http\n"
        "      url: https://example.test/mcp\n",
        encoding="utf-8",
    )
    config_path = (
        Path(environment["CODEX_HOME"]) if user_scope else project / ".codex"
    ) / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    unrelated = '# personal settings\nmodel = "gpt-5"\n[mcp_servers.personal] # user note\ncommand = "custom"\nid = "personal-id"\n'
    config_path.write_text(unrelated, encoding="utf-8")
    args = ("install", "--only", "mcp", "--target", "codex")
    if user_scope:
        args += ("--global",)
    runner = ApmLifecycleRunner(apm_engine_command)

    result = runner.run(args, cwd=isolated.work_root, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    native = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    for name in ("managed-stdio", "managed-http"):
        assert "id" not in native["mcp_servers"][name]
        native["mcp_servers"][name]["id"] = ""
    # Seed precisely the obsolete native field; keep the real install's ledger.
    config_path.write_text(tomlkit.dumps(native), encoding="utf-8")
    lockfile = project / "apm.lock.yaml"
    ownership = load_yaml(lockfile)["mcp_target_servers"]
    assert ownership == {"codex": ["managed-http", "managed-stdio"]}

    result = runner.run(args, cwd=isolated.work_root, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    corrected = config_path.read_text(encoding="utf-8")
    native = tomlkit.parse(corrected)
    assert unrelated in corrected
    for name in ("managed-stdio", "managed-http"):
        assert "id" not in native["mcp_servers"][name]
    assert load_yaml(lockfile)["mcp_target_servers"] == ownership

    result = runner.run(args, cwd=isolated.work_root, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert config_path.read_text(encoding="utf-8") == corrected
    assert load_yaml(lockfile)["mcp_target_servers"] == ownership
