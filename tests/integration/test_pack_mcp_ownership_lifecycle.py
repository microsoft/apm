"""``apm pack`` output does not depend on whether ``apm install`` ran (#3966).

``apm install`` renders the package's own MCP dependencies into root
``.mcp.json`` for copilot and claude. That render is APM's consumer output (it
can carry the author's credential headers), so pack must never ship it.
"""

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = pytest.mark.e2e

_FIXTURE_TOKEN = "fixture-token-3966-not-a-secret"
_MANIFESTS = (
    ".codex-plugin/plugin.json",
    ".claude-plugin/plugin.json",
    ".github/plugin/plugin.json",
)


def _pack(runner: ApmLifecycleRunner, project: Path, env: dict[str, str]) -> dict[str, bytes]:
    """Pack and return every manifest and ``build/`` file as {relative path: bytes}."""
    result = runner.run(("pack", "--force"), cwd=project, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    output = {rel: (project / rel).read_bytes() for rel in _MANIFESTS}
    for path in (project / "build").rglob("*"):
        if path.is_file():
            output[path.relative_to(project).as_posix()] = path.read_bytes()
    return output


def test_pack_output_is_identical_before_and_after_install(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    env["MY_TOKEN"] = _FIXTURE_TOKEN
    project = isolated.work_root
    skill = project / "skills" / "hello" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: hello\ndescription: hi\n---\n# hi\n", encoding="utf-8")
    project.joinpath("apm.yml").write_text(
        "name: hello-codex\nversion: 2.3.4\ndescription: Hello\nauthor: Ann\nlicense: MIT\n"
        "targets: [codex, copilot, claude]\n"
        "dependencies:\n  mcp:\n"
        "    - name: devsrv\n      registry: false\n      transport: http\n"
        "      url: https://internal.example.test/mcp\n"
        "      headers:\n        Authorization: 'Bearer ${MY_TOKEN}'\n",
        encoding="utf-8",
    )
    runner = ApmLifecycleRunner(apm_engine_command)

    before = _pack(runner, project, env)
    result = runner.run(("install",), cwd=project, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "devsrv" in project.joinpath(".mcp.json").read_text(encoding="utf-8")
    after = _pack(runner, project, env)

    assert after == before
    assert any(rel.startswith("build/") for rel in after)
    for rel, data in after.items():
        text = data.decode("utf-8", errors="replace")
        assert not rel.endswith(".mcp.json"), rel
        assert _FIXTURE_TOKEN not in text, rel
        if rel in _MANIFESTS:
            assert ".mcp.json" not in text
            assert "devsrv" not in text
            assert "Authorization" not in text
