"""Behavioural mutation evidence for resolve_user_invocable.

Direct cases fail if the resolver's branches are inverted, dropped, or
replaced. Consumer cases drive the real parser and the Codex agent
integrator, which look the resolver up by name. The mutant tests replace
that name and show those same consumer expectations no longer hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import apm_cli.integration.agent_integrator as agent_integrator_mod
import apm_cli.primitives.models as models_mod
import apm_cli.primitives.parser as parser_mod
from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.primitives.models import Chatmode, resolve_user_invocable
from apm_cli.primitives.parser import parse_primitive_file
from apm_cli.utils.diagnostics import (
    CATEGORY_AGENT_LOSSY_COMPILATION,
    Diagnostic,
    DiagnosticCollector,
)


def test_absent_defaults_to_user_invocable() -> None:
    """A missing field, including an explicit null, stays user-invocable."""
    assert resolve_user_invocable({}) is True
    assert resolve_user_invocable({"description": "Agent"}) is True
    assert resolve_user_invocable({"user-invocable": None}) is True


def test_boolean_user_invocable_passes_through() -> None:
    """Boolean user-invocable values are returned unchanged."""
    assert resolve_user_invocable({"user-invocable": False}) is False
    assert resolve_user_invocable({"user-invocable": True}) is True


def test_visibility_internal_is_programmatic_only_alias() -> None:
    """Only the internal visibility token aliases to not user-invocable."""
    assert resolve_user_invocable({"visibility": "internal"}) is False
    assert resolve_user_invocable({"visibility": " Internal "}) is False
    assert resolve_user_invocable({"visibility": "INTERNAL"}) is False
    assert resolve_user_invocable({"visibility": "public"}) is True
    # Non-string visibility is not the alias and must not be stringified.
    assert resolve_user_invocable({"visibility": True}) is True
    assert resolve_user_invocable({"visibility": ["internal"]}) is True
    assert resolve_user_invocable({"visibility": 0}) is True


def test_canonical_field_wins_when_both_are_set() -> None:
    """user-invocable wins over visibility, including quoted tokens."""
    assert resolve_user_invocable({"user-invocable": True, "visibility": "internal"}) is True
    assert resolve_user_invocable({"user-invocable": False, "visibility": "public"}) is False
    assert resolve_user_invocable({"user-invocable": "false", "visibility": "public"}) is False
    assert resolve_user_invocable({"user-invocable": "true", "visibility": "internal"}) is True


def test_string_and_non_bool_values_follow_coercion() -> None:
    """Quoted falsy tokens and non-bool values use the documented coercion."""
    falsy_tokens = ("false", "no", "0", "off", "False", " OFF ")
    for token in falsy_tokens:
        assert resolve_user_invocable({"user-invocable": token}) is False, token
    truthy_tokens = ("true", "yes", "1", "on", "TRUE", "", "internal")
    for token in truthy_tokens:
        assert resolve_user_invocable({"user-invocable": token}) is True, token
    assert resolve_user_invocable({"user-invocable": 0}) is False
    assert resolve_user_invocable({"user-invocable": 1}) is True
    assert resolve_user_invocable({"user-invocable": 0.0}) is False
    assert resolve_user_invocable({"user-invocable": []}) is False
    assert resolve_user_invocable({"user-invocable": ["x"]}) is True
    # Explicit null is absent, so the visibility alias still applies.
    assert resolve_user_invocable({"user-invocable": None, "visibility": "internal"}) is False


def _assert_parser_user_invocable(directory: Path, frontmatter: str, expected: bool) -> None:
    """Parse one agent and assert the stored resolved value."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "sample.agent.md"
    path.write_text(f"---\n{frontmatter}\n---\n\n# Agent\n\nBody.\n", encoding="utf-8")
    primitive = parse_primitive_file(path)
    assert isinstance(primitive, Chatmode)
    assert primitive.user_invocable is expected


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ("description: Default agent", True),
        ("description: Internal\nuser-invocable: false", False),
        ("description: Public\nuser-invocable: true", True),
        ("description: Aliased\nvisibility: internal", False),
        ("description: Mixed\nuser-invocable: true\nvisibility: internal", True),
        ("description: Mixed off\nuser-invocable: false\nvisibility: public", False),
        ('description: Quoted\nuser-invocable: "false"', False),
        ("description: Numeric\nuser-invocable: 0", False),
        ("description: Numeric on\nuser-invocable: 1", True),
    ],
)
def test_parser_path_stores_resolved_user_invocable(
    tmp_path: Path,
    frontmatter: str,
    expected: bool,
) -> None:
    """The real parser stores resolve_user_invocable's result."""
    _assert_parser_user_invocable(tmp_path, frontmatter, expected)


