"""Red-team round 20 (PARSER / YAML-CHAOS): policy-path bypass-sink audit.

Same break class as r19-parser-1 (``policy/project_config`` parsed an
untrusted ``apm.yml`` with stock ``yaml.safe_load``), now in the POLICY
DISCOVERY / PARSE path -- two raw sinks that BYPASS the bounded loader:

  * ``policy.discovery._detect_garbage`` parses the body of a *remotely
    fetched* policy (``resp.text`` off the wire in ``_fetch_from_url`` /
    ``_fetch_from_repo`` -- attacker / captive-portal / MITM controlled)
    with stock ``yaml.safe_load`` (discovery.py:1376).
  * ``policy.parser.parse_policy`` (reached via ``load_policy`` and
    ``discovery._load_from_file``) parses untrusted policy YAML with
    stock ``yaml.safe_load`` (parser.py:442).

Both are reachable on the DEFAULT ``apm install`` path via the policy
gate (``install/phases/policy_gate.py`` -> ``discover_policy_with_chain``)
and on ``apm audit``. Neither routes through ``apm_cli.utils.yaml_io``;
both do ``import yaml`` directly.

THE BOMB: a YAML *merge-key chain* (``l_n: &l_n {<<: [*l_{n-1}, *l_{n-1}]}``).
PyYAML shares anchors as references, so pure-alias billion-laughs is
linear and harmless -- but ``SafeLoader.flatten_mapping`` is recursive
and NOT memoized at the node level, so a chain of merge keys is
re-expanded ``fan**depth`` times during the compose phase. A 40-deep,
fan-2 chain is ~1KB yet wedges ``yaml.safe_load`` for minutes in pure
Python (it never reaches construction). The bounded ``_BoundedSafeLoader``
caps the merge budget and rejects the SAME bytes in ~2ms as a catchable
``yaml.YAMLError`` (``ConstructorError``).

Watchdog: ``flatten_mapping`` is pure Python, so a ``SIGALRM`` fires
between bytecode ops and proves the hang in-process without leaving a
runaway child.
"""

from __future__ import annotations

import signal
import time

import pytest
import yaml

from apm_cli.utils.yaml_io import load_yaml_str


class _Timeout(Exception):
    pass


def _merge_chain_bomb(depth: int = 40, fan: int = 2) -> str:
    """A sub-2KB YAML merge-key chain that hangs stock ``yaml.safe_load``."""
    lines = ["l0: &l0 {a: 1}"]
    for d in range(1, depth):
        refs = ",".join([f"*l{d - 1}"] * fan)
        lines.append(f"l{d}: &l{d} {{<<: [{refs}]}}")
    return "\n".join(lines)


MERGE_BOMB = _merge_chain_bomb()


def _assert_bounded(call, *, ceiling: float = 5.0) -> None:
    """Run ``call()`` under a SIGALRM watchdog; require it to FINISH fast.

    Post-fix SECURE CONTRACT: the real policy sink now routes the bytes
    through the bounded loader, so a merge-chain bomb is rejected as a
    ``yaml.YAMLError`` (or a wrapping ``PolicyValidationError``) in
    milliseconds rather than wedging the parser. Any return OR any non-
    ``_Timeout`` exception inside the ceiling satisfies the contract; a
    ``_Timeout`` means the bypass is still open (the genuine break).
    """

    def _fire(*_):
        raise _Timeout()

    prev = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, ceiling)
    start = time.monotonic()
    hung = False
    try:
        call()
    except _Timeout:
        hung = True
    except Exception:
        hung = False  # bounded / fail-closed -- finished fast, not a hang
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)
    elapsed = time.monotonic() - start
    assert not hung, (
        f"sink hung past {ceiling:.1f}s on a <2KB merge bomb -- the raw "
        "yaml.safe_load bypass is still open (must route through the "
        "bounded loader)."
    )
    assert elapsed < ceiling, f"sink took {elapsed:.2f}s (expected fast fail-closed)"


def test_bomb_is_small_and_bounded_loader_rejects_fast():
    """Control: <2KB bomb, bounded loader fail-closed in <0.5s."""
    assert len(MERGE_BOMB.encode("utf-8")) < 2048
    start = time.monotonic()
    with pytest.raises(yaml.YAMLError):
        load_yaml_str(MERGE_BOMB)
    assert time.monotonic() - start < 0.5


def test_detect_garbage_bounded_on_remote_policy_bomb():
    """REAL sink discovery._detect_garbage (remote policy body) fails fast."""
    from apm_cli.policy.discovery import _detect_garbage

    _assert_bounded(
        lambda: _detect_garbage(MERGE_BOMB, "url:https://evil.example/policy.yml", "label", None)
    )


def test_parse_policy_bounded_on_untrusted_policy_bomb():
    """REAL sink parser.parse_policy (load_policy / _load_from_file) fails fast."""
    from apm_cli.policy.parser import load_policy

    _assert_bounded(lambda: load_policy(MERGE_BOMB))
