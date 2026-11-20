"""Audit and install agree on compilation-target policy for every target spelling (#3732).

Promise under test (0.36.1 T2 brief, decisions A1, B1, D1):

* ``apm audit --ci`` evaluates the canonical target set the manifest declares,
  whatever the spelling: ``targets:`` list, ``target:`` scalar, comma-separated
  ``target:`` or ``target:`` list.
* Aliases are compared by primitive profile on both sides of the policy
  (``vscode`` and ``agents`` are ``copilot``), and ``all`` in ``allow`` means
  unrestricted.
* A singular ``target: all`` expands to the install set; policy evaluates that
  expansion and never the literal ``all``. A plural ``targets: [all]`` folds to
  auto-detect.
* ``apm install`` reaches the same verdict (it also judges auto-detected and
  ``--target`` selections). A blocked install writes nothing, and audit never
  writes to the project.
* A per-dependency ``targets:`` subset is not a package target and is never
  judged by compilation-target policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.integration._policy_scope_kit import output, tree_snapshot
from tests.integration._target_policy_kit import (
    block_policy,
    changed_paths,
    check_text,
    names_word,
    quoted_tokens,
    target_policy_world,
    violation_text,
)
from tests.utils.local_github_https_fixture import openssl_executable

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

CLAUDE_COPILOT = {"allow": ["claude", "copilot"]}


@dataclass(frozen=True)
class GateRow:
    """One manifest declaration judged by one compilation-target policy."""

    declaration: dict
    target_policy: dict
    passes: bool
    install_args: tuple[str, ...] = ()
    install_passes: bool | None = None
    dirs: tuple[str, ...] = ()
    dependencies: tuple[object, ...] | None = None
    # Canonical names the failure message must quote, and tokens it must not.
    names: frozenset[str] = field(default_factory=frozenset)
    never_names: frozenset[str] = field(default_factory=frozenset)


def _dep(name: str, *subset: str) -> dict:
    return {"git": f"https://github.com/contoso/{name}", "ref": "v1.0.0", "targets": list(subset)}


ROWS: dict[str, GateRow] = {
    "plural-list-outside-allow-list-is-rejected": GateRow(
        {"targets": ["claude", "cursor"]},
        CLAUDE_COPILOT,
        passes=False,
        names=frozenset({"cursor"}),
    ),
    "plural-list-inside-allow-list-is-accepted": GateRow(
        {"targets": ["claude", "copilot"]}, CLAUDE_COPILOT, passes=True
    ),
    "comma-separated-target-inside-allow-list-is-accepted": GateRow(
        {"target": "claude,copilot"}, CLAUDE_COPILOT, passes=True
    ),
    "comma-separated-target-names-only-the-disallowed-harness": GateRow(
        {"target": "claude,cursor"},
        CLAUDE_COPILOT,
        passes=False,
        names=frozenset({"cursor"}),
        never_names=frozenset({"claude,cursor"}),
    ),
    "singular-target-list-inside-allow-list-is-accepted": GateRow(
        {"target": ["claude", "copilot"]}, CLAUDE_COPILOT, passes=True
    ),
    "plural-vscode-alias-matches-copilot-allow": GateRow(
        {"targets": ["vscode"]}, CLAUDE_COPILOT, passes=True, never_names=frozenset({"vscode"})
    ),
    "singular-vscode-alias-matches-copilot-allow": GateRow(
        {"target": "vscode"}, CLAUDE_COPILOT, passes=True
    ),
    "singular-agents-alias-matches-copilot-allow": GateRow(
        {"target": "agents"}, CLAUDE_COPILOT, passes=True
    ),
    "policy-vscode-alias-allows-copilot-manifest": GateRow(
        {"targets": ["copilot"]}, {"allow": ["vscode"]}, passes=True
    ),
    "singular-all-is-judged-as-its-expansion": GateRow(
        {"target": "all"},
        CLAUDE_COPILOT,
        passes=False,
        names=frozenset({"cursor"}),
        never_names=frozenset({"all"}),
    ),
    "singular-all-passes-an-unrestricted-allow-all-policy": GateRow(
        {"target": "all"}, {"allow": ["all"]}, passes=True
    ),
    "plural-all-folds-to-auto-detect-and-passes": GateRow(
        {"targets": ["all"]}, CLAUDE_COPILOT, passes=True, dirs=(".claude",)
    ),
    "allow-all-policy-accepts-plural-claude": GateRow(
        {"targets": ["claude"]}, {"allow": ["all"]}, passes=True
    ),
    "allow-all-policy-accepts-singular-claude": GateRow(
        {"target": "claude"}, {"allow": ["all"]}, passes=True
    ),
    "enforced-vscode-alias-is-satisfied-by-copilot": GateRow(
        {"targets": ["copilot"]}, {"enforce": "vscode"}, passes=True
    ),
    "enforced-copilot-is-satisfied-by-vscode-alias": GateRow(
        {"target": "vscode"}, {"enforce": "copilot"}, passes=True
    ),
    "enforced-target-missing-from-plural-list-is-rejected": GateRow(
        {"targets": ["cursor"]}, {"enforce": "claude"}, passes=False, names=frozenset({"claude"})
    ),
    "auto-detected-target-is-judged-by-install-only": GateRow(
        {}, CLAUDE_COPILOT, passes=True, install_passes=False, dirs=(".cursor",)
    ),
    "cli-target-override-is-judged-by-install-only": GateRow(
        {"targets": ["claude"]},
        CLAUDE_COPILOT,
        passes=True,
        install_args=("--target", "cursor"),
        install_passes=False,
    ),
    "dependency-target-subset-is-not-a-package-target": GateRow(
        {"targets": ["claude", "copilot"]},
        CLAUDE_COPILOT,
        passes=True,
        dependencies=(_dep("pkg-a", "claude"), _dep("pkg-b", "cursor")),
        never_names=frozenset({"cursor"}),
    ),
    "package-targets-are-judged-beside-dependency-subsets": GateRow(
        {"targets": ["claude", "cursor"]},
        CLAUDE_COPILOT,
        passes=False,
        dependencies=(_dep("pkg-a", "claude"), _dep("pkg-b", "claude")),
        names=frozenset({"cursor"}),
    ),
}


@pytest.mark.parametrize("row_id", list(ROWS))
def test_audit_and_install_agree_on_target_policy(
    row_id: str, tmp_path: Path, apm_binary_path: Path
) -> None:
    row = ROWS[row_id]
    install_passes = row.passes if row.install_passes is None else row.install_passes
    policy = block_policy(row.target_policy)
    with target_policy_world(tmp_path, apm_binary_path, policy, packages=("pkg-a", "pkg-b")) as tw:
        deps = list(row.dependencies) if row.dependencies is not None else ["contoso/pkg-a#v1.0.0"]
        project = tw.consumer("consumer", row.declaration, dependencies=deps, dirs=row.dirs)

        tw.run(project, "install", "--no-policy", step="1-setup-no-policy")
        manifest = (project / "apm.yml").read_bytes()
        lockfile = (project / "apm.lock.yaml").read_bytes()

        unrestricted = "all" in (row.target_policy.get("allow") or ())
        result, check = tw.audit_read_only(project, step="2-audit", unrestricted=unrestricted)
        assert check["passed"] is row.passes, (
            f"audit compilation-target verdict {check['passed']} != {row.passes}: {check}"
        )
        assert result.returncode == (0 if row.passes else 1), output(result)
        if not row.passes:
            text = check_text(check)
            missing = sorted(name for name in row.names if not names_word(text, name))
            assert not missing, f"audit must name {missing}: {text}"
            quoted = quoted_tokens(text)
            assert not (row.never_names & quoted), f"audit must not quote {row.never_names}: {text}"
        else:
            assert not (row.never_names & quoted_tokens(check_text(check))), check

        before = tree_snapshot(project)
        result = tw.run(project, "install", *row.install_args, step="3-install", expected=(0, 1))
        assert result.returncode == (0 if install_passes else 1), output(result)
        if not install_passes:
            after = tree_snapshot(project)
            assert after == before, f"blocked install wrote: {changed_paths(before, after)}"
            violation = violation_text(result)
            assert violation, f"no compilation-target violation:\n{output(result)}"
            missing = sorted(name for name in row.names if not names_word(violation, name))
            assert not missing, f"install must name {missing}: {violation}"
            assert not (row.never_names & quoted_tokens(violation)), violation
        assert (project / "apm.yml").read_bytes() == manifest, "install rewrote apm.yml"
        if not install_passes:
            assert (project / "apm.lock.yaml").read_bytes() == lockfile


def test_extends_chain_intersects_canonical_target_names(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Parent ``allow: [vscode, claude]`` and child ``allow: [copilot]`` allow copilot."""
    leaf = block_policy({"allow": ["copilot"]}, extends="contoso/enterprise-policy")
    parent = block_policy({"allow": ["vscode", "claude"]})
    with target_policy_world(
        tmp_path, apm_binary_path, leaf, extra_policy_repos={"enterprise-policy": parent}
    ) as tw:
        allowed = tw.consumer("allowed", {"targets": ["copilot"]})
        tw.run(allowed, "install", "--no-policy", step="1-setup")
        _result, check = tw.audit_read_only(allowed, step="2-audit-copilot")
        assert check["passed"] is True, check
        tw.run(allowed, "install", step="3-install-copilot")

        tightened = tw.consumer("tightened", {"targets": ["claude"]})
        tw.run(tightened, "install", "--no-policy", step="4-setup")
        _result, check = tw.audit_read_only(tightened, step="5-audit-claude")
        assert check["passed"] is False, check
        assert names_word(check_text(check), "claude"), check
        before = tree_snapshot(tightened)
        tw.run(tightened, "install", step="6-install-claude", expected=1)
        assert tree_snapshot(tightened) == before


