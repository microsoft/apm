"""Round-17 parser red-team regression traps.

Two genuine breaks closed this round, both instances of the persistent
PARSER break-class -- a raw / under-guarded deserialization sink reachable
from untrusted input on a default command path:

* r17-parser-1 (HIGH): ``bundle/local_bundle._read_bundle_lockfile`` parsed an
  untrusted bundle's ``apm.lock.yaml`` with stock ``yaml.safe_load`` (bypassing
  the round-16 bounded loader). ``apm install <bundle>`` reaches it via
  ``detect_local_bundle`` BEFORE any trust gate (a directory qualifies on
  ``plugin.json`` presence alone), so a sub-kilobyte merge-key bomb hung
  install detection forever. Now routed through ``load_yaml_str`` -> the bomb
  fails closed as ``yaml.YAMLError`` (already caught) in milliseconds.

* r17-parser-2 (MED): the bounded loader has no huge-int digit cap and no
  recursion cap, so a 6000-digit decimal frontmatter scalar reaches CPython's
  ``int()`` -> ``ValueError`` (past ``sys.int_max_str_digits``) and a deeply
  nested document raises ``RecursionError`` -- NEITHER is a ``yaml.YAMLError``.
  They escaped the integrators' ``except yaml.YAMLError`` wrappers and aborted
  the whole ``apm audit`` drift replay (one hostile ``.md`` -> whole-run DoS).
  ``yaml_io._bounded_load`` now normalizes both into ``yaml.YAMLError`` so every
  fail-closed handler catches them as one class.

Each bomb probe runs in a daemon thread with a bounded ``join`` so a genuine
hang fails the test (thread stays alive) instead of wedging the suite; the
runtime bans ``timeout``/``pytest-timeout``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import yaml

from apm_cli.utils.yaml_io import load_frontmatter, load_yaml_str

_JOIN_TIMEOUT = 6.0


def _merge_bomb(levels: int = 32) -> str:
    """A linear-size YAML whose ``<<`` merges double per level -> O(2^N)."""
    lines = ["a0: &a0", "  k: v"]
    for i in range(1, levels + 1):
        prev = f"a{i - 1}"
        lines += [f"a{i}: &a{i}", f"  <<: [*{prev}, *{prev}]", "  k: v"]
    return "\n".join(lines) + "\n"


def _run_bounded(fn) -> dict:
    """Run *fn* in a daemon thread; record outcome. Hang => thread stays alive."""
    box: dict = {}

    def _worker() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:
            box["exc"] = exc

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(_JOIN_TIMEOUT)
    box["alive"] = th.is_alive()
    return box


# ---------------------------------------------------------------------------
# r17-parser-1: bundle lockfile sink routed through the bounded loader
# ---------------------------------------------------------------------------


def test_bundle_lockfile_merge_bomb_fails_closed(tmp_path: Path) -> None:
    """detect_local_bundle on a merge-bomb apm.lock.yaml must not hang."""
    from apm_cli.bundle.local_bundle import detect_local_bundle

    bdir = tmp_path / "evil"
    bdir.mkdir()
    (bdir / "plugin.json").write_text(
        json.dumps({"name": "evil", "version": "1.0.0"}), encoding="utf-8"
    )
    (bdir / "apm.lock.yaml").write_text(_merge_bomb(32), encoding="utf-8")

    box = _run_bounded(lambda: detect_local_bundle(bdir))

    assert not box["alive"], "detect_local_bundle hung on a bundle lockfile merge bomb"
    # The bomb is swallowed by _read_bundle_lockfile's except -> lockfile None.
    # detect_local_bundle still returns a bundle info (plugin.json present),
    # it simply carries no pack targets. The point is: it RETURNS.
    assert "exc" not in box or isinstance(box["exc"], (yaml.YAMLError,))


def test_bundle_lockfile_str_bomb_raises_fast() -> None:
    """The lockfile body via load_yaml_str raises YAMLError, not a hang."""
    box = _run_bounded(lambda: load_yaml_str(_merge_bomb(32)))
    assert not box["alive"], "load_yaml_str hung on a merge bomb"
    assert isinstance(box.get("exc"), yaml.YAMLError)


def test_bundle_lockfile_benign_still_parses(tmp_path: Path) -> None:
    """A normal bundle lockfile still parses to its pack targets."""
    from apm_cli.bundle.local_bundle import _extract_pack_targets, _read_bundle_lockfile

    bdir = tmp_path / "good"
    bdir.mkdir()
    (bdir / "apm.lock.yaml").write_text(
        "pack:\n  target: ['copilot', 'claude']\n", encoding="utf-8"
    )
    parsed = _read_bundle_lockfile(bdir)
    assert parsed is not None
    assert _extract_pack_targets(parsed) == ["copilot", "claude"]


# ---------------------------------------------------------------------------
# r17-parser-2: huge-int / deep-nest normalized to YAMLError centrally
# ---------------------------------------------------------------------------


def test_huge_int_scalar_normalized_to_yaml_error() -> None:
    """A 6000-digit decimal scalar raises YAMLError, not bare ValueError."""
    body = "bignum: " + ("1" * 6000) + "\n"
    with pytest.raises(yaml.YAMLError):
        load_yaml_str(body)


def test_huge_int_frontmatter_normalized_to_yaml_error(tmp_path: Path) -> None:
    """load_frontmatter on a huge-int scalar raises YAMLError (per-file skip)."""
    md = tmp_path / "evil.prompt.md"
    md.write_text(
        "---\ndescription: hi\nbignum: " + ("1" * 6000) + "\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(yaml.YAMLError):
        with open(md, encoding="utf-8") as fh:
            load_frontmatter(fh)


def test_command_integrator_huge_int_does_not_escape(tmp_path: Path) -> None:
    """The audit-replay path: a hostile prompt frontmatter must not raise a
    non-YAMLError that escapes the integrator's except wrapper."""
    from unittest.mock import MagicMock

    from apm_cli.integration.command_integrator import CommandIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS

    proj = tmp_path
    (proj / ".gemini" / "commands").mkdir(parents=True)
    pd = proj / "apm_modules" / "evil" / ".apm" / "prompts"
    pd.mkdir(parents=True)
    (pd / "evil.prompt.md").write_text(
        "---\ndescription: hi\nbignum: " + ("1" * 6000) + "\n---\nbody\n",
        encoding="utf-8",
    )
    info = MagicMock()
    info.install_path = proj / "apm_modules" / "evil"
    info.resolved_reference = None
    info.package = MagicMock()
    info.package.name = "evil"

    # Must NOT raise an uncaught ValueError/RecursionError. The integrator's
    # `except yaml.YAMLError` now catches the normalized failure and skips the
    # file; the call returns (possibly integrating nothing) rather than aborting.
    try:
        CommandIntegrator().integrate_commands_for_target(KNOWN_TARGETS["gemini"], info, proj)
    except yaml.YAMLError:
        pass  # acceptable fail-closed signal; never a bare ValueError
    except (ValueError, RecursionError) as exc:  # pragma: no cover - regression
        pytest.fail(f"non-YAMLError escaped the integrator wrapper: {exc!r}")


def test_deep_nest_normalized_to_yaml_error() -> None:
    """A deeply-nested document raises YAMLError (normalized RecursionError)."""
    depth = 60_000
    body = "x: " + ("[" * depth) + ("]" * depth) + "\n"
    box = _run_bounded(lambda: load_yaml_str(body))
    assert not box["alive"], "load_yaml_str hung on a deep-nest document"
    # Either the expansion guard (YAMLError) or normalized RecursionError ->
    # both surface as YAMLError; never a bare RecursionError.
    exc = box.get("exc")
    assert exc is None or isinstance(exc, yaml.YAMLError), (
        f"deep-nest produced a non-YAMLError: {exc!r}"
    )


def test_benign_frontmatter_still_parses(tmp_path: Path) -> None:
    """A normal prompt frontmatter still parses to its metadata."""
    md = tmp_path / "ok.prompt.md"
    md.write_text(
        "---\ndescription: a normal prompt\ncount: 3\n---\nbody text\n",
        encoding="utf-8",
    )
    with open(md, encoding="utf-8") as fh:
        post = load_frontmatter(fh)
    assert post.metadata["description"] == "a normal prompt"
    assert post.metadata["count"] == 3
