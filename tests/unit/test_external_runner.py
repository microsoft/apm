"""Unit tests for the shared external-scanner runner.

Covers :mod:`apm_cli.security.external.runner` -- the vendor-neutral loop
shared by ``apm audit --external`` and the install-time audit phase.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.security.content_scanner import ScanFinding
from apm_cli.security.external.base import ExternalScanError
from apm_cli.security.external.runner import merge_findings, run_external_scanners


def _finding(file="a.md"):
    return ScanFinding(
        file=file,
        line=1,
        column=1,
        char="\u202e",
        codepoint="U+202E",
        severity="critical",
        category="bidi-override",
        description="RLO",
    )


def test_merge_findings_groups_by_file():
    base = {"a.md": [_finding("a.md")]}
    merge_findings(base, {"a.md": [_finding("a.md")], "b.md": [_finding("b.md")]})
    assert len(base["a.md"]) == 2
    assert len(base["b.md"]) == 1


def test_unknown_scanner_raises(monkeypatch):
    def _resolve(name, sarif_file=None):
        raise ValueError(f"Unknown scanner: {name}")

    monkeypatch.setattr("apm_cli.security.external.registry.resolve_scanner", _resolve)
    with pytest.raises(ExternalScanError, match="Unknown scanner"):
        run_external_scanners(["bogus"], None, [Path(".")])


def test_unavailable_scanner_fails_closed(monkeypatch):
    scanner = MagicMock()
    scanner.is_available.return_value = (False, "not found on PATH")
    monkeypatch.setattr(
        "apm_cli.security.external.registry.resolve_scanner", lambda name, sarif_file=None: scanner
    )
    with pytest.raises(ExternalScanError, match="unavailable"):
        run_external_scanners(["skillspector"], None, [Path(".")])
    scanner.scan.assert_not_called()


def test_successful_scan_merges(monkeypatch):
    scanner = MagicMock()
    scanner.is_available.return_value = (True, "")
    finding = _finding("a.md")
    scanner.scan.return_value = {"a.md": [finding]}
    monkeypatch.setattr(
        "apm_cli.security.external.registry.resolve_scanner", lambda name, sarif_file=None: scanner
    )
    result = run_external_scanners(["skillspector"], None, [Path(".")])
    assert result == {"a.md": [finding]}


def test_scan_failure_wrapped(monkeypatch):
    scanner = MagicMock()
    scanner.is_available.return_value = (True, "")
    scanner.scan.side_effect = ExternalScanError("boom")
    monkeypatch.setattr(
        "apm_cli.security.external.registry.resolve_scanner", lambda name, sarif_file=None: scanner
    )
    with pytest.raises(ExternalScanError, match="failed"):
        run_external_scanners(["skillspector"], None, [Path(".")])


def test_options_by_name_none_defaults(monkeypatch):
    """options_by_name=None resolves to a default ScannerOptions (no crash)."""
    from apm_cli.security.external.options import ScannerOptions

    scanner = MagicMock()
    scanner.is_available.return_value = (True, "")
    scanner.scan.return_value = {}
    monkeypatch.setattr(
        "apm_cli.security.external.registry.resolve_scanner",
        lambda name, sarif_file=None: scanner,
    )
    run_external_scanners(["skillspector"], None, [Path(".")], options_by_name=None)
    _, kwargs = scanner.scan.call_args
    assert isinstance(kwargs["options"], ScannerOptions)
    assert kwargs["options"].extra_args == ()


def test_options_forwarded_to_scan(monkeypatch):
    from apm_cli.security.external.options import ScannerOptions

    scanner = MagicMock()
    scanner.is_available.return_value = (True, "")
    scanner.scan.return_value = {}
    monkeypatch.setattr(
        "apm_cli.security.external.registry.resolve_scanner",
        lambda name, sarif_file=None: scanner,
    )
    opts = ScannerOptions(llm=True, extra_args=("--model", "x"))
    run_external_scanners(
        ["skillspector"],
        None,
        [Path(".")],
        options_by_name={"skillspector": opts},
    )
    _, kwargs = scanner.scan.call_args
    assert kwargs["options"] is opts


def test_llm_egress_banner_emitted(monkeypatch):
    from apm_cli.security.external.options import ScannerOptions

    scanner = MagicMock()
    scanner.is_available.return_value = (True, "")
    scanner.scan.return_value = {}
    monkeypatch.setattr(
        "apm_cli.security.external.registry.resolve_scanner",
        lambda name, sarif_file=None: scanner,
    )
    logger = MagicMock()
    run_external_scanners(
        ["skillspector"],
        None,
        [Path(".")],
        logger=logger,
        options_by_name={"skillspector": ScannerOptions(llm=True)},
    )
    banner_calls = [c.args[0] for c in logger.warning.call_args_list if c.args]
    assert any("LLM analysis enabled" in msg for msg in banner_calls)


def test_no_banner_without_llm(monkeypatch):
    from apm_cli.security.external.options import ScannerOptions

    scanner = MagicMock()
    scanner.is_available.return_value = (True, "")
    scanner.scan.return_value = {}
    monkeypatch.setattr(
        "apm_cli.security.external.registry.resolve_scanner",
        lambda name, sarif_file=None: scanner,
    )
    logger = MagicMock()
    run_external_scanners(
        ["skillspector"],
        None,
        [Path(".")],
        logger=logger,
        options_by_name={"skillspector": ScannerOptions(llm=False)},
    )
    banner_calls = [c.args[0] for c in logger.warning.call_args_list if c.args]
    assert not any("LLM analysis enabled" in msg for msg in banner_calls)
