"""Unit tests for the ``schedule:`` frontmatter parser used by the
``copilot-app`` target.

Lives in the integrator module because the helper is intentionally
private to ``apm_cli.integration.prompt_integrator`` (no separate
module surface area for Wave 2).
"""

from __future__ import annotations

import pytest

from apm_cli.integration.prompt_integrator import (
    Schedule,
    _derive_package_owner,
    _parse_schedule,
)


class TestParseSchedule:
    def test_defaults_when_only_interval(self):
        s = _parse_schedule({"interval": "manual"})
        assert s == Schedule(interval="manual")

    def test_full_block(self):
        s = _parse_schedule(
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

    def test_rejects_non_mapping(self):
        with pytest.raises(ValueError, match=r"'schedule' must be a mapping"):
            _parse_schedule("daily")

    def test_rejects_unknown_interval(self):
        with pytest.raises(ValueError, match=r"interval must be one of"):
            _parse_schedule({"interval": "yearly"})

    def test_rejects_out_of_range_hour(self):
        with pytest.raises(ValueError, match=r"schedule_hour must be int 0..23"):
            _parse_schedule({"interval": "daily", "schedule_hour": 99})

    def test_rejects_out_of_range_day(self):
        with pytest.raises(ValueError, match=r"schedule_day must be int 0..6"):
            _parse_schedule({"interval": "weekly", "schedule_day": 9})

    def test_rejects_non_int_hour(self):
        with pytest.raises(ValueError, match=r"schedule_hour must be int 0..23"):
            _parse_schedule({"interval": "daily", "schedule_hour": "nine"})

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError, match=r"mode must be one of"):
            _parse_schedule({"interval": "manual", "mode": "rogue"})

    def test_rejects_non_string_model(self):
        with pytest.raises(ValueError, match=r"model must be a string"):
            _parse_schedule({"interval": "manual", "model": 42})


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
