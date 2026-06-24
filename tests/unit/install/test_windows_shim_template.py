"""Regression test for issue microsoft/apm#1509.

The Windows installer (install.ps1) writes a `apm.cmd` shim under
`%LOCALAPPDATA%\\Programs\\apm\\bin\\`. Historically the shim embedded the
fully-expanded `$releaseDir` path. On Windows accounts whose profile
directory contains non-ASCII characters (for example a username like
"Jose" with an accented 'e'), the lossy encoding that was paired with
the expanded path mangled or stripped the accented characters, so the
shim resolved to a non-existent path and cmd.exe reported:

    The system cannot find the path specified.

The fix is to emit the literal token ``%LOCALAPPDATA%`` in the shim
payload instead of the expanded profile path whenever the release
directory lives under ``$env:LOCALAPPDATA``. cmd.exe expands the token
at runtime, so the shim is independent of how the path was encoded on
disk -- and the embedded shim target stays purely ASCII even when the
user's profile path is not, which means the file itself can be written
with a cmd.exe-safe ASCII encoding.

This module-level test parses install.ps1 directly (no PowerShell host
required) and locks in those invariants as a regression trap.
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


def test_shim_not_written_with_utf16_encoding() -> None:
    """Regression for windows-CI #1518 follow-up: cmd.exe cannot parse UTF-16LE .cmd.

    A previous attempt at the #1509 fix wrote the shim as UTF-16LE with
    BOM via ``System.Text.UnicodeEncoding``, on the assumption that
    cmd.exe would auto-detect the encoding through the BOM. It does
    not: invoking a UTF-16 .cmd via PATH surfaces as garbled output
    (``>\ufffd\ufffd@``) and exit code 1 on real Windows runners.
    The shim must use an encoding cmd.exe interprets through the
    system OEM/ANSI code page.
    """
    block = _shim_block(_read_install_script())
    assert "System.Text.UnicodeEncoding" not in block, (
        "apm.cmd shim must not be written as UTF-16LE; cmd.exe does not "
        "auto-detect UTF-16 via BOM when batch files are invoked through "
        "PATH and the shim becomes unusable."
    )
    assert "System.Text.UTF8Encoding" not in block, (
        "apm.cmd shim must not be written as UTF-8 either; cmd.exe "
        "interprets .cmd files via the active OEM/ANSI code page."
    )


def test_shim_written_with_ascii_encoding() -> None:
    """The shim must be written with ASCII encoding.

    ASCII is the safest encoding for the apm.cmd payload because:

    * The %LOCALAPPDATA% literal token (test_shim_uses_localappdata_literal_token)
      keeps the embedded shim target ASCII-only even when the user's
      profile directory contains non-ASCII characters, neutralising
      the original #1509 mangling vector.
    * Every other token in the payload (``@echo off``, ``REM`` lines,
      ``%*``) is ASCII by construction.
    * cmd.exe reliably parses ASCII .cmd files across every OEM/ANSI
      code page, whereas UTF-16 (with or without BOM) is not
      auto-detected on PATH invocation.
    """
    block = _shim_block(_read_install_script())
    assert re.search(r"-Encoding\s+ASCII", block), (
        "apm.cmd shim must be written with `-Encoding ASCII` (or "
        "equivalent ASCII-only writer) so cmd.exe parses it reliably."
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


def test_shim_else_branch_uses_release_dir_absolute_target() -> None:
    """Regression trap for the else branch (custom APM_INSTALL_DIR).

    When ``$releaseDir`` does NOT live under ``$env:LOCALAPPDATA`` the
    shim must fall back to the absolute path. Locking this in protects
    custom-root installs from being silently rewritten under a
    ``%LOCALAPPDATA%`` token that would resolve to the wrong directory.
    """
    block = _shim_block(_read_install_script())
    # Both branches of the if/else must exist.
    assert re.search(r"\bif\s*\(\s*\$underLocalAppData\s*\)", block), (
        "install.ps1 must branch on $underLocalAppData when picking the shim target."
    )
    assert re.search(r"\belse\s*\{", block), (
        "install.ps1 must keep the absolute-path else branch so custom "
        "APM_INSTALL_DIR roots outside %LOCALAPPDATA% still produce a "
        "valid shim target."
    )
    # The else branch must produce a shim target rooted at $releaseDir
    # (after percent-escaping) and must NOT inject a %LOCALAPPDATA% token.
    else_match = re.search(
        r"if\s*\(\s*\$underLocalAppData\s*\)\s*\{.*?\}\s*else\s*\{(?P<body>.*?)\}",
        block,
        re.DOTALL,
    )
    assert else_match is not None, "Could not isolate the else branch body."
    else_body = else_match.group("body")
    assert "%LOCALAPPDATA%" not in else_body, (
        "The else branch (custom APM_INSTALL_DIR outside %LOCALAPPDATA%) "
        "must not embed a %LOCALAPPDATA% token; doing so would rewrite the "
        "shim target to the wrong directory at cmd.exe runtime."
    )
    assert re.search(r"\$releaseDir(?:Escaped)?\b.*apm\.exe", else_body), (
        "The else branch must construct the shim target from $releaseDir "
        "(escaped or raw) and end in apm.exe."
    )


def test_shim_target_escapes_literal_percent_in_both_branches() -> None:
    """Regression trap for the percent-escape hardening.

    cmd.exe expands ``%name%`` as an environment variable reference.
    Any literal ``%`` in the constructed shim target (whether it came
    from a custom APM_INSTALL_DIR or a tag containing ``%``) must be
    doubled to ``%%`` so cmd.exe writes a literal ``%`` to the path it
    invokes. The leading ``%LOCALAPPDATA%`` token in the if-branch is
    intentionally left unescaped so cmd.exe expands it at runtime.
    """
    block = _shim_block(_read_install_script())
    # Both branches must escape literal '%' in the path segment they own.
    escape_pattern = r"-replace\s*'%'\s*,\s*'%%'"
    matches = re.findall(escape_pattern, block)
    assert len(matches) >= 2, (
        "install.ps1 must escape literal '%' (-replace '%', '%%') in both "
        "the under-%LOCALAPPDATA% branch (for $relative) and the else "
        "branch (for $releaseDir); cmd.exe would otherwise interpret "
        "stray '%foo%' segments as env-var references."
    )


def test_shim_carries_advisory_rem_about_localappdata_expansion() -> None:
    """The generated apm.cmd must carry an advisory REM comment.

    The original bug (#1509) was made worse because users would open
    apm.cmd and hand-edit the embedded path, hard-coding their
    expanded profile directory and re-introducing the encoding-mangle
    failure on the next install. A REM line inside the shim explains
    the file is generated and that cmd.exe expands %LOCALAPPDATA% at
    runtime, giving anyone who opens it the context they need before
    editing.
    """
    block = _shim_block(_read_install_script())
    assert re.search(r"REM\s+Generated by install\.ps1", block), (
        "install.ps1 must embed an advisory 'REM Generated by "
        "install.ps1' line in the apm.cmd shim content so editors of "
        "the generated file see the regeneration warning."
    )
    assert "microsoft/apm#1509" in block, (
        "The advisory REM in the shim should cite issue #1509 so the "
        "regression context is one search away from anyone reading the "
        "shim file."
    )
    assert re.search(r"REM\s+.*%LOCALAPPDATA%.*runtime", block), (
        "The advisory REM must explain that cmd.exe expands "
        "%LOCALAPPDATA% at runtime, naming the mechanism the fix "
        "depends on."
    )
