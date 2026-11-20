"""Install checks complete before any lockfile or deployed byte is committed (#4085).

Spec req-pl-002: a blocking check aborts the install before any byte is
written. A package that brings in an org-denied MCP server must leave
``apm.lock.yaml`` and every deployed file exactly as they were, both on a
fresh project and on a project that already holds an earlier install.
``--dry-run`` reaches the same decision from the same plan.
"""

from __future__ import annotations

import hashlib
import json
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


def _assert_lock_matches_disk(project: Path) -> None:
    """Every deployed path the on-disk lockfile records is present on disk."""
    lock_path = project / "apm.lock.yaml"
    if not lock_path.exists():
        return
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    claims = {
        path for dep in lock.get("dependencies") or [] for path in dep.get("deployed_files") or []
    }
    claims.update(path for path in lock.get("local_deployed_files") or [])
    claims.update(row["value"] for row in lock.get("deployments") or [])
    missing = sorted(path for path in claims if not (project / path).exists())
    assert missing == [], f"lockfile claims files not on disk: {missing}"


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
    assert "Deployed files restored to their previous state." in text, text
    assert "native target" not in text, text
    _assert_lock_matches_disk(chain.project)
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


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
def test_direct_mcp_entry_records_exactly_what_it_wrote(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    """``install --mcp`` commits one lockfile that records the server it wrote.

    The lockfile names the server for exactly the target whose config holds
    it, and repeating the entry changes no bytes, at project and user scope.
    """
    chain = Chain(tmp_path / "direct-mcp-lock", apm_binary_path, dirs=(".github",))
    chain.write_manifest([])
    scope = ("--global",) if user_scope else ()
    root = chain.isolated.config_root if user_scope else chain.project
    config = (
        chain.isolated.home / ".copilot" / "mcp-config.json"
        if user_scope
        else chain.project / ".mcp.json"
    )

    first = chain.ok("install", "--mcp", "entry-srv", *scope, *_TARGET, "--", "npx", "entry")
    lock = yaml.safe_load((root / "apm.lock.yaml").read_text(encoding="utf-8"))
    written = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    text = first.stdout + first.stderr
    assert lock["mcp_servers"] == ["entry-srv"], text
    assert lock["mcp_target_servers"] == {"copilot": ["entry-srv"]}, text
    assert set(written) == {"entry-srv"}, text
    before = ((root / "apm.lock.yaml").read_bytes(), config.read_bytes())

    chain.ok("install", "--mcp", "entry-srv", *scope, *_TARGET, "--", "npx", "entry")
    assert ((root / "apm.lock.yaml").read_bytes(), config.read_bytes()) == before
    if not user_scope:
        chain.ok("audit", "--ci")


def test_failed_install_removes_the_target_root_it_created(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4159: a failed install leaves no empty target root it created itself."""
    chain = Chain(tmp_path / "created-root", apm_binary_path)
    chain.seed_org_policy(_DENY_POLICY)
    chain.write_manifest([], apm=["./pkg"])
    _write_package(chain, revision="v1", mcp=[stdio_server("evil-srv", "srv")])
    before = _tree_state(chain.project)
    assert not (chain.project / ".github").exists()

    result = chain.run("install", "--trust-transitive-mcp", *_TARGET)
    text = result.stdout + result.stderr
    assert result.returncode == 1, text
    assert "evil-srv" in text, text
    assert not (chain.project / ".github").exists(), text
    assert _tree_state(chain.project) == before, text
    assert not (chain.project / "apm.lock.yaml").exists(), text
