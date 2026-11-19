"""Global reinstall adopts APM's own rendered agents (#3722, 0.36.1).

With root-local agents in ``~/.apm/agents`` no user lockfile records the
deployments, so content adoption is the only idempotency path. Adoption must
compare the RENDERED bytes: Codex writes TOML, Kiro rewrites frontmatter and
``copy_agent`` rewrites context links. A ``--force`` install followed by a
plain ``install -g`` must therefore report nothing skipped and change nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration._harness_output_kit import HarnessScene, describe, flat

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_AGENT = "---\nname: kit-agent\ndescription: Research helper\n---\n\nAgent body.\n"
_SKIPPED = re.compile(r"\b\d+ files? skipped\b")
_DEPLOYED = {
    "codex": ".codex/agents/kit-agent.toml",
    "kiro": ".kiro/agents/kit-agent.md",
    "copilot": ".copilot/agents/kit-agent.agent.md",
    "claude": ".claude/agents/kit-agent.md",
}


@pytest.fixture
def scene(tmp_path: Path, apm_binary_path: Path) -> Iterator[HarnessScene]:
    result = HarnessScene.create(tmp_path / "scene", apm_binary_path, signal_dirs=())
    root = result.isolated.config_root
    (root / "apm.yml").write_text(
        "name: user-profile\nversion: 1.0.0\ndependencies:\n  apm: []\n", encoding="utf-8"
    )
    try:
        yield result
    finally:
        result.close()


def _reinstall(scene: HarnessScene, targets: tuple[str, ...]) -> None:
    cwd = scene.isolated.work_root / "cwd"
    cwd.mkdir(exist_ok=True)
    common = ("install", "-g", "--no-policy", "--target", ",".join(targets))
    forced = scene.runner.run((*common, "--force"), cwd=cwd, env=scene.env)
    assert forced.returncode == 0, describe(forced)
    paths = [scene.home / _DEPLOYED[target] for target in targets]
    assert all(path.is_file() for path in paths), [str(p) for p in paths if not p.is_file()]
    before = {path: path.read_bytes() for path in paths}

    again = scene.runner.run(common, cwd=cwd, env=scene.env)

    assert again.returncode == 0, describe(again)
    assert not _SKIPPED.search(flat(again.stdout + again.stderr)), describe(again)
    assert {path: path.read_bytes() for path in paths} == before


def test_global_reinstall_adopts_transformed_agents_for_every_target(
    scene: HarnessScene,
) -> None:
    agents = scene.isolated.config_root / "agents"
    agents.mkdir()
    (agents / "kit-agent.agent.md").write_text(_AGENT, encoding="utf-8")

    _reinstall(scene, ("codex", "kiro", "copilot", "claude"))


def test_global_reinstall_adopts_agents_whose_links_were_rewritten(
    scene: HarnessScene,
) -> None:
    root = scene.isolated.config_root
    (root / "agents").mkdir()
    (root / ".apm" / "context").mkdir(parents=True)
    (root / ".apm" / "context" / "kit.context.md").write_text(
        "---\ndescription: kit context\n---\nContext.\n", encoding="utf-8"
    )
    (root / "agents" / "kit-agent.agent.md").write_text(
        _AGENT + "See [context](../.apm/context/kit.context.md).\n", encoding="utf-8"
    )

    _reinstall(scene, ("copilot", "claude"))


_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "upgrade_0361" / "t3"


@pytest.mark.parametrize("label", ["v0.35.1", "v0.36.0"])
def test_upgrade_adopts_agents_rendered_by_an_earlier_release(
    scene: HarnessScene, label: str
) -> None:
    """Agents a tagged release deployed with ``-g`` are adopted, not skipped."""
    source = _FIXTURES / label / "user-agents" / "home"
    for path in sorted(source.rglob("*")):
        if path.is_file():
            target = scene.home / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    paths = [scene.home / relative for relative in _DEPLOYED.values()]
    before = {path: path.read_bytes() for path in paths}
    cwd = scene.isolated.work_root / "cwd"
    cwd.mkdir()

    result = scene.runner.run(
        ("install", "-g", "--no-policy", "--target", ",".join(_DEPLOYED)),
        cwd=cwd,
        env=scene.env,
    )

    assert result.returncode == 0, describe(result)
    assert not _SKIPPED.search(flat(result.stdout + result.stderr)), describe(result)
    assert {path: path.read_bytes() for path in paths} == before
