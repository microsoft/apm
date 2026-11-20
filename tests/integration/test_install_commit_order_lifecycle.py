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


# The install-time content audit scans the bytes integrators deployed, so a
# blocking verdict lands after deploy. The source agent below is ASCII-only; the
# Kiro agent integrator decodes the YAML escape and re-serializes it, so the
# right-to-left override exists only in the deployed file.
_AUDIT_TARGET = ("--target", "kiro")
_RLO = "\u202e"
_HIDDEN_DESCRIPTION = "agent \\u202e helper"


def _write_audit_package(chain: Chain, *, revision: str, description: str, extra: bool) -> None:
    root = chain.project / "pkg"
    manifest = {
        "name": "pkg",
        "version": "1.0.0",
        "description": "audit commit-order fixture",
        "author": "Fixture",
    }
    agents = root / ".apm" / "agents"
    skill = root / ".apm" / "skills" / "pkg-skill"
    agents.mkdir(parents=True, exist_ok=True)
    skill.mkdir(parents=True, exist_ok=True)
    (root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (agents / "pkg-agent.agent.md").write_text(
        f'---\ndescription: "{description}"\n---\n# pkg agent {revision}\n', encoding="utf-8"
    )
    (skill / "SKILL.md").write_text(
        f"---\nname: pkg-skill\ndescription: fixture skill\n---\n# pkg skill {revision}\n",
        encoding="utf-8",
    )
    extra_agent = agents / "extra-agent.agent.md"
    if extra:
        extra_agent.write_text(f"# extra agent {revision}\n", encoding="utf-8")
    elif extra_agent.exists():
        extra_agent.unlink()


def _tree_state_of(root: Path, skip: frozenset[str] = frozenset()) -> dict[str, str]:
    """Return relpath -> sha256 (or ``<dir>``) for every path below *root*."""
    state: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] in skip:
            continue
        if path.is_dir():
            state[rel.as_posix()] = "<dir>"
        else:
            state[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def _tree_state(project: Path) -> dict[str, str]:
    """Return the committed project state: everything but sources and modules."""
    return _tree_state_of(project, frozenset({".git", "pkg", "apm_modules", "apm.yml"}))


def _audit_chain(tmp_path: Path, binary: Path, name: str) -> Chain:
    chain = Chain(tmp_path / name, binary)
    chain.ok("experimental", "enable", "external_scanners")
    chain.write_manifest([], apm=["./pkg"])
    return chain


def _deployed_agent(chain: Chain) -> Path:
    found = sorted((chain.project / ".kiro" / "agents").glob("pkg-agent*"))
    assert len(found) == 1, found
    return found[0]


def _assert_audit_block_commits_nothing(chain: Chain, before: dict[str, str]) -> str:
    apm_home = Path(chain.env["APM_HOME"])
    receipts = _tree_state_of(apm_home / "deployment-receipts")
    result = chain.run("install", "--audit", "block", *_AUDIT_TARGET)
    text = result.stdout + result.stderr
    assert result.returncode == 1, text
    assert "Install-time audit blocked" in text, text
    assert _tree_state(chain.project) == before, text
    assert _tree_state_of(apm_home / "deployment-receipts") == receipts, text
    assert not (apm_home / "install-snapshots").exists(), text
    text.encode("ascii")
    return text


def test_audit_block_keeps_previous_install_byte_identical(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = _audit_chain(tmp_path, apm_binary_path, "audit-previous")
    _write_audit_package(chain, revision="v1", description="agent helper", extra=False)
    chain.ok("install", "--audit", "block", *_AUDIT_TARGET)
    before = _tree_state(chain.project)
    assert "apm.lock.yaml" in before
    assert _RLO not in _deployed_agent(chain).read_text(encoding="utf-8")

    _write_audit_package(chain, revision="v2", description=_HIDDEN_DESCRIPTION, extra=True)
    _assert_audit_block_commits_nothing(chain, before)


def test_audit_block_on_fresh_project_commits_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = _audit_chain(tmp_path, apm_binary_path, "audit-fresh")
    _write_audit_package(chain, revision="v1", description=_HIDDEN_DESCRIPTION, extra=True)
    before = _tree_state(chain.project)

    _assert_audit_block_commits_nothing(chain, before)
    assert not (chain.project / "apm.lock.yaml").exists()
    assert not (chain.project / ".kiro").exists()


def test_audit_block_sees_hidden_character_only_in_transformed_output(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = _audit_chain(tmp_path, apm_binary_path, "audit-transform")
    _write_audit_package(chain, revision="v1", description=_HIDDEN_DESCRIPTION, extra=False)
    for source in (chain.project / "pkg").rglob("*"):
        if source.is_file():
            source.read_bytes().decode("ascii")

    unaudited = chain.ok("install", "--no-audit", *_AUDIT_TARGET)
    assert _RLO in _deployed_agent(chain).read_text(encoding="utf-8"), unaudited.stdout
    before = _tree_state(chain.project)

    _write_audit_package(chain, revision="v2", description=_HIDDEN_DESCRIPTION, extra=True)
    _assert_audit_block_commits_nothing(chain, before)
