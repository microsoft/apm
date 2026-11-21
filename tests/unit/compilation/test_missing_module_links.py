"""Missing apm_modules link targets in deployed files (#3143)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.compilation.link_resolver import missing_module_link_targets
from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I7")]

_AGENT = ".github/agents/helper.agent.md"
_TARGET = "apm_modules/acme/pkg/.apm/context/shared.context.md"


def _project(tmp_path: Path, body: str, *, agent: str = _AGENT, local: bool = False) -> Path:
    root = tmp_path / "project"
    path = root / agent
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    dependency = LockedDependency(repo_url="acme/pkg")
    lockfile = LockFile(dependencies={"acme/pkg": dependency})
    if local:
        lockfile.local_deployed_files = [agent]
    else:
        dependency.deployed_files = [agent]
    lockfile.write(root / "apm.lock.yaml")
    return root


def _materialize(root: Path) -> None:
    (root / _TARGET).parent.mkdir(parents=True)
    (root / _TARGET).write_text("# facts\n", encoding="utf-8")


@pytest.mark.parametrize(
    "link",
    [
        f"[facts](../../{_TARGET})",
        f'[facts](../../{_TARGET} "Facts")',
        f"[facts](<../../{_TARGET}>)",
        f"[facts](../../{_TARGET}#section)",
    ],
)
def test_a_link_into_apm_modules_is_reported_only_while_its_target_is_missing(
    tmp_path: Path, link: str
) -> None:
    root = _project(tmp_path, f"Read {link}.\n")
    assert missing_module_link_targets(root) == [(_AGENT, _TARGET)]
    _materialize(root)
    assert missing_module_link_targets(root) == []


@pytest.mark.parametrize(
    "link",
    [
        "[docs](//docs.example.com/apm_modules/guide.md)",
        "[docs](https://example.com/apm_modules/guide.md)",
        "[local](../context/notes.md)",
    ],
)
def test_urls_and_links_outside_apm_modules_are_not_reported(tmp_path: Path, link: str) -> None:
    root = _project(tmp_path, f"See {link}.\n")
    assert missing_module_link_targets(root) == []


def test_a_workspace_deployed_file_is_scanned_and_a_package_filter_narrows(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, f"Read [facts](../../{_TARGET}).\n", local=True)
    assert missing_module_link_targets(root) == [(_AGENT, _TARGET)]
    assert missing_module_link_targets(root, "acme/pkg") == []


def test_a_codex_toml_agent_is_scanned_through_its_instructions(tmp_path: Path) -> None:
    agent = ".codex/agents/helper.toml"
    body = (
        'name = "helper"\ndescription = "d"\n'
        f'developer_instructions = "Read [facts](../../{_TARGET})."\n'
    )
    root = _project(tmp_path, body, agent=agent)
    assert missing_module_link_targets(root) == [(agent, _TARGET)]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symbolic links")
def test_a_claim_beneath_a_symlinked_parent_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "helper.agent.md").write_text(f"[facts](../../{_TARGET})\n", encoding="utf-8")
    root = _project(tmp_path, "placeholder\n")
    agents = root / ".github" / "agents"
    (agents / "helper.agent.md").unlink()
    agents.rmdir()
    try:
        os.symlink(outside, agents, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    assert missing_module_link_targets(root) == []
