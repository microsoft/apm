"""Unit tests for the DiagnosticCollector and related utilities."""

import threading
from dataclasses import FrozenInstanceError
from unittest.mock import call, patch  # noqa: F401

import pytest

from apm_cli.utils.diagnostics import (
    CATEGORY_AUTH,
    CATEGORY_COLLISION,
    CATEGORY_ERROR,
    CATEGORY_INFO,
    CATEGORY_OVERWRITE,
    CATEGORY_WARNING,
    Diagnostic,
    DiagnosticCollector,
    _group_by_package,
)

# ── Diagnostic dataclass ────────────────────────────────────────────


class TestDiagnosticDataclass:
    def test_creation_required_fields(self):
        d = Diagnostic(message="file.md", category=CATEGORY_WARNING)
        assert d.message == "file.md"
        assert d.category == CATEGORY_WARNING
        assert d.package == ""
        assert d.detail == ""

    def test_creation_all_fields(self):
        d = Diagnostic(
            message="readme.md",
            category=CATEGORY_ERROR,
            package="my-pkg",
            detail="download failed",
        )
        assert d.message == "readme.md"
        assert d.category == CATEGORY_ERROR
        assert d.package == "my-pkg"
        assert d.detail == "download failed"

    def test_frozen_immutable(self):
        d = Diagnostic(message="x", category=CATEGORY_WARNING)
        with pytest.raises(FrozenInstanceError):
            d.message = "y"

    def test_equality(self):
        a = Diagnostic(message="f", category=CATEGORY_ERROR, package="p", detail="d")
        b = Diagnostic(message="f", category=CATEGORY_ERROR, package="p", detail="d")
        assert a == b

    def test_inequality(self):
        a = Diagnostic(message="f", category=CATEGORY_ERROR)
        b = Diagnostic(message="f", category=CATEGORY_WARNING)
        assert a != b


# ── DiagnosticCollector — recording ─────────────────────────────────


class TestDiagnosticCollectorRecording:
    def test_skip_records_collision(self):
        dc = DiagnosticCollector()
        dc.skip("path/file.md", package="pkg-a")
        items = dc.by_category()
        assert CATEGORY_COLLISION in items
        assert len(items[CATEGORY_COLLISION]) == 1
        d = items[CATEGORY_COLLISION][0]
        assert d.message == "path/file.md"
        assert d.package == "pkg-a"

    def test_overwrite_records_overwrite(self):
        dc = DiagnosticCollector()
        dc.overwrite("rules.md", package="pkg-b", detail="replaced")
        items = dc.by_category()
        assert CATEGORY_OVERWRITE in items
        d = items[CATEGORY_OVERWRITE][0]
        assert d.message == "rules.md"
        assert d.detail == "replaced"

    def test_warn_records_warning(self):
        dc = DiagnosticCollector()
        dc.warn("something odd", package="pkg-c", detail="extra info")
        items = dc.by_category()
        assert CATEGORY_WARNING in items
        d = items[CATEGORY_WARNING][0]
        assert d.message == "something odd"
        assert d.package == "pkg-c"
        assert d.detail == "extra info"

    def test_error_records_error(self):
        dc = DiagnosticCollector()
        dc.error("download failed", package="pkg-d", detail="404")
        items = dc.by_category()
        assert CATEGORY_ERROR in items
        d = items[CATEGORY_ERROR][0]
        assert d.message == "download failed"
        assert d.detail == "404"

    def test_multiple_diagnostics_across_categories(self):
        dc = DiagnosticCollector()
        dc.skip("a.md", package="p1")
        dc.overwrite("b.md", package="p2")
        dc.warn("w", package="p3")
        dc.error("e", package="p4")
        groups = dc.by_category()
        assert len(groups) == 4
        assert len(groups[CATEGORY_COLLISION]) == 1
        assert len(groups[CATEGORY_OVERWRITE]) == 1
        assert len(groups[CATEGORY_WARNING]) == 1
        assert len(groups[CATEGORY_ERROR]) == 1


