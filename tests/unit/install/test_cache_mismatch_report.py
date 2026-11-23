"""The download phase's cache-mismatch notice and its reported-set handoff (#4351)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.install.phases import download

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-package-content-hash")]


def test_cache_mismatch_counts_as_reported_only_when_a_sink_received_it(tmp_path, capsys):
    """With no diagnostics and no logger, integrate keeps its fallback report (#4351)."""
    package = tmp_path / "apm_modules" / "pkg"
    package.mkdir(parents=True)
    (package / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
    locked = SimpleNamespace(content_hash="sha256:" + "0" * 64, carried_licenses=None)
    silent = SimpleNamespace(
        diagnostics=None, logger=None, existing_lockfile=None, cache_mismatch_reported=set()
    )
    download._report_cache_mismatch(silent, "pkg", locked, package)
    assert capsys.readouterr().out == ""
    assert silent.cache_mismatch_reported == set()

    heard = SimpleNamespace(
        diagnostics=None,
        logger=SimpleNamespace(progress=print),
        existing_lockfile=None,
        cache_mismatch_reported=set(),
    )
    download._report_cache_mismatch(heard, "pkg", locked, package)
    out = capsys.readouterr().out
    assert out.count("Content hash mismatch for pkg") == 1, out
    assert heard.cache_mismatch_reported == {"pkg"}
