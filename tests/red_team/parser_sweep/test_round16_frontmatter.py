"""Round-16 parser breaks r16-parser-1..2: frontmatter parsers off the bounded loader.

Eight files (13 ``frontmatter.load`` sinks) parsed installed-package ``.md``
frontmatter with the stock ``python-frontmatter`` ``YAMLHandler``, which calls
``yaml.load(..., Loader=SafeLoader)`` -- the UNBOUNDED loader, bypassing the
round-12 merge-entry budget and round-13 alias-expansion guard. Three of those
sinks are sibling integrators reachable from a DEFAULT path:

  * r16-parser-1 (HIGH): ``prompt_integrator`` / ``command_integrator`` /
    ``copilot_app_workflow_integrator`` parse a freshly-installed (untrusted)
    package's ``*.prompt.md`` frontmatter during ``apm install`` and during the
    ``apm audit`` drift replay. A merge-bomb frontmatter block hung the stock
    SafeLoader in an O(2^N) construct loop the surrounding ``except Exception``
    cannot preempt (the GIL is held inside the C construct loop). The fix adds a
    centralized ``load_frontmatter`` to ``utils/yaml_io.py`` that forces the
    bounded ``_BoundedSafeLoader`` via a ``_BoundedYAMLHandler`` subclass, and
    routes all 13 sinks through it.
  * r16-parser-2 (MED): ``command_integrator._transform_prompt_to_command``
    parsed frontmatter with an UNWRAPPED ``load``; a malformed block raised an
    uncaught ``yaml.YAMLError`` that, on the ``apm audit`` drift-replay loop
    (``install/drift.py::run_replay`` -- no per-package try/except), crashed the
    WHOLE audit with a traceback. The fix wraps both the gemini and the shared
    ``integrate_command`` branches in ``integrate_commands_for_target`` with
    ``except yaml.YAMLError`` -> per-file ``diagnostics.warn`` + ``files_skipped``
    + ``continue`` (fail closed), and the unwrapped parse now raises the exact
    ``yaml.YAMLError`` those wraps catch (proven below).

The watchdog uses a daemon thread + ``join``/``is_alive`` (no pytest-timeout
dependency, per the runtime's process-kill ban). Benign frontmatter must still
parse correctly.
"""

from __future__ import annotations

import threading
import time

import pytest
import yaml

from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.utils.yaml_io import load_frontmatter


def _merge_bomb_frontmatter(levels: int = 30) -> str:
    """A ``.prompt.md`` whose frontmatter merged value-list doubles each level."""
    lines = ["---", "a: &a {k: v}"]
    for i in range(1, levels + 1):
        prev = "a" if i == 1 else f"m{i - 1}"
        lines.append(f"m{i}: &m{i}")
        lines.append(f"  <<: [*{prev}, *{prev}]")
    lines.append(f"description: *m{levels}")
    lines.append("---")
    lines.append("# bomb body")
    return "\n".join(lines) + "\n"


def _alias_bomb_frontmatter(levels: int = 30) -> str:
    """A pure-alias billion-laughs frontmatter block (no merge keys)."""
    lines = ["---", "l0: &l0 [x, x]"]
    for i in range(1, levels + 1):
        lines.append(f"l{i}: &l{i} [*l{i - 1}, *l{i - 1}]")
    lines.append(f"description: *l{levels}")
    lines.append("---")
    lines.append("# alias bomb body")
    return "\n".join(lines) + "\n"


def _run_fast(fn, label: str, budget: float = 15.0):
    """Run *fn* on a daemon thread; fail if it does not return within *budget*."""
    result: dict[str, object] = {}

    def go():
        t0 = time.time()
        try:
            result["val"] = fn()
        except BaseException as exc:  # watchdog records any exit
            result["exc"] = exc
        result["dt"] = time.time() - t0

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(budget)
    assert not th.is_alive(), f"{label} HUNG >{budget}s (frontmatter SafeLoader bypass)"
    return result


# --------------------------------------------------------------------------- #
# r16-parser-1 -- merge / alias bomb in frontmatter must fail fast              #
# --------------------------------------------------------------------------- #