def _codex_user_invocable_warnings(directory: Path, field_lines: str) -> list[Diagnostic]:
    """Render one Codex agent and return user-invocability lossy warnings."""
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "agent.agent.md"
    source.write_text(
        f"---\nname: agent\ndescription: Agent\n{field_lines}\n---\nReview changes.\n",
        encoding="utf-8",
    )
    diagnostics = DiagnosticCollector()
    AgentIntegrator._write_codex_agent(
        source,
        directory / "agent.toml",
        diagnostics=diagnostics,
        package_name="pkg",
    )
    return [
        diagnostic
        for diagnostic in diagnostics.by_category().get(CATEGORY_AGENT_LOSSY_COMPILATION, [])
        if "user-invocability" in diagnostic.message
    ]


def _assert_codex_warns(directory: Path, field_lines: str) -> None:
    """The integrator warns when the resolver marks the agent programmatic-only."""
    assert len(_codex_user_invocable_warnings(directory, field_lines)) == 1


def _assert_codex_silent(directory: Path, field_lines: str) -> None:
    """The integrator stays silent when the resolver marks the agent invocable."""
    assert _codex_user_invocable_warnings(directory, field_lines) == []


def test_integrator_path_follows_resolved_user_invocable(tmp_path: Path) -> None:
    """Codex render warns exactly when the real resolver returns False."""
    _assert_codex_warns(tmp_path / "false", "user-invocable: false")
    _assert_codex_warns(tmp_path / "alias", "visibility: internal")
    _assert_codex_silent(tmp_path / "true", "user-invocable: true")
    _assert_codex_silent(tmp_path / "canonical", "user-invocable: true\nvisibility: internal")


def _always_true(_metadata: dict) -> bool:
    """Mutant that drops every programmatic-only branch."""
    return True


def _inverted(metadata: dict) -> bool:
    """Mutant that flips the canonical resolver."""
    return not models_mod.resolve_user_invocable(metadata)


def test_always_true_mutant_breaks_consumer_expectations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patching the lookup sites makes the False consumer expectations fail.

    The helpers are the same ones the consumer-path tests call. If those
    tests did not observe resolve_user_invocable, this mutant would not
    raise.
    """
    monkeypatch.setattr(parser_mod, "resolve_user_invocable", _always_true)
    with pytest.raises(AssertionError):
        _assert_parser_user_invocable(
            tmp_path / "parser-false",
            "description: Internal\nuser-invocable: false",
            False,
        )

    monkeypatch.setattr(agent_integrator_mod, "resolve_user_invocable", _always_true)
    with pytest.raises(AssertionError):
        _assert_codex_warns(tmp_path / "codex-false", "user-invocable: false")


def test_inverted_mutant_breaks_consumer_expectations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inverted resolver makes the True consumer expectations fail."""
    monkeypatch.setattr(parser_mod, "resolve_user_invocable", _inverted)
    with pytest.raises(AssertionError):
        _assert_parser_user_invocable(
            tmp_path / "parser-true",
            "description: Public\nuser-invocable: true",
            True,
        )

    monkeypatch.setattr(agent_integrator_mod, "resolve_user_invocable", _inverted)
    with pytest.raises(AssertionError):
        _assert_codex_silent(tmp_path / "codex-true", "user-invocable: true")