# ── DiagnosticCollector — query helpers ─────────────────────────────


class TestDiagnosticCollectorQueryHelpers:
    def test_has_diagnostics_false_when_empty(self):
        dc = DiagnosticCollector()
        assert dc.has_diagnostics is False

    def test_has_diagnostics_true_after_recording(self):
        dc = DiagnosticCollector()
        dc.warn("w")
        assert dc.has_diagnostics is True

    def test_error_count_zero(self):
        dc = DiagnosticCollector()
        dc.warn("w")
        assert dc.error_count == 0

    def test_error_count_returns_correct_count(self):
        dc = DiagnosticCollector()
        dc.error("e1")
        dc.error("e2")
        dc.warn("w")
        assert dc.error_count == 2

    def test_by_category_groups_correctly(self):
        dc = DiagnosticCollector()
        dc.skip("s1")
        dc.skip("s2")
        dc.error("e1")
        groups = dc.by_category()
        assert len(groups[CATEGORY_COLLISION]) == 2
        assert len(groups[CATEGORY_ERROR]) == 1
        assert CATEGORY_WARNING not in groups

    def test_by_category_preserves_insertion_order(self):
        dc = DiagnosticCollector()
        dc.skip("first")
        dc.skip("second")
        dc.skip("third")
        collisions = dc.by_category()[CATEGORY_COLLISION]
        assert [d.message for d in collisions] == ["first", "second", "third"]

    # ── count_for_package ───────────────────────────────────────────

    def test_count_for_package_filtered_by_category(self):
        dc = DiagnosticCollector()
        dc.skip("a.md", package="pkg1")
        dc.skip("b.md", package="pkg1")
        dc.error("fail", package="pkg1")
        dc.warn("w", package="pkg2")
        assert dc.count_for_package("pkg1", CATEGORY_COLLISION) == 2

    def test_count_for_package_all_categories(self):
        dc = DiagnosticCollector()
        dc.skip("a.md", package="pkg1")
        dc.error("fail", package="pkg1")
        dc.warn("w", package="pkg1")
        dc.warn("other", package="pkg2")
        assert dc.count_for_package("pkg1") == 3

    def test_count_for_package_nonexistent(self):
        dc = DiagnosticCollector()
        dc.skip("a.md", package="pkg1")
        assert dc.count_for_package("nonexistent") == 0


# ── DiagnosticCollector — rendering ─────────────────────────────────

_MOCK_BASE = "apm_cli.utils.diagnostics"


