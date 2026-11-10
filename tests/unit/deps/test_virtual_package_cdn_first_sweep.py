"""Regression tests for the raw-CDN-first marker sweep (microsoft/apm#3283).

A public in-repo plugin whose manifest sits at ``.claude-plugin/plugin.json``
previously spent one anonymous Contents API call per earlier marker the CDN
missed (four for a Claude Code plugin layout), exhausting the shared 60/h
anonymous budget after roughly fifteen plugins.  The approved bounded repair
sweeps every candidate marker through the raw CDN first; the Contents API is
only reached when the sweep finds nothing (API-only / private recovery kept
byte-for-byte).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.deps import github_downloader_validation as gdv
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference

MARKER_COUNT = len(
    [
        "apm.yml",
        "SKILL.md",
        "plugin.json",
        ".github/plugin/plugin.json",
        ".claude-plugin/plugin.json",
        ".cursor-plugin/plugin.json",
        "README.md",
    ]
)


def _make_subdir_dep(
    repo_url: str = "owner/repo",
    vpath: str = "plugins/skill-creator",
    ref: str | None = "main",
) -> DependencyReference:
    return DependencyReference(
        repo_url=repo_url,
        reference=ref,
        virtual_path=vpath,
        is_virtual=True,
    )


def _not_found_response() -> MagicMock:
    response = MagicMock()
    response.status_code = 404
    response.headers = {}
    response.content = b"{}"
    response.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "404 Not Found", response=response
    )
    return response


def _recording_raw_wrapper(downloader: GitHubPackageDownloader):
    """Wrap ``download_raw_file`` recording every ``raw_only`` value seen."""
    calls: list[bool] = []
    original = downloader.download_raw_file

    def _wrapper(dep_ref, file_path, ref="main", **kwargs):
        calls.append(bool(kwargs.get("raw_only", False)))
        return original(dep_ref, file_path, ref, **kwargs)

    return calls, _wrapper


class TestRawCdnFirstSweep:
    """Public github.com subdirectory validation must not spend API calls."""

    def test_sweep_hit_costs_zero_contents_api_calls(self) -> None:
        """A Claude-plugin layout validates with zero Contents API requests."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep()
        cdn_only_hit = f"{dep_ref.virtual_path}/.claude-plugin/plugin.json"

        def _cdn(owner, repo, ref, file_path):
            return b'{"name": "skill-creator"}' if file_path == cdn_only_hit else None

        with (
            patch.object(downloader._strategies, "try_raw_download", side_effect=_cdn),
            patch.object(downloader, "_resilient_get") as api_mock,
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is True
        api_mock.assert_not_called()

    def test_sweep_probes_every_marker_raw_only_before_api(self) -> None:
        """Sweep calls are exactly the marker list, all with raw_only=True."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep()
        calls, wrapper = _recording_raw_wrapper(downloader)

        with (
            patch.object(downloader, "download_raw_file", side_effect=wrapper),
            patch.object(downloader._strategies, "try_raw_download", return_value=None),
            patch.object(
                downloader.auth_resolver,
                "try_with_fallback",
                side_effect=lambda _host, fn, **kw: fn(None, {}),
            ),
            patch.object(
                downloader, "_resilient_get", return_value=_not_found_response()
            ) as api_mock,
            patch.object(gdv, "_ref_exists_via_ls_remote", return_value=(False, None)),
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        # Sweep ran over every marker raw-only; then the legacy per-marker
        # probes escalated to the API (which 404s below) -- so we see the
        # marker list twice: once raw_only=True, once legacy.
        assert ok is False
        assert calls[:MARKER_COUNT] == [True] * MARKER_COUNT
        assert calls[MARKER_COUNT:] == [False] * MARKER_COUNT
        # One Contents API request per candidate ref (main + master fallback)
        # per marker in the legacy pass, plus the fallback-1 directory probe
        # -- the sweep itself spent zero.
        assert api_mock.call_count == 2 * MARKER_COUNT + 1

    def test_sweep_miss_still_validates_via_api(self) -> None:
        """API-only recovery is preserved when the CDN has no marker."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep()

        response = MagicMock()
        response.status_code = 200
        response.content = b"marker-bytes"
        response.raise_for_status = lambda: None

        with (
            patch.object(downloader._strategies, "try_raw_download", return_value=None),
            patch.object(
                downloader.auth_resolver,
                "try_with_fallback",
                side_effect=lambda _host, fn, **kw: fn(None, {}),
            ),
            patch.object(downloader, "_resilient_get", return_value=response),
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is True

    def test_non_public_host_skips_sweep(self) -> None:
        """GitHub Enterprise hosts keep the legacy probe order."""
        downloader = GitHubPackageDownloader()
        dep_ref = DependencyReference(
            repo_url="owner/repo",
            host="acme.ghe.com",
            reference="main",
            virtual_path="plugins/skill-creator",
            is_virtual=True,
        )
        assert downloader.auth_resolver.uses_public_github_anonymous_first("acme.ghe.com") is False
        calls, wrapper = _recording_raw_wrapper(downloader)

        with (
            patch.object(downloader, "download_raw_file", side_effect=wrapper),
            # Simulated legacy per-marker miss keeps the probe offline, while
            # the directory and ls-remote fallbacks are stubbed at the
            # validation-module boundary.
            patch.object(
                downloader._strategies,
                "download_github_file",
                side_effect=RuntimeError("simulated miss"),
            ) as legacy_mock,
            patch.object(downloader._strategies, "try_raw_download") as cdn_mock,
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(gdv, "_ref_exists_via_ls_remote", return_value=(False, None)),
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False
        assert legacy_mock.call_count == MARKER_COUNT
        cdn_mock.assert_not_called()
        assert calls == [False] * MARKER_COUNT, "no probe may run raw-only off public github"

    def test_virtual_file_probe_keeps_legacy_semantics(self) -> None:
        """Single-file virtual packages are unaffected by the sweep."""
        downloader = GitHubPackageDownloader()
        dep_ref = DependencyReference(
            repo_url="owner/repo",
            reference="main",
            virtual_path="prompts/code-review.prompt.md",
            is_virtual=True,
        )
        calls, wrapper = _recording_raw_wrapper(downloader)

        with (
            patch.object(downloader, "download_raw_file", side_effect=wrapper),
            patch.object(downloader._strategies, "try_raw_download", return_value=None),
            patch.object(
                downloader.auth_resolver,
                "try_with_fallback",
                side_effect=lambda _host, fn, **kw: fn(None, {}),
            ),
            patch.object(downloader, "_resilient_get", return_value=_not_found_response()),
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False
        assert calls == [False]

    def test_raw_only_never_touches_contents_api_on_miss(self) -> None:
        """raw_only=True miss raises without any Contents API traffic."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="plugins/missing")

        with (
            patch.object(downloader._strategies, "try_raw_download", return_value=None),
            patch.object(downloader, "_resilient_get") as api_mock,
        ):
            with pytest.raises(RuntimeError, match="not found via raw CDN"):
                downloader.download_raw_file(
                    dep_ref, "plugins/missing/apm.yml", "main", raw_only=True
                )

        api_mock.assert_not_called()