def test_mcp_add_judges_alias_targets_canonically(tmp_path: Path, apm_binary_path: Path) -> None:
    """``install --mcp`` accepts ``targets: [vscode]`` under ``allow: [claude, copilot]``."""
    mcp_args = ("--mcp", "srv", "--transport", "http", "--url", "https://mcp.example.com/mcp")
    with target_policy_world(tmp_path, apm_binary_path, block_policy(CLAUDE_COPILOT)) as tw:
        alias = tw.consumer("alias", {"targets": ["vscode"]}, dependencies=[])
        result = tw.run(alias, "install", *mcp_args, step="1-mcp-alias", expected=(0, 1))
        assert result.returncode == 0, output(result)
        # The alias selects copilot exactly, including its .mcp.json destination.
        assert (alias / ".mcp.json").is_file(), "vscode alias missed the copilot MCP destination"
        assert not (alias / ".vscode" / "mcp.json").exists()
        assert not (alias / ".github" / "mcp.json").exists()

        outside = tw.consumer("outside", {"targets": ["claude", "cursor"]}, dependencies=[])
        before = tree_snapshot(outside)
        result = tw.run(outside, "install", *mcp_args, step="2-mcp-outside", expected=1)
        assert tree_snapshot(outside) == before, "blocked --mcp install wrote files"
        assert "cursor" in output(result), output(result)