class TestDiagnosticCollectorRendering:
    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_render_summary_does_nothing_when_empty(
        self, mock_info, mock_warning, mock_echo, mock_console
    ):
        dc = DiagnosticCollector()
        dc.render_summary()
        mock_echo.assert_not_called()
        mock_warning.assert_not_called()
        mock_info.assert_not_called()

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_render_summary_normal_shows_counts_not_files(
        self, mock_info, mock_warning, mock_echo, mock_console
    ):
        dc = DiagnosticCollector(verbose=False)
        dc.skip("a.md", package="p1")
        dc.skip("b.md", package="p1")
        dc.render_summary()
        # Should mention count
        warning_texts = [str(c) for c in mock_warning.call_args_list]
        assert any("2 files skipped" in t for t in warning_texts)
        # Should NOT list individual file paths
        echo_texts = [str(c) for c in mock_echo.call_args_list]
        assert not any("a.md" in t for t in echo_texts)
        assert not any("b.md" in t for t in echo_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_render_summary_verbose_shows_file_paths(
        self, mock_info, mock_warning, mock_echo, mock_console
    ):
        dc = DiagnosticCollector(verbose=True)
        dc.skip("a.md", package="p1")
        dc.render_summary()
        echo_texts = [str(c) for c in mock_echo.call_args_list]
        assert any("a.md" in t for t in echo_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_collision_group_shows_force_hint(
        self, mock_info, mock_warning, mock_echo, mock_console
    ):
        dc = DiagnosticCollector()
        dc.skip("f.md")
        dc.render_summary()
        info_texts = [str(c) for c in mock_info.call_args_list]
        assert any("--force" in t for t in info_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_overwrite_group_shows_overwrote_message(
        self, mock_info, mock_warning, mock_echo, mock_console
    ):
        dc = DiagnosticCollector()
        dc.overwrite("skill.md", package="pkg")
        dc.render_summary()
        warning_texts = [str(c) for c in mock_warning.call_args_list]
        assert any("skill" in t and "replaced" in t for t in warning_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_overwrite_verbose_renders_detail(
        self, mock_info, mock_warning, mock_echo, mock_console
    ):
        dc = DiagnosticCollector(verbose=True)
        dc.overwrite("skill.md", package="pkg", detail="replaced by newer version")
        dc.render_summary()
        echo_texts = [str(c) for c in mock_echo.call_args_list]
        assert any("replaced by newer version" in t for t in echo_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_error_group_shows_packages_failed(
        self, mock_info, mock_warning, mock_echo, mock_console
    ):
        dc = DiagnosticCollector()
        dc.error("timeout", package="pkg-x")
        dc.render_summary()
        echo_texts = [str(c) for c in mock_echo.call_args_list]
        assert any("failed" in t for t in echo_texts)
        assert any("pkg-x" in t for t in echo_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_warning_group_shows_individual_warnings(
        self, mock_info, mock_warning, mock_echo, mock_console
    ):
        dc = DiagnosticCollector()
        dc.warn("something weird", package="pkg-w")
        dc.render_summary()
        warning_texts = [str(c) for c in mock_warning.call_args_list]
        assert any("something weird" in t for t in warning_texts)
        assert any("pkg-w" in t for t in warning_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_render_summary_handles_all_categories(
        self, mock_info, mock_warning, mock_echo, mock_console
    ):
        dc = DiagnosticCollector(verbose=True)
        dc.skip("collision.md", package="p1")
        dc.overwrite("over.md", package="p2", detail="replaced")
        dc.warn("watch out", package="p3")
        dc.error("boom", package="p4", detail="stack trace")
        dc.render_summary()

        all_texts = (
            [str(c) for c in mock_echo.call_args_list]
            + [str(c) for c in mock_warning.call_args_list]
            + [str(c) for c in mock_info.call_args_list]
        )
        combined = " ".join(all_texts)
        # All categories should appear
        assert "skipped" in combined
        assert "replaced" in combined
        assert "watch out" in combined
        assert "failed" in combined


# ── Thread safety ───────────────────────────────────────────────────


class TestDiagnosticCollectorThreadSafety:
    def test_concurrent_skip_calls_preserve_all_data(self):
        dc = DiagnosticCollector()
        num_threads = 10
        items_per_thread = 100
        barrier = threading.Barrier(num_threads)

        def worker(tid: int):
            barrier.wait()
            for i in range(items_per_thread):
                dc.skip(f"t{tid}-{i}.md", package=f"pkg-{tid}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = num_threads * items_per_thread
        assert len(dc.by_category()[CATEGORY_COLLISION]) == total


# ── _group_by_package helper ────────────────────────────────────────


class TestGroupByPackage:
    def test_groups_by_package(self):
        items = [
            Diagnostic(message="a", category=CATEGORY_WARNING, package="p1"),
            Diagnostic(message="b", category=CATEGORY_WARNING, package="p2"),
            Diagnostic(message="c", category=CATEGORY_WARNING, package="p1"),
        ]
        groups = _group_by_package(items)
        assert list(groups.keys()) == ["p1", "p2"]
        assert len(groups["p1"]) == 2
        assert len(groups["p2"]) == 1

    def test_empty_package_key(self):
        items = [
            Diagnostic(message="x", category=CATEGORY_WARNING, package=""),
            Diagnostic(message="y", category=CATEGORY_WARNING, package="pkg"),
        ]
        groups = _group_by_package(items)
        assert "" in groups
        assert len(groups[""]) == 1
        assert len(groups["pkg"]) == 1

    def test_preserves_insertion_order(self):
        items = [
            Diagnostic(message="a", category=CATEGORY_WARNING, package="z"),
            Diagnostic(message="b", category=CATEGORY_WARNING, package="a"),
            Diagnostic(message="c", category=CATEGORY_WARNING, package="m"),
        ]
        groups = _group_by_package(items)
        assert list(groups.keys()) == ["z", "a", "m"]


# ── Info category ───────────────────────────────────────────────────


class TestInfoCategory:
    def test_info_adds_diagnostic(self):
        dc = DiagnosticCollector()
        dc.info("3 dependencies have no pinned version")
        assert dc.has_diagnostics is True
        assert len(dc._diagnostics) == 1
        assert dc._diagnostics[0].category == CATEGORY_INFO
        assert dc._diagnostics[0].message == "3 dependencies have no pinned version"

    def test_info_renders_in_summary(self):
        dc = DiagnosticCollector()
        dc.info("2 dependencies have no pinned version -- pin with #tag")
        with (
            patch(f"{_MOCK_BASE}._get_console", return_value=None),
            patch(f"{_MOCK_BASE}._rich_echo") as mock_echo,  # noqa: F841
            patch(f"{_MOCK_BASE}._rich_warning"),
            patch(f"{_MOCK_BASE}._rich_info") as mock_info,
        ):
            dc.render_summary()
            mock_info.assert_any_call(
                "  [i] 2 dependencies have no pinned version -- pin with #tag"
            )

    def test_info_appears_after_other_categories(self):
        dc = DiagnosticCollector()
        dc.info("hint message")
        dc.warn("a warning", package="pkg")

        call_order = []
        with (
            patch(f"{_MOCK_BASE}._get_console", return_value=None),
            patch(f"{_MOCK_BASE}._rich_echo") as mock_echo,  # noqa: F841
            patch(
                f"{_MOCK_BASE}._rich_warning",
                side_effect=lambda *a, **k: call_order.append("warning"),
            ),
            patch(
                f"{_MOCK_BASE}._rich_info", side_effect=lambda *a, **k: call_order.append("info")
            ),
        ):
            dc.render_summary()
        # Warning must render before info
        warn_idx = next(i for i, c in enumerate(call_order) if c == "warning")
        info_idx = next(i for i, c in enumerate(call_order) if c == "info")
        assert warn_idx < info_idx, f"warning at {warn_idx} should precede info at {info_idx}"

    def test_info_unpinned_deps_singular(self):
        dc = DiagnosticCollector()
        dc.info("1 dependency has no pinned version -- pin with #tag or #sha to prevent drift")
        with (
            patch(f"{_MOCK_BASE}._get_console", return_value=None),
            patch(f"{_MOCK_BASE}._rich_echo"),
            patch(f"{_MOCK_BASE}._rich_info") as mock_info,
        ):
            dc.render_summary()
            mock_info.assert_any_call(
                "  [i] 1 dependency has no pinned version -- pin with #tag or #sha to prevent drift"
            )

    def test_info_unpinned_deps_plural(self):
        dc = DiagnosticCollector()
        dc.info("3 dependencies have no pinned version -- pin with #tag or #sha to prevent drift")
        with (
            patch(f"{_MOCK_BASE}._get_console", return_value=None),
            patch(f"{_MOCK_BASE}._rich_echo"),
            patch(f"{_MOCK_BASE}._rich_info") as mock_info,
        ):
            dc.render_summary()
            mock_info.assert_any_call(
                "  [i] 3 dependencies have no pinned version "
                "-- pin with #tag or #sha to prevent drift"
            )


# ── Auth category ───────────────────────────────────────────────────


class TestAuthCategory:
    def test_auth_adds_diagnostic(self):
        dc = DiagnosticCollector()
        dc.auth("EMU token detected — fallback to unauthenticated", package="pkg-a")
        assert dc.has_diagnostics is True
        assert len(dc._diagnostics) == 1
        assert dc._diagnostics[0].category == CATEGORY_AUTH
        assert dc._diagnostics[0].message == "EMU token detected — fallback to unauthenticated"
        assert dc._diagnostics[0].package == "pkg-a"

    def test_auth_with_detail(self):
        dc = DiagnosticCollector()
        dc.auth("credential fallback", package="pkg-b", detail="tried GITHUB_APM_PAT first")
        d = dc._diagnostics[0]
        assert d.detail == "tried GITHUB_APM_PAT first"

    def test_auth_count_zero_when_empty(self):
        dc = DiagnosticCollector()
        dc.warn("unrelated")
        assert dc.auth_count == 0

    def test_auth_count_returns_correct_count(self):
        dc = DiagnosticCollector()
        dc.auth("issue 1")
        dc.auth("issue 2")
        dc.warn("not auth")
        assert dc.auth_count == 2

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_auth_render_singular(self, mock_info, mock_warning, mock_echo, mock_console):
        dc = DiagnosticCollector()
        dc.auth("token expired", package="pkg-x")
        dc.render_summary()
        warning_texts = [str(c) for c in mock_warning.call_args_list]
        assert any("1 authentication issue" in t for t in warning_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_auth_render_plural(self, mock_info, mock_warning, mock_echo, mock_console):
        dc = DiagnosticCollector()
        dc.auth("issue 1", package="p1")
        dc.auth("issue 2", package="p2")
        dc.render_summary()
        warning_texts = [str(c) for c in mock_warning.call_args_list]
        assert any("2 authentication issues" in t for t in warning_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_auth_render_shows_package_and_message(
        self, mock_info, mock_warning, mock_echo, mock_console
    ):
        dc = DiagnosticCollector()
        dc.auth("EMU token fallback", package="my-pkg")
        dc.render_summary()
        echo_texts = [str(c) for c in mock_echo.call_args_list]
        assert any("my-pkg" in t and "EMU token fallback" in t for t in echo_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_auth_verbose_renders_detail(self, mock_info, mock_warning, mock_echo, mock_console):
        dc = DiagnosticCollector(verbose=True)
        dc.auth("fallback used", package="pkg", detail="GITHUB_APM_PAT → unauthenticated")
        dc.render_summary()
        echo_texts = [str(c) for c in mock_echo.call_args_list]
        assert any("GITHUB_APM_PAT" in t for t in echo_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_auth_non_verbose_shows_hint(self, mock_info, mock_warning, mock_echo, mock_console):
        dc = DiagnosticCollector(verbose=False)
        dc.auth("credential issue", detail="secret detail")
        dc.render_summary()
        info_texts = [str(c) for c in mock_info.call_args_list]
        assert any("--verbose" in t for t in info_texts)
        # detail should NOT appear in non-verbose mode
        echo_texts = [str(c) for c in mock_echo.call_args_list]
        assert not any("secret detail" in t for t in echo_texts)

    @patch(f"{_MOCK_BASE}._get_console", return_value=None)
    @patch(f"{_MOCK_BASE}._rich_echo")
    @patch(f"{_MOCK_BASE}._rich_warning")
    @patch(f"{_MOCK_BASE}._rich_info")
    def test_auth_renders_before_collision(self, mock_info, mock_warning, mock_echo, mock_console):
        dc = DiagnosticCollector()
        dc.skip("collision.md", package="p1")
        dc.auth("auth issue", package="p2")
        call_order = []

        with (
            patch(f"{_MOCK_BASE}._get_console", return_value=None),
            patch(f"{_MOCK_BASE}._rich_echo"),
            patch(
                f"{_MOCK_BASE}._rich_warning", side_effect=lambda *a, **k: call_order.append(str(a))
            ),
            patch(f"{_MOCK_BASE}._rich_info"),
        ):
            dc.render_summary()

        auth_idx = next(i for i, t in enumerate(call_order) if "authentication" in t)
        coll_idx = next(i for i, t in enumerate(call_order) if "skipped" in t)
        assert auth_idx < coll_idx, "auth should render before collision"
