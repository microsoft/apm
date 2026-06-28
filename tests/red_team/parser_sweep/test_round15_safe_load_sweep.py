"""Round-15 parser breaks r15-parser-1..4: bounded string/handle entry point.

Round-14 routed the five manifest-PATH readers through the bounded
``load_yaml``. But ``utils/yaml_io.py`` exposed ONLY a path entry point, so
every remaining ``yaml.safe_load(text | handle)`` sink still bypassed the
round-12 merge-entry budget and round-13 alias expansion guard. Four of those
sinks are reachable from an UNTRUSTED clone / bundle on a default path:

  * r15-parser-1 (HIGH): ``LockFile.from_yaml`` (deps/lockfile.py) -- the shared
    reader behind every ``apm.lock.yaml`` parse via ``LockFile.read``. A bundle
    lockfile from an untrusted ``apm pack`` artifact is parsed here; the merge
    bomb hung ``safe_load`` at parse time and the surrounding
    ``except (yaml.YAMLError, ValueError, KeyError)`` never fired on a
    non-terminating loop. This transitively covers ``bundle/local_bundle.py``.
  * r15-parser-2 (HIGH): ``bundle/unpacker.py::unpack_bundle`` parsed the
    untrusted bundle lockfile's ``pack:`` metadata with stock ``safe_load``.
  * r15-parser-3 (MED): the integration frontmatter parsers
    (``agent_integrator`` opencode/codex, ``instruction_integrator``
    windsurf/kiro) parsed an installed package's ``.md`` frontmatter with stock
    ``safe_load`` wrapped in an ``except`` that cannot catch a hang.
  * r15-parser-4 (MED): ``core/build_orchestrator.py`` (``detect_outputs`` /
    ``produce``) read the project / clone ``apm.yml`` via stock
    ``safe_load(handle)`` on the ``apm pack`` path -- the exact untrusted-clone
    apm.yml threat round-14 hardened in drift/init, missed for this pair.

The fix adds ``load_yaml_str(text)`` (same ``_BoundedSafeLoader``) to
``utils/yaml_io.py`` and routes the four genuine sinks through it (the
build_orchestrator pair uses the path-based ``load_yaml``). Each trap proves
the sink returns FAST (daemon-thread watchdog, no pytest-timeout dependency)
and that a LEGIT manifest still parses.

NOTE: the admin-owned marketplace registry / policy.d ``safe_load`` sinks are
NOT attacker-controlled from an untrusted clone on a default path (they are the
framework's trusted control plane) and are out of this round's threat surface.
"""

from __future__ import annotations

import threading
import time

import pytest
import yaml

from apm_cli.core.build_orchestrator import detect_outputs
from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.utils.diagnostics import DiagnosticCollector
from apm_cli.utils.yaml_io import load_yaml_str


def _merge_bomb(levels: int = 30) -> str:
    """A linear-size YAML whose merged value-list doubles each level."""
    lines = ["a: &a {k: v}"]
    for i in range(1, levels + 1):
        prev = "a" if i == 1 else f"m{i - 1}"
        lines.append(f"m{i}: &m{i}")
        lines.append(f"  <<: [*{prev}, *{prev}]")
    lines.append(f"target: *m{levels}")
    lines.append(f"marketplace: *m{levels}")
    lines.append(f"dependencies: [*m{levels}]")
    return "\n".join(lines) + "\n"


def _run_fast(fn, label: str, budget: float = 15.0):
    """Run *fn* on a daemon thread; fail if it does not return within *budget*."""
    result: dict[str, object] = {}

    def go():
        t0 = time.time()
        try:
            result["val"] = fn()
        except BaseException as exc:
            result["exc"] = exc
        result["dt"] = time.time() - t0

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(budget)
    assert not th.is_alive(), f"{label} HUNG >{budget}s on merge bomb (safe_load bypass)"
    return result


def test_bounded_loader_rejects_merge_bomb_fast():
    """The new ``load_yaml_str`` raises YAMLError on the bomb (no hang)."""
    with pytest.raises(yaml.YAMLError):
        load_yaml_str(_merge_bomb())


