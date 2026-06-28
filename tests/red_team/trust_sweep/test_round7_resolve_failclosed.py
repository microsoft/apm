"""Round-7 trust regression trap: resolve() OSError must fail CLOSED.

r7-trust-1 (MED) -- ``is_fingerprint_trusted`` called ``str(script_file.resolve())``
unguarded. When the project apm.yml is a symlink being swapped concurrently
(``os.replace`` race), ``Path.resolve()`` can raise ``OSError(EINVAL)`` in the
swap window. That call sits on the FIRING boundary: ``install/service.py`` does
NOT wrap ``build_runner_from_context`` in ``suppress`` the way update/uninstall
do, so a propagated OSError aborts the whole ``apm install`` -- a
fail-not-closed DoS (NOT a trust bypass; a stored fingerprint still has to
match). The fix wraps ``resolve()`` in the trust gate itself: on OSError treat
the tier as untrusted (return False) so the install/update/uninstall flow
proceeds without the project's scripts.

Run:
    uv run --extra dev pytest tests/red_team/trust_sweep/test_round7_resolve_failclosed.py -q
"""

from __future__ import annotations

import pytest

from apm_cli.core import script_trust as st


class _ResolveRaises:
    """Duck-typed stand-in: only .resolve() is exercised by the trust gate."""

    def __init__(self, errno_code: int) -> None:
        self._errno = errno_code

    def resolve(self):
        raise OSError(self._errno, "swap window")


@pytest.mark.parametrize("errno_code", [22, 40, 2])
def test_resolve_oserror_returns_false_not_raises(monkeypatch, tmp_path, errno_code):
    """A resolve() OSError must fail closed (False), never propagate."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    result = st.is_fingerprint_trusted(_ResolveRaises(errno_code), "deadbeef")
    assert result is False


def test_none_fingerprint_short_circuits_before_resolve(monkeypatch, tmp_path):
    """None fingerprint returns False without even touching resolve()."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    assert st.is_fingerprint_trusted(_ResolveRaises(22), None) is False


def test_trusted_match_still_works_after_guard(tmp_path, monkeypatch):
    """The fail-closed guard must not break the normal trusted-match path."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    apm = tmp_path / "apm.yml"
    apm.write_text(
        "name: x\nversion: 1\nlifecycle:\n  post-install:\n    - run: echo hi\n",
        encoding="utf-8",
    )
    fp = st.trust_project_scripts(apm)
    assert fp is not None
    assert st.is_fingerprint_trusted(apm, fp) is True
    assert st.is_fingerprint_trusted(apm, "not-the-fp") is False
