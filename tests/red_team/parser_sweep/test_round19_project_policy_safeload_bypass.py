"""Round-19 parser break r19-parser-1: project-policy raw ``yaml.safe_load`` bypass.

Rounds 3-18 routed every *known* untrusted-YAML sink through the
merge/alias-bounded ``_BoundedSafeLoader`` (``load_yaml`` /
``load_yaml_str`` / ``load_frontmatter``). But ``policy/project_config.py``
still reads the **untrusted project ``apm.yml``** with stock
``yaml.safe_load`` in two readers:

* ``read_project_fetch_failure_default`` -> ``_read_or_default`` (line ~101)
* ``read_project_policy_hash_pin``                        (line ~194)

Both are on DEFAULT-ON paths:

* ``read_project_fetch_failure_default`` is called by the ``apm install``
  policy gate (``install/phases/policy_gate.py`` ->
  ``_read_project_fetch_failure_default``) and by ``apm audit``
  (``commands/audit.py``) -- so it runs on a plain ``apm install`` /
  ``apm audit`` against a freshly cloned, untrusted repo.
* ``read_project_policy_hash_pin`` is called by ``policy/discovery.py``.

Stock ``yaml.safe_load`` has NO expansion budget, so a sub-kilobyte
merge-key billion-laughs ``apm.yml`` (``<<: [*a, *a]`` once per level)
drives its eager ``flatten_mapping`` to O(2^N) work that holds the GIL
and NEVER yields. The reader's ``except (OSError, yaml.YAMLError)`` cannot
preempt it -- no exception is ever raised; the call simply hangs. An
untrusted clone thus wedges ``apm install`` / ``apm audit`` into a
parse-time CPU DoS, violating Secure Contract #1.

The identical bomb is correctly REJECTED by the bounded loader
(``load_yaml`` raises ``yaml.YAMLError`` in ~0.02s), proving the fix is a
one-line redirect: parse these two readers through
``apm_cli.utils.yaml_io.load_yaml`` (which every sibling reader already
uses) instead of stock ``yaml.safe_load``.

These traps FAIL on the current head (the readers hang) and pass once the
sink is routed through the bounded loader.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from apm_cli.policy.project_config import (
    read_project_fetch_failure_default,
    read_project_policy_hash_pin,
)
from apm_cli.utils.yaml_io import load_yaml

from .conftest import run_guarded, write_apm_yml

# A wedged stock safe_load on this bomb runs for minutes; the bounded loader
# fails it closed in ~0.02s. A generous 6s ceiling cleanly separates a fast
# fail-closed read from an exponential hang.
_CEILING_S = 6.0


def _merge_bomb_under_policy(levels: int = 30) -> str:
    """Aliased ``<<: [*a, *a]`` doubling bomb, anchored under ``policy:``.

    The terminal anchor is merged into the ``policy`` mapping so the bomb
    sits exactly where the project-policy readers look, and a ~1.1KB file
    drives stock ``safe_load`` to O(2^N).
    """
    lines = ["a0: &a0", "  k0: 1"]
    prev = "a0"
    for i in range(1, levels):
        lines += [f"a{i}: &a{i}", f"  <<: [*{prev}, *{prev}]", f"  k{i}: 1"]
        prev = f"a{i}"
    lines += [
        "policy:",
        f"  <<: [*{prev}, *{prev}]",
        "  fetch_failure_default: block",
        "  hash: sha256:" + "a" * 64,
    ]
    return "\n".join(lines) + "\n"


def test_bounded_loader_rejects_the_same_bomb_fast(tmp_path: Path) -> None:
    """Baseline: the bounded loader fails the bomb closed (the fix target)."""
    apm_yml = write_apm_yml(tmp_path, _merge_bomb_under_policy(30))

    start = time.monotonic()
    with pytest.raises(yaml.YAMLError):
        load_yaml(apm_yml)
    assert time.monotonic() - start < _CEILING_S


def test_fetch_failure_default_reader_does_not_hang_on_bomb(tmp_path: Path) -> None:
    """``apm install`` / ``apm audit`` policy gate must not hang on the bomb.

    Drives the REAL ``read_project_fetch_failure_default`` -- the function
    the install policy gate and ``apm audit`` call -- against an untrusted
    merge-bomb ``apm.yml``. It must return (fail closed to the default)
    within the budget, never an exponential hang.
    """
    write_apm_yml(tmp_path, _merge_bomb_under_policy(30))

    finished, result, exc = run_guarded(
        lambda: read_project_fetch_failure_default(tmp_path), timeout=_CEILING_S
    )

    assert finished, (
        "read_project_fetch_failure_default hung on a ~1KB merge-bomb apm.yml "
        "-- stock yaml.safe_load has no expansion budget, so an untrusted clone "
        "wedges the apm install policy gate / apm audit into a CPU DoS"
    )
    # When fixed (routed through the bounded loader) the bomb raises
    # yaml.YAMLError, the reader's except clause catches it, and it returns
    # the safe default rather than crashing.
    assert exc is None, f"reader escaped a foreign exception: {exc!r}"
    assert result in {"warn", "block"}


def test_policy_hash_pin_reader_does_not_hang_on_bomb(tmp_path: Path) -> None:
    """``policy/discovery`` hash-pin read must not hang on the bomb.

    ``read_project_policy_hash_pin`` is the sibling raw-``safe_load`` reader
    reached from ``policy/discovery.py`` during install policy discovery.
    """
    write_apm_yml(tmp_path, _merge_bomb_under_policy(30))

    finished, _result, exc = run_guarded(
        lambda: read_project_policy_hash_pin(tmp_path), timeout=_CEILING_S
    )

    assert finished, (
        "read_project_policy_hash_pin hung on a ~1KB merge-bomb apm.yml "
        "-- stock yaml.safe_load bypasses the bounded loader on a default "
        "install policy-discovery path"
    )
    # A bounded-loader rejection surfaces as yaml.YAMLError, which the
    # reader's except clause turns into None (fail closed). It must never
    # escape a foreign (non-YAMLError) exception either.
    assert exc is None or isinstance(exc, yaml.YAMLError), (
        f"hash-pin reader escaped a foreign exception: {exc!r}"
    )


def test_legit_policy_apm_yml_still_reads(tmp_path: Path) -> None:
    """No false-positive: a normal policy block still reads correctly."""
    write_apm_yml(
        tmp_path,
        "policy:\n  fetch_failure_default: block\n",
    )
    assert read_project_fetch_failure_default(tmp_path) == "block"