def test_bounded_loader_accepts_benign_dag():
    """A benign anchor DAG (no value doubling) must still parse correctly."""
    text = "base: &b {name: ok}\n" + "\n".join(f"n{i}: *b" for i in range(30)) + "\n"
    data = load_yaml_str(text)
    assert data["n0"]["name"] == "ok"
    assert data["base"]["name"] == "ok"


def test_lockfile_from_yaml_hangs_on_merge_bomb():
    """r15-parser-1: LockFile.from_yaml must fail closed (raise), not hang."""
    res = _run_fast(lambda: LockFile.from_yaml(_merge_bomb()), "LockFile.from_yaml")
    assert isinstance(res.get("exc"), yaml.YAMLError), res


def test_lockfile_read_fails_closed_on_bomb(tmp_path):
    """LockFile.read swallows the bomb's YAMLError -> None (transitive bundle fix)."""
    p = tmp_path / "apm.lock.yaml"
    p.write_text(_merge_bomb(), encoding="utf-8")
    res = _run_fast(lambda: LockFile.read(p), "LockFile.read")
    assert res.get("val") is None, res


def test_lockfile_from_yaml_legit_still_parses():
    """A normal lockfile string still deserializes."""
    lock = LockFile.from_yaml('lockfile_version: "1"\ndependencies: []\n')
    assert isinstance(lock, LockFile)


def test_bundle_unpacker_hangs_on_bomb_lockfile(tmp_path):
    """r15-parser-2: unpack_bundle's pack-meta read must fail closed (not hang)."""
    from apm_cli.bundle.unpacker import unpack_bundle

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "apm.lock.yaml").write_text(_merge_bomb(), encoding="utf-8")
    out = tmp_path / "out"

    def call():
        try:
            return unpack_bundle(bundle, out, skip_verify=True, dry_run=True, force=True)
        except BaseException as exc:
            return exc

    res = _run_fast(call, "unpack_bundle")
    assert "val" in res, res


def test_agent_frontmatter_parser_hangs_on_bomb(tmp_path):
    """r15-parser-3: agent .agent.md frontmatter parse must not hang."""
    md = tmp_path / "evil.agent.md"
    md.write_text(f"---\n{_merge_bomb()}\n---\nbody\n", encoding="utf-8")
    res = _run_fast(
        lambda: AgentIntegrator._warn_opencode_frontmatter(md, DiagnosticCollector(), "evil-pkg"),
        "agent _warn_opencode_frontmatter",
    )
    assert "val" in res, res


def test_instruction_frontmatter_parsers_hang_on_bomb():
    """r15-parser-3: windsurf + kiro instruction frontmatter parse must not hang."""
    content = f"---\n{_merge_bomb()}\n---\nbody\n"
    res_w = _run_fast(
        lambda: InstructionIntegrator._convert_to_windsurf_rules(content),
        "instruction windsurf frontmatter",
    )
    assert "val" in res_w, res_w
    res_k = _run_fast(
        lambda: InstructionIntegrator._convert_to_kiro_steering(content),
        "instruction kiro frontmatter",
    )
    assert "val" in res_k, res_k


def test_instruction_frontmatter_legit_still_parses():
    """A normal applyTo frontmatter still drives the windsurf glob trigger."""
    out = InstructionIntegrator._convert_to_windsurf_rules('---\napplyTo: "**/*.py"\n---\nbody\n')
    assert "trigger: glob" in out


def test_build_orchestrator_detect_outputs_hangs(tmp_path):
    """r15-parser-4: detect_outputs must fail closed (BuildError), not hang."""
    p = tmp_path / "apm.yml"
    p.write_text(_merge_bomb(), encoding="utf-8")
    res = _run_fast(lambda: detect_outputs(p), "detect_outputs")
    from apm_cli.core.build_orchestrator import BuildError

    assert isinstance(res.get("exc"), BuildError), res


def test_build_orchestrator_detect_outputs_legit_still_parses(tmp_path):
    """A normal apm.yml drives the bundle producer."""
    p = tmp_path / "apm.yml"
    p.write_text("dependencies:\n  - some/dep\n", encoding="utf-8")
    out = detect_outputs(p)
    assert out  # non-empty producer set
