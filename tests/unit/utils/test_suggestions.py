"""Tests for close-match suggestion helpers."""

from __future__ import annotations

from apm_cli.utils.suggestions import close_name_matches, format_close_match_hint


class TestCloseNameMatches:
    def test_near_miss_returns_suggestion(self):
        candidates = ["apm-review-panel", "apm-triage-panel", "shepherd-driver"]
        matches = close_name_matches("apm-review-panl", candidates)
        assert matches[0] == "apm-review-panel"
        assert "shepherd-driver" not in matches

    def test_wildly_different_name_returns_no_suggestion(self):
        candidates = ["apm-review-panel", "apm-triage-panel", "shepherd-driver"]
        assert close_name_matches("completely-unrelated-package", candidates) == []

    def test_empty_candidates_returns_no_suggestion(self):
        assert close_name_matches("apm-review-panel", []) == []

    def test_empty_query_returns_no_suggestion(self):
        assert close_name_matches("", ["apm-review-panel"]) == []

    def test_none_or_unreadable_candidates_return_no_suggestion(self):
        assert close_name_matches("apm-review-panl", None) == []

        class Boom:
            def __iter__(self):
                raise RuntimeError("marketplace cache unreadable")

        assert close_name_matches("apm-review-panl", Boom()) == []

    def test_match_is_case_insensitive_but_preserves_canonical_name(self):
        assert close_name_matches("APM-REVIEW-PANL", ["apm-review-panel"]) == ["apm-review-panel"]


class TestFormatCloseMatchHint:
    def test_single_suggestion(self):
        assert format_close_match_hint(["apm-review-panel"]) == " Did you mean: apm-review-panel?"

    def test_multiple_suggestions(self):
        assert (
            format_close_match_hint(
                ["apm-review-panel", "apm-triage-panel"],
                similar_label="Similar plugins",
            )
            == " Similar plugins: apm-review-panel, apm-triage-panel"
        )

    def test_no_suggestions_returns_empty_string(self):
        assert format_close_match_hint([]) == ""
