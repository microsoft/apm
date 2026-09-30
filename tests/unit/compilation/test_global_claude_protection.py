"""Component contracts for native delivery proof and user-root cleanup."""

from pathlib import Path

import pytest

from apm_cli.compilation.build_id import has_valid_build_id, stabilize_build_id
from apm_cli.compilation.constants import AGENTS_MD_GENERATED_MARKER, BUILD_ID_PLACEHOLDER
from apm_cli.compilation.root_context_protection import (
    clean_redundant_user_root,
    protected_user_root_status,
)
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = pytest.mark.component


def _generated(body: str = "Keep this instruction.") -> str:
    return stabilize_build_id(f"{AGENTS_MD_GENERATED_MARKER}\n{BUILD_ID_PLACEHOLDER}\n\n{body}\n")


@pytest.mark.parametrize(
    "content",
    [
        "Personal guidance.\n",
        _generated() + "User addition.\n",
        _generated().replace("Keep this", "Changed"),
        f"{AGENTS_MD_GENERATED_MARKER}\nNo build ID.\n",
        _generated() + _generated(),
    ],
)
def test_cleanup_preserves_unverified_content(tmp_path: Path, content: str) -> None:
    """A marker alone, stale hash or ambiguous hash never proves ownership."""
    path = tmp_path / "CLAUDE.md"
    path.write_text(content, encoding="utf-8")
    before = path.read_bytes()
    assert protected_user_root_status(path, content) is not None
    assert clean_redundant_user_root(path, tmp_path, _generated(), dry_run=False).startswith(
        "skipped-"
    )
    assert path.read_bytes() == before


def test_cleanup_requires_current_full_coverage(tmp_path: Path) -> None:
    """An intact but different prior generation must not be silently deleted."""
    path = tmp_path / "CLAUDE.md"
    prior = _generated("Prior instruction no longer installed.")
    path.write_text(prior, encoding="utf-8")
    assert has_valid_build_id(prior)
    assert (
        clean_redundant_user_root(path, tmp_path, _generated(), dry_run=False) == "skipped-modified"
    )
    assert path.read_text(encoding="utf-8") == prior


def test_cleanup_dry_run_then_live(tmp_path: Path) -> None:
    """Preview and live cleanup share the same exact-content protection."""
    path = tmp_path / "CLAUDE.md"
    content = _generated()
    path.write_text(content, encoding="utf-8")
    assert has_valid_build_id(content)
    assert clean_redundant_user_root(path, tmp_path, content, dry_run=True) == "would-remove"
    assert path.read_text(encoding="utf-8") == content
    assert clean_redundant_user_root(path, tmp_path, content, dry_run=False) == "removed"
    assert not path.exists()


def test_cleanup_unlink_error_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filesystem failures must reach the caller's error reporting."""
    path = tmp_path / "CLAUDE.md"
    content = _generated()
    path.write_text(content, encoding="utf-8")

    def denied(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(PermissionError, match="cleanup denied"):
        clean_redundant_user_root(path, tmp_path, content, dry_run=False)
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.windows_compat
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_native_comparison_uses_install_plan_without_mutation(tmp_path: Path, newline: str) -> None:
    """Changed native content cannot suppress a matching source basename."""
    source = tmp_path / "source" / "style.instructions.md"
    source.parent.mkdir()
    source.write_text("---\ndescription: Test\n---\nKeep this instruction.\n", encoding="utf-8")
    root = tmp_path / "claude"
    rule = root / "rules" / "style.md"
    integrator = InstructionIntegrator()
    profile = KNOWN_TARGETS["claude"]
    assert not integrator.deployed_rule_matches(source, profile, root)
    assert not root.exists()
    rule.parent.mkdir(parents=True)
    rule.write_text("A different instruction.\n", encoding="utf-8")
    assert not integrator.deployed_rule_matches(source, profile, root)
    rule.write_bytes(f"Keep this instruction.{newline}".encode("ascii"))
    assert integrator.deployed_rule_matches(source, profile, root)
    rule.write_text("---\npaths: ['src/**']\n---\nKeep this instruction.\n", encoding="utf-8")
    assert not integrator.deployed_rule_matches(source, profile, root)
