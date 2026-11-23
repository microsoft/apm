"""A Claude root at HOME admits only its primitive directories (#5096).

PROMISE: P-INV-I5 -- with CLAUDE_CONFIG_DIR set to $HOME the Claude root scopes
to ".", and the deploy-path gate admits the directories Claude primitives
deploy into while every other path under HOME, "./"-spelled, "..", absolute and
symlinked destinations stay refused (source: #5096 scope record).
owner: integration/targets.py get_integration_prefixes,
integration/base_integrator.py BaseIntegrator.validate_deploy_path
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS, get_integration_prefixes

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I5")]


@pytest.fixture
def claude_at_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(name, str(home))
    claude = KNOWN_TARGETS["claude"].for_scope(user_scope=True)
    assert claude.root_dir == "."
    return home, claude


def test_the_root_contributes_its_primitive_directories_not_itself(claude_at_home) -> None:
    _home, claude = claude_at_home

    prefixes = get_integration_prefixes(targets=[claude])

    assert set(prefixes) == {"rules/", "agents/", "commands/", "skills/", "hooks/"}


def test_a_claude_root_at_home_admits_its_primitive_directories(claude_at_home) -> None:
    home, claude = claude_at_home

    assert BaseIntegrator.validate_deploy_path("skills/demo/SKILL.md", home, targets=[claude])
    assert BaseIntegrator.validate_deploy_path("skills/demo/SKILL.md", home, user_scope=True)
    assert BaseIntegrator.validate_deploy_path("agents/helper.md", home, targets=[claude])


@pytest.mark.parametrize(
    "rel_path",
    [
        "./../x",
        "../x",
        "./skills/demo/SKILL.md",
        ".//skills/demo/SKILL.md",
        "././skills/demo/SKILL.md",
        "./.",
        "skills/./demo/SKILL.md",
        "skills/.",
        "skills/demo/.",
        ".ssh/authorized_keys",
        "settings.json",
    ],
)
def test_a_claude_root_at_home_admits_nothing_else(claude_at_home, rel_path: str) -> None:
    home, claude = claude_at_home

    assert not BaseIntegrator.validate_deploy_path(rel_path, home, targets=[claude])
    assert not BaseIntegrator.validate_deploy_path(rel_path, home, user_scope=True)


def test_a_claude_root_at_home_refuses_absolute_and_symlink_escapes(
    claude_at_home, tmp_path: Path
) -> None:
    home, claude = claude_at_home
    outside = tmp_path / "outside"
    (outside / "demo").mkdir(parents=True)
    try:
        (home / "skills").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")

    assert not BaseIntegrator.validate_deploy_path(
        str(outside / "demo" / "SKILL.md"), home, targets=[claude]
    )
    assert not BaseIntegrator.validate_deploy_path("skills/demo/SKILL.md", home, targets=[claude])