def test_load_frontmatter_rejects_merge_bomb_fast(tmp_path):
    """The centralized ``load_frontmatter`` raises (no hang) on a merge bomb."""
    bomb = tmp_path / "bomb.prompt.md"
    bomb.write_text(_merge_bomb_frontmatter())
    res = _run_fast(lambda: load_frontmatter(str(bomb)), "load_frontmatter(merge)")
    assert isinstance(res.get("exc"), yaml.YAMLError), res


def test_load_frontmatter_rejects_alias_bomb_fast(tmp_path):
    """The centralized ``load_frontmatter`` raises (no hang) on an alias bomb."""
    bomb = tmp_path / "bomb.prompt.md"
    bomb.write_text(_alias_bomb_frontmatter())
    res = _run_fast(lambda: load_frontmatter(str(bomb)), "load_frontmatter(alias)")
    assert isinstance(res.get("exc"), yaml.YAMLError), res


def test_command_integrator_transform_rejects_bomb_fast(tmp_path):
    """The real ``_transform_prompt_to_command`` integrator entry fails fast."""
    bomb = tmp_path / "evil.prompt.md"
    bomb.write_text(_merge_bomb_frontmatter())
    ci = CommandIntegrator()
    res = _run_fast(
        lambda: ci._transform_prompt_to_command(bomb),
        "command_integrator._transform_prompt_to_command",
    )
    assert isinstance(res.get("exc"), yaml.YAMLError), res


# --------------------------------------------------------------------------- #
# r16-parser-2 -- malformed frontmatter raises the catchable YAMLError type     #
# --------------------------------------------------------------------------- #

_MALFORMED = "---\nkey: : : broken\n  - bad: [unclosed\n---\nbody\n"


def test_load_frontmatter_malformed_raises_yamlerror(tmp_path):
    """A malformed frontmatter block raises ``yaml.YAMLError`` (catchable type)."""
    bad = tmp_path / "bad.prompt.md"
    bad.write_text(_MALFORMED)
    with pytest.raises(yaml.YAMLError):
        load_frontmatter(str(bad))


def test_command_transform_malformed_raises_yamlerror(tmp_path):
    """The unwrapped integrator parse raises the exact type the wrap catches.

    ``integrate_commands_for_target`` catches ``yaml.YAMLError`` around the
    transform; if a malformed block raised a DIFFERENT type the audit
    drift-replay would still crash. Pin the contract here.
    """
    bad = tmp_path / "bad.prompt.md"
    bad.write_text(_MALFORMED)
    ci = CommandIntegrator()
    with pytest.raises(yaml.YAMLError):
        ci._transform_prompt_to_command(bad)


# --------------------------------------------------------------------------- #
# control -- benign frontmatter still parses                                    #
# --------------------------------------------------------------------------- #


def test_benign_frontmatter_still_parses(tmp_path):
    """A legitimate prompt frontmatter block parses correctly through the loader."""
    good = tmp_path / "good.prompt.md"
    good.write_text(
        "---\n"
        "description: A helpful prompt\n"
        "model: gpt-4o\n"
        "allowed-tools: [read, write]\n"
        "---\n"
        "# Body\nDo the thing.\n"
    )
    post = load_frontmatter(str(good))
    assert post.metadata["description"] == "A helpful prompt"
    assert post.metadata["model"] == "gpt-4o"
    assert post.metadata["allowed-tools"] == ["read", "write"]
    assert "Do the thing." in post.content


def test_benign_frontmatter_with_reused_anchor_parses(tmp_path):
    """A benign reused-anchor DAG (not a bomb) must still resolve."""
    good = tmp_path / "anchor.prompt.md"
    good.write_text(
        "---\ncommon: &c {tool: read}\na: *c\nb: *c\ndescription: anchored\n---\n# body\n"
    )
    post = load_frontmatter(str(good))
    assert post.metadata["a"] == {"tool": "read"}
    assert post.metadata["b"] == {"tool": "read"}
    assert post.metadata["description"] == "anchored"
