"""Round-8 trust regression trap: ELOOP symlink-loop fail-closed.

r8-trust-1 (MED) -- the round-7 fix wrapped ``script_file.resolve()`` in
``is_fingerprint_trusted`` with ``except OSError`` to fail CLOSED on a
concurrent symlink swap. But on CPython 3.12+, ``pathlib.Path.resolve()``
detects an ELOOP symlink loop and raises ``RuntimeError`` (NOT a subclass
of ``OSError``), so the guard was bypassed and the error propagated out of
the firing boundary -- ``install/service.py`` does not wrap the trust
check, so the ``RuntimeError`` aborted ``apm install`` (a fail-not-closed
DoS, not a trust bypass). The fix broadens the except to
``(OSError, RuntimeError, ValueError)``.
"""

from __future__ import annotations

import os

from apm_cli.core.script_trust import is_fingerprint_trusted


def test_eloop_symlink_loop_fails_closed(tmp_path):
    """A symlink loop must fail CLOSED (return False), never raise."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    os.symlink(a, b)
    os.symlink(b, a)

    # Must return False (untrusted) without raising -- not RuntimeError.
    assert is_fingerprint_trusted(a, "0" * 64) is False


def test_none_fingerprint_short_circuits(tmp_path):
    """A None fingerprint is untrusted regardless of the path."""
    assert is_fingerprint_trusted(tmp_path / "apm.yml", None) is False


def test_resolvable_path_still_evaluated(tmp_path, monkeypatch):
    """The fail-closed guard must not break the normal trusted-match path."""
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    real = tmp_path / "apm.yml"
    real.write_text("lifecycle:\n  post-install:\n    - {type: command, command: echo hi}\n")
    from apm_cli.core import script_trust

    fp = script_trust.script_file_fingerprint(real)
    assert is_fingerprint_trusted(real, fp) is False  # not yet trusted
    script_trust.trust_project_scripts(real)
    assert is_fingerprint_trusted(real, fp) is True
