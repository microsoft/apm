"""Install checks complete before any lockfile or deployed byte is committed (#4085).

Spec req-pl-002: a blocking check aborts the install before any byte is
written. A package that brings in an org-denied MCP server must leave
``apm.lock.yaml`` and every deployed file exactly as they were, both on a
fresh project and on a project that already holds an earlier install.
``--dry-run`` reaches the same decision from the same plan.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain, stdio_server

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_DENY_POLICY = (
    "name: acme\nversion: 1.0.0\nenforcement: block\n"
    "mcp:\n  deny: ['evil-*']\n  self_defined: allow\n"
)
_TARGET = ("--target", "copilot")


def _write_package(chain: Chain, *, revision: str, mcp: list[dict]) -> None:
    root = chain.project / "pkg"
    manifest = {
        "name": "pkg",
        "version": "1.0.0",
        "description": "commit-order fixture",
        "author": "Fixture",
        "dependencies": {"mcp": mcp},
    }
    (root / ".apm" / "skills" / "pkg-skill").mkdir(parents=True, exist_ok=True)
    (root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (root / ".apm" / "skills" / "pkg-skill" / "SKILL.md").write_text(
        f"---\nname: pkg-skill\ndescription: fixture skill\n---\n# pkg {revision}\n",
        encoding="utf-8",
    )


def _committed_state(project: Path) -> dict[str, str]:
    """Return relpath -> sha256 for the lockfile and every deployed file."""
    state: dict[str, str] = {}
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if rel.parts[0] in {".git", "pkg", "apm_modules", "apm.yml"}:
            continue
        if path.is_file():
            state[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def _chain(tmp_path: Path, binary: Path) -> Chain:
    chain = Chain(tmp_path / "commit-order", binary, dirs=(".github",))
    chain.seed_org_policy(_DENY_POLICY)
    chain.write_manifest([], apm=["./pkg"])
    return chain


def _assert_blocked_without_commit(chain: Chain, before: dict[str, str]) -> str:
    result = chain.run("install", "--trust-transitive-mcp", *_TARGET)
    text = result.stdout + result.stderr
    assert result.returncode == 1, text
    assert "evil-srv" in text, text
    assert _committed_state(chain.project) == before, text
    assert "remain installed" not in text, text
    return text


def test_blocked_transitive_mcp_on_fresh_project_commits_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = _chain(tmp_path, apm_binary_path)
    _write_package(chain, revision="v1", mcp=[stdio_server("evil-srv", "srv")])
    before = _committed_state(chain.project)

    _assert_blocked_without_commit(chain, before)
    assert not (chain.project / "apm.lock.yaml").exists()


def test_blocked_transitive_mcp_keeps_previous_install_byte_identical(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = _chain(tmp_path, apm_binary_path)
    _write_package(chain, revision="v1", mcp=[stdio_server("good-srv", "srv")])
    chain.ok("install", "--trust-transitive-mcp", *_TARGET)
    before = _committed_state(chain.project)
    assert "apm.lock.yaml" in before
    assert any(rel.endswith("pkg-skill/SKILL.md") for rel in before), before

    _write_package(chain, revision="v2", mcp=[stdio_server("evil-srv", "srv")])
    _assert_blocked_without_commit(chain, before)


def test_dry_run_reaches_the_install_decision_for_transitive_mcp(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = _chain(tmp_path, apm_binary_path)
    _write_package(chain, revision="v1", mcp=[stdio_server("evil-srv", "srv")])
    before = _committed_state(chain.project)

    preview = chain.run("install", "--dry-run", "--trust-transitive-mcp", *_TARGET)
    text = preview.stdout + preview.stderr
    assert preview.returncode == 0, text
    assert _committed_state(chain.project) == before, text
    assert not (chain.project / "apm_modules").exists(), text
    assert "Would be blocked by policy" in text and "evil-srv" in text, text

    _assert_blocked_without_commit(chain, before)
