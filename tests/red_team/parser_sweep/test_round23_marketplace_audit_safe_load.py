"""Round-23 red-team probe: ``apm marketplace audit`` parser-bomb bypass.

Reachable from the user-facing command ``apm marketplace audit NAME``:

    cli -> commands/marketplace/audit.py::audit
        -> marketplace.audit.run_audit
            -> check_plugin
                -> fetch_plugin_apm_yml   <-- parses UNTRUSTED remote apm.yml

``fetch_plugin_apm_yml`` fetches each plugin's *own* ``apm.yml`` from that
plugin's third-party repo at its pinned ref (untrusted content) and parses it
with **stock** ``yaml.safe_load``, catching only ``yaml.YAMLError``:

    data = yaml.safe_load(raw.decode("utf-8", errors="replace"))

This bypasses the bounded loader (``load_yaml_str`` / ``_BoundedSafeLoader``)
the repo built precisely to fail closed on parser bombs. Two consequences:

1. CONTRACT VIOLATION (crash): ``fetch_plugin_apm_yml`` documents *"Never
   raises -- every failure is reported through the status enum"*. A hostile
   ``apm.yml`` with an oversized decimal int (bare ``ValueError`` past
   ``sys.int_max_str_digits``) or deep nesting (``RecursionError``) escapes the
   ``except yaml.YAMLError``. (``run_audit`` has a broad ``except Exception``
   that masks this as a *NETWORK_ERROR*, mislabelling a malformed manifest.)

2. WHOLE-RUN DoS (hang): a merge-key ``<<`` bomb expands O(2^N) inside stock
   ``safe_load``. This does NOT raise -- it hangs -- so ``run_audit``'s broad
   ``except`` cannot contain it. One hostile plugin wedges the entire
   ``apm marketplace audit`` run. There is no byte cap on this path (unlike
   ``builder.py``), and a size cap would not help an algorithmic bomb anyway.

The fix is to route the decode through ``load_yaml_str`` (which normalizes the
bomb / huge-int / deep-nest into a single ``yaml.YAMLError`` and applies the
merge/alias budget), so every failure becomes a graceful ``PARSE_ERROR``.

These probes assert SECURE behavior and therefore FAIL on the PR head.
"""

from __future__ import annotations

import threading
import time

import apm_cli.marketplace.audit as audit_mod
from apm_cli.marketplace.audit import FetchStatus, fetch_plugin_apm_yml
from apm_cli.marketplace.models import MarketplacePlugin, MarketplaceSource


def _plugin_and_source() -> tuple[MarketplacePlugin, MarketplaceSource]:
    """A plugin whose source resolves to addressable github coords."""
    plugin = MarketplacePlugin(name="evil", source={"type": "github", "repo": "owner/name"})
    source = MarketplaceSource(name="mkt", host="github.com")
    return plugin, source


def _run_audit_fetch(monkeypatch, payload: bytes):
    """Drive the real fetch+parse with ``fetch_raw`` stubbed to return *payload*."""
    plugin, source = _plugin_and_source()
    monkeypatch.setattr(audit_mod, "fetch_raw", lambda *a, **k: payload)
    return fetch_plugin_apm_yml(plugin, source)


def test_oversized_int_does_not_escape(monkeypatch):
    """A 6000-digit int must yield PARSE_ERROR, not a bare ValueError escape."""
    payload = ("bignum: " + "9" * 6000 + "\n").encode("utf-8")
    status, data, _detail = _run_audit_fetch(monkeypatch, payload)
    # Pre-fix: stock safe_load raises ValueError, which escapes the
    # `except yaml.YAMLError` and violates the documented "Never raises".
    assert status == FetchStatus.PARSE_ERROR
    assert data is None


def test_deep_nesting_does_not_escape(monkeypatch):
    """A deeply nested mapping must yield PARSE_ERROR, not RecursionError escape."""
    lines = []
    for i in range(2000):
        lines.append("  " * i + "k:")
    lines.append("  " * 2000 + "v: 1")
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    status, data, _detail = _run_audit_fetch(monkeypatch, payload)
    assert status == FetchStatus.PARSE_ERROR
    assert data is None


def _merge_bomb(levels: int) -> bytes:
    rows = ["l0: &l0 {k: v}"]
    for i in range(1, levels + 1):
        rows.append(f"l{i}: &l{i} {{<<: [*l{i - 1}, *l{i - 1}]}}")
    rows.append(f"boom: {{<<: [*l{levels}, *l{levels}]}}")
    return ("\n".join(rows) + "\n").encode("utf-8")


def test_merge_key_bomb_fails_closed_fast(monkeypatch):
    """An O(2^N) merge-key bomb must NOT hang the audit fetch.

    Stock ``yaml.safe_load`` flattens ``<<`` merges eagerly with no budget, so
    a ~26-level bomb runs for tens of seconds (a DoS that ``run_audit``'s
    ``except Exception`` cannot interrupt). The bounded loader rejects it in
    milliseconds. We assert the real fetch returns FAST.
    """
    payload = _merge_bomb(26)
    box: dict[str, object] = {}

    def run() -> None:
        t0 = time.time()
        try:
            box["result"] = _run_audit_fetch(monkeypatch, payload)
        except BaseException as exc:
            box["exc"] = type(exc).__name__
        box["elapsed"] = time.time() - t0

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(5.0)

    assert not worker.is_alive(), (
        "fetch_plugin_apm_yml hung >5s on a merge-key bomb -- stock safe_load "
        "has no merge budget; route the decode through load_yaml_str"
    )
    # And the fast return must be a graceful PARSE_ERROR (fail closed).
    result = box.get("result")
    assert result is not None, f"unexpected escape: {box.get('exc')}"
    status, data, _detail = result
    assert status == FetchStatus.PARSE_ERROR
    assert data is None


def test_benign_manifest_still_parses(monkeypatch):
    """A legit small apm.yml must still parse OK (no false-positive regression)."""
    payload = b"name: good-plugin\nversion: 1.2.3\ndescription: hello\n"
    status, data, _detail = _run_audit_fetch(monkeypatch, payload)
    assert status == FetchStatus.OK
    assert isinstance(data, dict)
    assert data["name"] == "good-plugin"