def test_local_bundle_install_and_audit_agree(tmp_path: Path, apm_binary_path: Path) -> None:
    """A bundle consumer is judged by the same canonical set by install and audit."""
    with target_policy_world(tmp_path, apm_binary_path, block_policy(CLAUDE_COPILOT)) as tw:
        producer = tw.consumer("producer", {"targets": ["claude"]})
        tw.run(producer, "install", step="1-producer-install")
        tw.run(producer, "pack", "-o", "dist", step="2-pack")
        bundle = producer / "dist" / "producer-1.0.0"
        assert (bundle / "plugin.json").is_file()

        alias = tw.consumer("alias", {"targets": ["vscode"]}, dependencies=[])
        _result, check = tw.audit_read_only(alias, step="3-audit-alias")
        assert check["passed"] is True, check
        tw.run(alias, "install", str(bundle), step="4-bundle-alias")
        assert (alias / ".agents" / "skills" / "pkg-a-skill" / "SKILL.md").is_file()

        outside = tw.consumer("outside", {"targets": ["claude", "cursor"]}, dependencies=[])
        result, check = tw.audit_read_only(outside, step="5-audit-outside")
        assert check["passed"] is False, check
        assert result.returncode == 1, output(result)
        before = tree_snapshot(outside)
        tw.run(outside, "install", str(bundle), step="6-bundle-outside", expected=1)
        assert tree_snapshot(outside) == before, "blocked bundle install wrote files"
