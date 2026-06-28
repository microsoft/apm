"""Round-30 parser break r30-parser-1: workspace ``~/.apm/apm.yml`` merge bomb.

The registry-precedence loader merges a ``registries:`` map from several
layers. The workspace layer is read straight off ``~/.apm/apm.yml`` by
``apm_cli.deps.registry.config_loader._load_yaml_registries`` using stock
``yaml.safe_load`` -- it never routes through the bounded ``load_yaml``
loader. PyYAML's eager ``flatten_mapping`` expands chained aliased merge
keys (``<<: [*a, *a, *a]``) with O(3^N) cumulative ``merge.extend`` volume,
so a sub-kilobyte file drives ``safe_load`` to seconds-to-minutes of pure-CPU
burn. The function's ``except Exception: return {}`` guard does NOT help:
a CPU/OOM blow-up at construction time is a HANG, not an exception, so the
guard never fires.

``_load_yaml_registries`` is default-reachable: ``APMPackage.from_apm_yml``
(the core manifest loader behind ``apm install``, ``apm lifecycle``,
``apm audit``, ``apm pack``) calls ``resolve_effective_registries`` ->
``load_merged_registries``, which reads ``~/.apm/apm.yml`` whenever it
exists -- regardless of how benign the project ``apm.yml`` is. A poisoned
workspace file therefore wedges essentially every default command into a
parse-time CPU DoS.

SECURE contract: a hostile workspace file fails closed FAST (the loader
already swallows parse errors into ``{}``), so the call must RETURN within a
small fixed budget. Routing ``_load_yaml_registries`` through the bounded
``load_yaml`` makes the merge bomb raise ``yaml.YAMLError`` in ~ms, which the
existing ``except Exception`` collapses to ``{}``.

Red-before: on the current head the stock ``safe_load`` burns ~6s at n=15
(and climbs 3x per level), so the guarded call does NOT finish inside the
budget -> ``finished is False``.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    __import__("os").environ.get("APM_E2E_TESTS") != "1",
    reason="red-team parser sweep runs under APM_E2E_TESTS=1",
)

# Wall-clock ceiling for a fail-closed parse. The fix returns in ~ms; the
# pre-fix stock safe_load at n=15 is ~6s and climbs 3x per added level.
_BUDGET_SECONDS = 3.0
_BOMB_LEVELS = 15


def _run_guarded(fn, timeout: float):
    """Run *fn* in a daemon thread; return (finished, result, exception)."""
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:
            box["exception"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout)
    return (not thread.is_alive()), box.get("result"), box.get("exception")


def _merge_bomb(levels: int) -> str:
    """A linear-size manifest whose chained ``<<`` merges expand O(3^N)."""
    lines = ["a0: &a0 {k: v}"]
    for i in range(1, levels):
        prev = i - 1
        lines.append(f"a{i}: &a{i}")
        lines.append(f"  <<: [*a{prev}, *a{prev}, *a{prev}]")
        lines.append(f"  k{i}: v")
    lines.append("registries:")
    lines.append(f"  reg{levels}:")
    lines.append(f"    <<: [*a{levels - 1}, *a{levels - 1}]")
    lines.append("    url: https://example.invalid/reg")
    return "\n".join(lines)


def _write_workspace_bomb(home: Path) -> Path:
    apm_dir = home / ".apm"
    apm_dir.mkdir(parents=True, exist_ok=True)
    target = apm_dir / "apm.yml"
    target.write_text(_merge_bomb(_BOMB_LEVELS), encoding="utf-8")
    return target


def test_load_yaml_registries_merge_bomb_fails_closed_fast(tmp_path):
    """The real seam must fail closed FAST on a workspace merge bomb."""
    from apm_cli.deps.registry.config_loader import _load_yaml_registries

    bomb_path = _write_workspace_bomb(tmp_path)

    finished, result, exc = _run_guarded(lambda: _load_yaml_registries(bomb_path), _BUDGET_SECONDS)

    assert finished, (
        f"_load_yaml_registries did not return within {_BUDGET_SECONDS}s on a "
        f"{bomb_path.stat().st_size}-byte merge bomb -- stock yaml.safe_load "
        "CPU DoS bypasses the bounded loader (the except Exception guard cannot "
        "catch a hang)."
    )
    # Fail-closed: a hostile workspace file yields no registries, never a crash.
    assert exc is None, f"expected fail-closed empty map, got {exc!r}"
    assert result == {}


def test_load_merged_registries_workspace_bomb_fast(tmp_path, monkeypatch):
    """Default-reachable layer merge must not hang on a poisoned ~/.apm/apm.yml."""
    from apm_cli.deps.registry import config_loader

    home = tmp_path / "home"
    home.mkdir()
    _write_workspace_bomb(home)
    monkeypatch.setenv("HOME", str(home))
    # config.json layer must be inert so only the workspace YAML is exercised.
    monkeypatch.setattr(config_loader, "_load_config_json_registries", lambda: {}, raising=True)

    finished, result, exc = _run_guarded(
        lambda: config_loader.load_merged_registries(
            project_registries=None, policy_registries=None
        ),
        _BUDGET_SECONDS,
    )

    assert finished, (
        f"load_merged_registries hung >{_BUDGET_SECONDS}s reading a poisoned "
        "~/.apm/apm.yml -- every apm command that loads a project manifest "
        "(install/lifecycle/audit/pack via from_apm_yml) inherits this CPU DoS."
    )
    assert exc is None, f"expected fail-closed merge, got {exc!r}"
    assert isinstance(result, dict)
