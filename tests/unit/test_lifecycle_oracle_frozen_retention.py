"""I3-X1: install --frozen keeps packages apm.yml no longer declares (#3662, #5282).

Frozen never rewrites apm.lock.yaml, so it keeps an undeclared package, names it
and exits 0, while ``audit --ci`` fails ``no-orphaned-packages`` on the same state.
I3 exempts exactly that pair, and the exempted step earns no I3 receipt. These
negative controls keep the exception narrow.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apm_cli.core.remediation import ORPHANED_PACKAGES
from tests.lifecycle import oracles

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I3")]

_KEPT = (
    "[i] Kept packages no longer in apm.yml because --frozen does not change "
    f"apm.lock.yaml: /w/packages/lsp-kit. {ORPHANED_PACKAGES.sentence()}\n"
)
_ORPHAN_ROW = "| [x]      | no-orphaned-packages     | 1 orphaned package(s) in lockfile |\n"
_DRIFT_ROW = "| [x]      | drift                    | drift detected: 1 file(s)         |\n"
_PASS_ROW = "| [+]      | lockfile-exists          | Lockfile present                  |\n"
_HINT = f"[i] {ORPHANED_PACKAGES.sentence()}\n"


def _i3(frozen_output: str, audit_output: str, *, frozen: int = 0, audit: int = 1):
    op = SimpleNamespace(
        name="install-frozen", paired={"audit": audit}, paired_output={"audit": audit_output}
    )
    result = SimpleNamespace(returncode=frozen, stdout=frozen_output, stderr="")
    return oracles._CHECKS["I3"](None, None, result, None, op)


def _json_audit(*failed: str) -> str:
    checks = [{"name": "lockfile-exists", "passed": True, "message": "ok"}]
    checks += [{"name": name, "passed": False, "message": "run 'apm install'"} for name in failed]
    return json.dumps({"passed": False, "checks": checks})


@pytest.mark.parametrize(
    "audit_output",
    [_PASS_ROW + _ORPHAN_ROW + _HINT, _json_audit("no-orphaned-packages")],
    ids=["text", "json"],
)
def test_a_named_frozen_keep_of_an_orphan_is_exempt_and_earns_no_receipt(
    audit_output: str,
) -> None:
    assert _i3(_KEPT, audit_output) is None


@pytest.mark.parametrize(
    "audit_output",
    [_ORPHAN_ROW + _DRIFT_ROW + _HINT, _json_audit("no-orphaned-packages", "drift")],
    ids=["text", "json"],
)
def test_another_failing_audit_check_still_fails_i3(audit_output: str) -> None:
    messages = [v.message for v in _i3(_KEPT, audit_output)]
    assert messages == ["audit --ci exited 1 but install --frozen exited 0"]


def test_a_frozen_install_that_does_not_name_the_keep_still_fails_i3() -> None:
    messages = [v.message for v in _i3("[+] Installed 1 package\n", _ORPHAN_ROW + _HINT)]
    assert messages == ["audit --ci exited 1 but install --frozen exited 0"]


def test_an_exempt_audit_without_a_remediation_fails_i3() -> None:
    messages = [v.message for v in _i3(_KEPT, _ORPHAN_ROW)]
    assert messages == ["audit --ci exited 1 without a remediation"]
