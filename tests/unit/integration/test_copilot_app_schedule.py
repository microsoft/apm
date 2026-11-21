"""Unit tests for the workflow-frontmatter parser used by the
``copilot-app`` target.

Option B: dispatch is by frontmatter SHAPE (top-level keys), not by a
nested ``schedule:`` block.  The parser consumes flat top-level keys
directly from the prompt's frontmatter.
"""

from __future__ import annotations

import pytest

from apm_cli.integration.prompt_integrator import (
    Schedule,
    _derive_package_owner,
    _is_workflow_shape,
    _parse_workflow_frontmatter,
)

pytestmark = pytest.mark.unit


class TestParseWorkflowFrontmatter:
    def test_defaults_when_only_interval(self):
        s = _parse_workflow_frontmatter({"interval": "manual"})
        assert s == Schedule(interval="manual")

    def test_defaults_to_manual_when_only_other_keys(self):
        # interval optional: presence of other execution keys is enough
        s = _parse_workflow_frontmatter({"mode": "plan"})
        assert s.interval == "manual"
        assert s.mode == "plan"

    def test_full_frontmatter(self):
        s = _parse_workflow_frontmatter(
            {
                "interval": "weekly",
                "schedule_hour": 18,
                "schedule_day": 5,
                "mode": "plan",
                "model": "gpt-5",
                "reasoning_effort": "high",
            }
        )
        assert s.interval == "weekly"
        assert s.schedule_hour == 18
        assert s.schedule_day == 5
        assert s.mode == "plan"
        assert s.model == "gpt-5"
        assert s.reasoning_effort == "high"

    @pytest.mark.parametrize(
        ("frontmatter", "match"),
        [
            ("daily", r"frontmatter must be a mapping"),
            ({"interval": "yearly"}, r"interval must be one of"),
            ({"interval": "daily", "schedule_hour": 99}, r"schedule_hour must be int 0..23"),
            ({"interval": "weekly", "schedule_day": 9}, r"schedule_day must be int 0..6"),
            ({"interval": "daily", "schedule_hour": "nine"}, r"schedule_hour must be int 0..23"),
            ({"interval": "manual", "mode": "rogue"}, r"mode must be one of"),
            ({"interval": "manual", "mode": "autopilot"}, r"autopilot"),
            ({"interval": "manual", "model": 42}, r"model must be a string"),
        ],
        ids=[
            "non-mapping",
            "unknown-interval",
            "out-of-range-hour",
            "out-of-range-day",
            "non-int-hour",
            "unknown-mode",
            "autopilot-mode",
            "non-string-model",
        ],
    )
    def test_rejects_invalid_frontmatter(self, frontmatter, match):
        with pytest.raises(ValueError, match=match):
            _parse_workflow_frontmatter(frontmatter)


class TestIsWorkflowShape:
    def test_plain_prompt_is_not_workflow(self):
        assert not _is_workflow_shape({"name": "hello", "description": "x"})

    def test_model_alone_is_not_workflow(self):
        # Pinning a model is legitimate for plain slash commands.
        assert not _is_workflow_shape({"name": "hello", "model": "gpt-5"})

    def test_interval_marks_workflow(self):
        assert _is_workflow_shape({"interval": "manual"})

    def test_mode_alone_is_not_workflow(self):
        # ``mode`` is overloaded: VSCode uses agent|ask|edit, the App uses
        # interactive|plan|autopilot.  Same concept, different vocabularies.
        # A plain slash command with ``mode: agent`` must NOT be treated
        # as a workflow.  Author opts in to the App with ``interval: manual``.
        assert not _is_workflow_shape({"name": "hello", "mode": "agent"})
        assert not _is_workflow_shape({"name": "hello", "mode": "plan"})

    def test_schedule_hour_marks_workflow(self):
        assert _is_workflow_shape({"schedule_hour": 9})

    def test_schedule_day_marks_workflow(self):
        assert _is_workflow_shape({"schedule_day": 1})

    def test_reasoning_effort_alone_is_not_workflow(self):
        # ``reasoning_effort`` is a plain-prompt hint in VSCode/Copilot;
        # not a workflow marker.
        assert not _is_workflow_shape({"reasoning_effort": "high"})

    def test_handles_non_dict(self):
        assert not _is_workflow_shape(None)
        assert not _is_workflow_shape("nope")


class _PkgFake:
    """Minimal stand-in for ``APMPackage`` (only the attrs the helper reads)."""

    def __init__(self, source=None, author=None):
        self.source = source
        self.author = author


class _PkgInfoFake:
    def __init__(self, package):
        self.package = package


class TestDerivePackageOwner:
    def test_github_url(self):
        pi = _PkgInfoFake(_PkgFake(source="https://github.com/alice/repo"))
        assert _derive_package_owner(pi) == "alice"

    def test_short_github_form(self):
        pi = _PkgInfoFake(_PkgFake(source="alice/repo"))
        assert _derive_package_owner(pi) == "alice"

    def test_github_prefix(self):
        pi = _PkgInfoFake(_PkgFake(source="github:alice/repo"))
        assert _derive_package_owner(pi) == "alice"

    def test_falls_back_to_author(self):
        pi = _PkgInfoFake(_PkgFake(source=None, author="Alice Author"))
        assert _derive_package_owner(pi) == "Alice Author"

    def test_falls_back_to_local(self):
        pi = _PkgInfoFake(_PkgFake(source=None, author=None))
        assert _derive_package_owner(pi) == "local"
