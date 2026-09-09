#!/usr/bin/env python
"""Empirical cross-platform probe for the apm#1952 content-hash fix.

Exercises the REAL git ``core.autocrlf`` line-ending translation through the
product's own :func:`apm_cli.utils.content_hash.compute_file_hash` -- the
single function both the record side (``apm install``) and the verify side
(``apm audit``) call -- and prints the resulting envelope.

When run on a Windows runner (``core.autocrlf=true``) git re-materializes the
committed sample with ``\\r\\n``; on POSIX (``core.autocrlf=input``) it stays
``\\n``. After the fix the computed hash MUST be identical on every platform.
The companion workflow (.github/workflows/crlf-invariance.yml) runs this on
ubuntu/windows/macos and asserts all three emitted hashes match byte-for-byte.

Also probes the apm#2619 fix: marketplace-plugin installs synthesize an
``apm.yml`` (``synthesize_apm_yml_from_plugin``), rewrite it with the short
commit SHA (``stamp_plugin_version``), and serialize inline plugin hooks to
``.apm/hooks/hooks.json``. Those files are written by APM itself -- not
checked out by git -- into the tree :func:`compute_package_hash` hashes raw,
so platform-native newlines made the lockfile ``content_hash`` diverge across
OSes. The probe runs the REAL synthesize+stamp chain (including the
inline-hooks writer) and emits the package hash alongside the file hash; the
gather job asserts both are byte-identical on ubuntu/windows/macos.

Also enforces in-process invariants as a local defense (via hard checks, not
``assert``, so they survive ``python -O``):
  * CRLF text and LF text hash equal
  * a bare CR still changes the hash (smuggling vector stays caught)
  * binary (NUL byte) content is hashed raw
  * the synthesized+stamped apm.yml and inline hooks.json contain no CR bytes

Usage:
    python scripts/crlf_invariance_probe.py --out hash.txt
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

# Allow running from a source checkout without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apm_cli.utils.content_hash import compute_file_hash

SAMPLE_TEXT = b"# Title\n\nLine one.\nLine two.\n\n- bullet\n"

# Fake pinned commit used to exercise stamp_plugin_version deterministically.
PROBE_STAMP_SHA = "2c7ec5e78b8e5d43ea02e90bb8826f6b9f147b0c"


def _check(condition: bool, message: str) -> None:
    """Hard-fail the probe when a load-bearing invariant does not hold.

    Deliberately not ``assert``: these checks are the probe's actual
    detectors for platform-INVARIANT regressions (bytes wrong the same
    way on every OS pass the gather job's cross-OS uniqueness check), so
    they must survive ``python -O`` / ``PYTHONOPTIMIZE``.
    """
    if not condition:
        raise SystemExit(f"probe check failed: {message}")


def build_synthetic_plugin_fixture(pkg: Path) -> None:
    """Create the minimal marketplace-plugin fixture for apm#2619 probing.

    Single source of truth shared with the test suite
    (tests/integration/test_install_content_hash_roundtrip.py imports this)
    so the CI probe and the tests always exercise the same tree shape.

    The plugin manifest declares an INLINE ``hooks`` object so the
    ``.apm/hooks/hooks.json`` writer -- the second apm#2619 fix site -- is
    exercised, not just the synthesized ``apm.yml``. Every file the fixture
    itself creates is written with ``write_bytes`` so the only
    platform-sensitive writes are the product code paths under test.
    """
    skill = pkg / "skills" / "demo"
    skill.mkdir(parents=True)
    (pkg / "plugin.json").write_bytes(
        b'{"name": "demo-plugin", "description": "Demo", '
        b'"hooks": {"PreToolUse": [{"matcher": "Bash", '
        b'"hooks": [{"type": "command", "command": "echo hi"}]}]}}\n'
    )
    (skill / "SKILL.md").write_bytes(b"---\nname: demo\ndescription: Demo skill\n---\n\n# Demo\n")


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _autocrlf_roundtrip_hash(work: Path) -> tuple[str, bytes]:
    """Commit a LF sample, apply the platform's real autocrlf, hash on-disk bytes.

    Returns (envelope, on_disk_bytes). To faithfully reproduce the apm#1952
    split we use the setting each platform actually ships with: ``true`` on
    Windows (git materializes the working tree with ``\\r\\n``) and ``input``
    on POSIX (no checkout translation, so the committed ``\\n`` stays ``\\n``).
    The on-disk bytes therefore come back CRLF on Windows and LF on POSIX --
    exactly the Windows-vs-POSIX divergence that produced the false drift --
    and the canonical envelope MUST be identical across all of them.
    """
    _git(["init", "-q"], work)
    _git(["config", "user.email", "probe@example.com"], work)
    _git(["config", "user.name", "probe"], work)
    # Commit the sample with canonical LF content and no .gitattributes so the
    # ambient core.autocrlf governs checkout translation.
    sample = work / "sample.md"
    sample.write_bytes(SAMPLE_TEXT)
    _git(["add", "sample.md"], work)
    _git(["commit", "-q", "-m", "sample"], work)
    # Windows devs run autocrlf=true (CRLF working tree); POSIX devs and CI run
    # autocrlf=input (LF working tree). Picking per-OS reproduces the genuine
    # cross-platform on-disk split rather than forcing CRLF everywhere.
    autocrlf = "true" if platform.system() == "Windows" else "input"
    _git(["config", "core.autocrlf", autocrlf], work)
    sample.unlink()
    _git(["checkout", "--", "sample.md"], work)
    on_disk = sample.read_bytes()
    return compute_file_hash(sample), on_disk


def _synthetic_manifest_package_hash(work: Path) -> str:
    """Run the REAL marketplace-plugin synthesize+stamp chain, hash the tree.

    apm#2619: the synthetic ``apm.yml`` and inline-hooks ``hooks.json`` are
    written by APM itself (not materialized by git), so
    ``compute_package_hash`` -- which hashes raw bytes -- diverged across
    OSes while the writers used platform-native newlines.
    """
    from apm_cli.deps.package_validator import stamp_plugin_version
    from apm_cli.models.validation import validate_apm_package
    from apm_cli.utils.content_hash import compute_package_hash

    pkg = work / "pkg"
    build_synthetic_plugin_fixture(pkg)

    result = validate_apm_package(pkg)
    _check(result.is_valid, f"probe fixture invalid: {result.errors}")
    stamp_plugin_version(
        result.package,
        result.package_type,
        PROBE_STAMP_SHA,
        pkg,
    )
    manifest = (pkg / "apm.yml").read_bytes()
    _check(b"\r" not in manifest, "synthesized apm.yml must be LF-only (apm#2619)")
    hooks_json_path = pkg / ".apm" / "hooks" / "hooks.json"
    _check(hooks_json_path.is_file(), "inline hooks must synthesize .apm/hooks/hooks.json")
    _check(
        b"\r" not in hooks_json_path.read_bytes(),
        "inline hooks.json must be LF-only (apm#2619)",
    )
    return compute_package_hash(pkg)


def _check_in_process_invariants(work: Path) -> None:
    lf = work / "lf.md"
    lf.write_bytes(b"# H\n\ntext\n")
    crlf = work / "crlf.md"
    crlf.write_bytes(b"# H\r\n\r\ntext\r\n")
    _check(
        compute_file_hash(lf) == compute_file_hash(crlf),
        "CRLF and LF text must hash equal (the apm#1952 fix)",
    )

    bare_cr = work / "cr.md"
    bare_cr.write_bytes(b"# H\r\rtext\n")
    _check(
        compute_file_hash(bare_cr) != compute_file_hash(lf),
        "a bare CR must still change the hash (smuggling vector stays caught)",
    )

    bin1 = work / "a.bin"
    bin1.write_bytes(b"\x00\r\n\xff")
    h1 = compute_file_hash(bin1)
    bin1.write_bytes(b"\x00\n\xff")
    _check(compute_file_hash(bin1) != h1, "binary content must be hashed raw")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "write the combined envelope to this file: "
            "'<autocrlf-roundtrip file hash>|<synthetic-manifest package hash>'"
        ),
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        _check_in_process_invariants(work)
        repo = work / "repo"
        repo.mkdir()
        envelope, on_disk = _autocrlf_roundtrip_hash(repo)
        synth = work / "synth"
        synth.mkdir()
        package_envelope = _synthetic_manifest_package_hash(synth)

    eol = "CRLF" if b"\r\n" in on_disk else "LF"
    print(
        f"os={platform.system()} on_disk_eol={eol} "
        f"file_hash={envelope} synthetic_pkg_hash={package_envelope}"
    )
    if args.out is not None:
        # Single line so the gather job's whole-file uniqueness check covers
        # both the per-file (apm#1952) and package-tree (apm#2619) envelopes.
        args.out.write_text(
            f"{envelope}|{package_envelope}\n",
            encoding="utf-8",
            newline="",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
