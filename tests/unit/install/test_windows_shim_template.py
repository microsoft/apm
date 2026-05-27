"""Regression test for issue microsoft/apm#1509.

The Windows installer (install.ps1) writes a `apm.cmd` shim under
`%LOCALAPPDATA%\\Programs\\apm\\bin\\`. Historically the shim embedded the
fully-expanded `$releaseDir` path and was saved with `-Encoding ASCII`. On
Windows accounts whose profile directory contains non-ASCII characters
(for example a username like "Jose" with an accented 'e'), the ASCII
encoding mangled or stripped the accented characters, so the shim
resolved to a non-existent path and cmd.exe reported:

    The system cannot find the path specified.

The fix is twofold:

1. Emit the literal token ``%LOCALAPPDATA%`` in the shim payload instead
   of the expanded profile path whenever the release directory lives
   under ``$env:LOCALAPPDATA``. cmd.exe expands the token at runtime, so
   the shim is independent of how the path was encoded on disk.
2. Stop using ``-Encoding ASCII`` for the shim file. Any custom
   ``APM_INSTALL_DIR`` outside ``%LOCALAPPDATA%`` is still written
   verbatim, so it must use a non-lossy encoding that preserves the
   author's intent on disk.

This module-level test parses install.ps1 directly (no PowerShell host
required) and locks in both invariants as a regression trap.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_PS1 = REPO_ROOT / "install.ps1"


def _read_install_script() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8")


def _shim_block(text: str) -> str:
    """Return the contiguous block of install.ps1 that writes apm.cmd.

    PowerShell line comments (``#``-prefixed) are stripped so assertions
    operate on executable statements only -- otherwise an explanatory
    comment that names the bad pattern would trip the regression trap.
    """
    match = re.search(
        r"\$shimPath\s*=.*?apm\.cmd.*?(?=\n\s*Add-ToUserPath)",
        text,
        re.DOTALL,
    )
    assert match is not None, "Could not locate apm.cmd shim-writing block in install.ps1"
    raw = match.group(0)
    code_lines = [line for line in raw.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(code_lines)


def test_install_ps1_exists() -> None:
    assert INSTALL_PS1.is_file(), f"install.ps1 missing at {INSTALL_PS1}"


def test_shim_uses_localappdata_literal_token() -> None:
    """Regression for #1509: shim payload must reference %LOCALAPPDATA%."""
    block = _shim_block(_read_install_script())
    assert "%LOCALAPPDATA%" in block, (
        "install.ps1 must emit the literal %LOCALAPPDATA% token in the "
        "apm.cmd shim so cmd.exe expands the path at runtime (issue #1509)."
    )


def test_shim_not_written_with_ascii_encoding() -> None:
    """Regression for #1509: -Encoding ASCII mangles non-ASCII paths."""
    block = _shim_block(_read_install_script())
    assert "-Encoding ASCII" not in block, (
        "apm.cmd shim must not be written with -Encoding ASCII; the "
        "encoding strips accented characters from custom install paths "
        "(issue #1509)."
    )


def test_shim_uses_utf16le_with_bom_encoding() -> None:
    """Shim must be written as UTF-16LE with BOM so cmd.exe auto-detects it.

    UTF-8 (with or without BOM) is not safely interpreted by cmd.exe across
    OEM/ANSI code pages, which would re-introduce non-ASCII path corruption
    on a custom APM_INSTALL_DIR (Copilot review on PR #1512).
    """
    block = _shim_block(_read_install_script())
    assert "System.Text.UTF8Encoding" not in block, (
        "apm.cmd shim must not be written with UTF8Encoding; cmd.exe does "
        "not reliably auto-detect UTF-8 and may garble non-ASCII path "
        "characters."
    )
    assert "System.Text.UnicodeEncoding" in block, (
        "apm.cmd shim must be written with System.Text.UnicodeEncoding "
        "(UTF-16LE) so cmd.exe auto-detects the encoding via the BOM."
    )
    # UnicodeEncoding(bigEndian=$false, byteOrderMark=$true) -> little-endian
    # with BOM. Lock the BOM flag in so a future edit cannot silently drop
    # it.
    assert re.search(
        r"New-Object\s+System\.Text\.UnicodeEncoding\s*\(\s*\$false\s*,\s*\$true\s*\)",
        block,
    ), (
        "UnicodeEncoding must be constructed as ($false, $true): little-endian "
        "with a byte-order mark."
    )


def test_shim_localappdata_check_enforces_path_boundary() -> None:
    """Shim path rewrite must enforce a separator boundary.

    A bare ``$releaseDir.StartsWith($env:LOCALAPPDATA, ...)`` check produces
    false positives for sibling directories that share a textual prefix
    (e.g. ``C:\\Users\\x\\AppData\\LocalStuff\\...``). The implementation must
    either trim+append a separator or otherwise verify the prefix ends at a
    path boundary (Copilot review on PR #1512).
    """
    block = _shim_block(_read_install_script())
    # The unsafe pattern is StartsWith on the raw $localAppData / $env:LOCALAPPDATA
    # without a trailing separator. Allow StartsWith only against a variable
    # that carries an explicit separator suffix.
    unsafe = re.search(
        r"\$releaseDir\.StartsWith\(\s*\$(?:localAppData|env:LOCALAPPDATA)\s*,",
        block,
    )
    assert unsafe is None, (
        "install.ps1 must not call $releaseDir.StartsWith($localAppData, ...) "
        "directly: append a path separator (or compare against an explicitly "
        "trimmed+suffixed prefix) so siblings like 'LocalStuff' are not "
        "rewritten under %LOCALAPPDATA%."
    )
    # Positive assertion: a separator-aware prefix variable must be in play.
    assert re.search(r"\$prefixWithSep|TrimEnd\(\s*'\\\\?'?", block) or (
        "'\\\\'" in block and "TrimEnd" in block
    ), (
        "install.ps1 must build a separator-suffixed prefix (e.g. "
        "$prefixWithSep = $localAppDataTrimmed + '\\\\') before calling "
        "StartsWith, so the prefix only matches at a path boundary."
    )
