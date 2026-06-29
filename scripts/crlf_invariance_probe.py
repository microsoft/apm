#!/usr/bin/env python
"""Empirical cross-platform probe for the apm#1952 content-hash fix.

Exercises the REAL git ``core.autocrlf`` line-ending translation through the
product's own :func:`apm_cli.utils.content_hash.compute_file_hash` -- the
single function both the record side (``apm install``) and the verify side
(``apm audit``) call -- and prints the resulting envelope.

When run on a Windows runner with ``core.autocrlf=true``, git re-materializes
the committed sample with ``\\r\\n``; on POSIX it stays ``\\n``. After the fix
the computed hash MUST be identical on every platform. The companion workflow
(.github/workflows/crlf-invariance.yml) runs this on ubuntu/windows/macos and
asserts all three emitted hashes match byte-for-byte.

Also asserts in-process invariants as a local defense:
  * CRLF text and LF text hash equal
  * a bare CR still changes the hash (smuggling vector stays caught)
  * binary (NUL byte) content is hashed raw

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


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _autocrlf_roundtrip_hash(work: Path) -> tuple[str, bytes]:
    """Commit a LF sample, force autocrlf, re-check it out, hash on-disk bytes.

    Returns (envelope, on_disk_bytes). On a Windows runner the on-disk bytes
    come back CRLF; on POSIX they stay LF. The envelope must be identical.
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
    # Turn on the exact setting that triggers apm#1952, then re-materialize.
    _git(["config", "core.autocrlf", "true"], work)
    sample.unlink()
    _git(["checkout", "--", "sample.md"], work)
    on_disk = sample.read_bytes()
    return compute_file_hash(sample), on_disk


def _assert_in_process_invariants(work: Path) -> None:
    lf = work / "lf.md"
    lf.write_bytes(b"# H\n\ntext\n")
    crlf = work / "crlf.md"
    crlf.write_bytes(b"# H\r\n\r\ntext\r\n")
    assert compute_file_hash(lf) == compute_file_hash(crlf), (
        "CRLF and LF text must hash equal (the apm#1952 fix)"
    )

    bare_cr = work / "cr.md"
    bare_cr.write_bytes(b"# H\r\rtext\n")
    assert compute_file_hash(bare_cr) != compute_file_hash(lf), (
        "a bare CR must still change the hash (smuggling vector stays caught)"
    )

    bin1 = work / "a.bin"
    bin1.write_bytes(b"\x00\r\n\xff")
    h1 = compute_file_hash(bin1)
    bin1.write_bytes(b"\x00\n\xff")
    assert compute_file_hash(bin1) != h1, "binary content must be hashed raw"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the autocrlf-roundtrip envelope to this file",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        _assert_in_process_invariants(work)
        repo = work / "repo"
        repo.mkdir()
        envelope, on_disk = _autocrlf_roundtrip_hash(repo)

    eol = "CRLF" if b"\r\n" in on_disk else "LF"
    print(f"os={platform.system()} on_disk_eol={eol} hash={envelope}")
    if args.out is not None:
        args.out.write_text(envelope + "\n", encoding="utf-8", newline="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
