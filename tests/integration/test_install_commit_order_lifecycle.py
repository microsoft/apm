"""Install checks complete before any lockfile or deployed byte is committed (#4085).

Spec req-pl-002: a blocking check aborts the install before any byte is
written. A package that brings in an org-denied MCP server must leave
``apm.lock.yaml`` and every deployed file exactly as they were, both on a
fresh project and on a project that already holds an earlier install.
``--dry-run`` reaches the same decision from the same plan.
"""

from __future__ import annotations

import hashlib
import re
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


_ELAPSED = re.compile(r"after \d+(\.\d+)?s")


def _refusal_lines(text: str) -> list[str]:
    """Return the error lines of a run, with elapsed time masked."""
    return sorted(
        _ELAPSED.sub("after <t>", line.strip()) for line in text.splitlines() if "[x]" in line
    )


@pytest.mark.parametrize(
    "entry", [("install", "--mcp"), ("mcp", "install")], ids=["install-mcp", "mcp-install"]
)
def test_direct_mcp_entry_reaches_the_install_policy_verdict(
    tmp_path: Path, apm_binary_path: Path, entry: tuple[str, str]
) -> None:
    """A denied ``--mcp`` server is refused by the commit gate, like a declared one.

    The direct entry writes nothing (apm.yml, apm.lock.yaml and every MCP
    config keep their bytes) and renders the same refusal, with the same exit
    code, as ``apm install`` with that server declared in apm.yml.
    """
    chain = Chain(tmp_path / "direct-mcp", apm_binary_path, dirs=(".github",))
    chain.seed_org_policy(_DENY_POLICY)
    chain.write_manifest([stdio_server("good-srv", "good")])
    chain.ok("install", *_TARGET)
    manifest = (chain.project / "apm.yml").read_bytes()
    before = _committed_state(chain.project)
    assert "apm.lock.yaml" in before and ".mcp.json" in before, before

    direct = chain.run(*entry, "evil-srv", *_TARGET, "--", "npx", "evil")
    direct_text = direct.stdout + direct.stderr
    assert direct.returncode == 1, direct_text
    assert (chain.project / "apm.yml").read_bytes() == manifest, direct_text
    assert _committed_state(chain.project) == before, direct_text

    chain.write_manifest([stdio_server("good-srv", "good"), stdio_server("evil-srv", "evil")])
    declared = chain.run("install", *_TARGET)
    declared_text = declared.stdout + declared.stderr
    assert declared.returncode == 1, declared_text
    assert _committed_state(chain.project) == before, declared_text

    assert "evil-srv" in direct_text, direct_text
    assert _refusal_lines(direct_text) == _refusal_lines(declared_text), (
        direct_text,
        declared_text,
    )
