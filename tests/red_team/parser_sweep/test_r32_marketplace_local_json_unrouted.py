"""Round-32 PARSER-CHAOS: unrouted JSON sink in marketplace local/git fetch.

``marketplace.client.fetch_marketplace`` resolves a marketplace manifest from
attacker-controlled bytes. For ``kind == "local"`` sources (a marketplace
registered as a filesystem path -- e.g. a cloned/shared marketplace repo) the
manifest is read by ``_fetch_local_file`` / ``_fetch_local_direct_read`` with::

    with open(safe_file, encoding="utf-8") as f:
        return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise MarketplaceFetchError(...)

``json.load`` has two failure classes that the narrow except cannot catch:

* a >4300-digit integer literal raises a *bare* ``ValueError`` (the CPython
  int-string conversion limit). ``json.JSONDecodeError`` is a *subclass* of
  ``ValueError``, so catching the child does NOT catch the parent.
* a deeply nested document raises ``RecursionError`` (a ``RuntimeError``
  subclass, not a ``ValueError`` at all).

Either escapes ``fetch_marketplace``'s own ``except MarketplaceFetchError`` and
crashes a default command (``apm marketplace ...`` / ``apm install`` marketplace
resolution). The bounded JSON readers added in rounds 21-31 (``_bounded_read_json``,
``_read_capped_json``) were NOT applied to these local/git readers.

Sink: src/apm_cli/marketplace/client.py:_fetch_local_file /
      _fetch_local_direct_read  (sibling: _fetch_git :: generic-git read).

A benign manifest still parses cleanly (no false positive).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.marketplace import client
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import MarketplaceSource


def _local_source(directory: Path) -> MarketplaceSource:
    src = MarketplaceSource(name="evil", url=str(directory), path="marketplace.json")
    # Sanity: the directory must classify as a local-filesystem marketplace so
    # the fetch dispatches to _fetch_local (not a network kind).
    assert src.kind == "local"
    return src


def test_huge_int_marketplace_json_crashes_fetch(tmp_path: Path) -> None:
    """A >4300-digit integer literal escapes as a bare ValueError on HEAD."""
    mp = tmp_path / "mp"
    mp.mkdir()
    (mp / "marketplace.json").write_text('{"version":' + "9" * 5000 + "}", encoding="utf-8")
    source = _local_source(mp)

    # FAILS ON HEAD: a bare ValueError escapes the narrow except and the
    # fetch_marketplace `except MarketplaceFetchError`. The desired (fixed)
    # behaviour is a fail-closed MarketplaceFetchError.
    with pytest.raises(MarketplaceFetchError):
        client.fetch_marketplace(source)


def test_deeply_nested_marketplace_json_crashes_fetch(tmp_path: Path) -> None:
    """A deeply nested document escapes as RecursionError on HEAD."""
    mp = tmp_path / "mp"
    mp.mkdir()
    depth = 40000
    (mp / "marketplace.json").write_text('{"a":' * depth + "1" + "}" * depth, encoding="utf-8")
    source = _local_source(mp)

    # FAILS ON HEAD: RecursionError (RuntimeError subclass) is not caught by
    # `except (json.JSONDecodeError, OSError)` nor by the outer
    # `except MarketplaceFetchError`.
    with pytest.raises(MarketplaceFetchError):
        client.fetch_marketplace(source)


def test_benign_local_marketplace_still_parses(tmp_path: Path) -> None:
    """Control: a well-formed local marketplace.json parses with no error."""
    mp = tmp_path / "mp"
    mp.mkdir()
    payload = {"name": "ok", "owner": "o", "repo": "r", "plugins": []}
    (mp / "marketplace.json").write_text(json.dumps(payload), encoding="utf-8")
    source = _local_source(mp)

    manifest = client.fetch_marketplace(source)
    assert manifest is not None
