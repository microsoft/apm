"""RESEARCH: bucket py-spy raw (collapsed) stacks by install phase and by leaf category."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

_FRAME = re.compile(r"^(.*) \((.*?):(\d+)\)$")
_MARK = "/apm_cli/"
_ENTRY = ("entrypoint.py", "cli.py", "commands/install.py", "install/fast_entry.py")
_CATEGORIES = (
    ("utils/content_hash", "hashing"),
    ("install/phases/content_hash_stream", "hashing"),
    ("cache/checkout_verify", "verify"),
    ("cache/deploy_store", "receipts"),
    ("core/deployment_ledger", "receipts"),
    ("cache/", "cache"),
    ("policy/", "policy"),
    ("utils/console", "rendering"),
    ("install/tui", "rendering"),
    ("utils/yaml_io", "yaml"),
    ("deps/lockfile", "lockfile"),
    ("install/phases/lockfile", "lockfile"),
    ("utils/path_security", "path"),
    ("utils/paths", "path"),
    ("utils/file_ops", "file-copy"),
    ("integration/", "integrate"),
    ("deps/", "deps"),
    ("models/", "models"),
    ("primitives/", "primitives"),
    ("compilation/", "compilation"),
)


def _frames(stack: str) -> list[tuple[str, str]]:
    out = []
    for token in stack.split(";"):
        match = _FRAME.match(token)
        if match:
            out.append((match.group(1), match.group(2).replace("\\", "/")))
        else:
            out.append((token, ""))
    return out


def _apm(path: str) -> str | None:
    if path.startswith("apm_cli/"):
        return path[len("apm_cli/") :]
    return path.rsplit(_MARK, 1)[1] if _MARK in path else None


def _phase(frames: list[tuple[str, str]]) -> str:
    in_pipeline = None
    for func, path in frames:
        mod = _apm(path)
        if mod is None:
            continue
        if mod.startswith("install/phases/"):
            return "phase:" + mod[len("install/phases/") : -3]
        if mod == "install/pipeline.py":
            if func == "_run_integration_phase":
                return "phase:integrate"
            in_pipeline = func
    if in_pipeline:
        return "pipeline:" + in_pipeline
    mods = [(_apm(p), f) for f, p in frames if _apm(p)]
    rest = [(m, f) for m, f in mods if m not in _ENTRY]
    if any(f == "_find_and_load" for f, _ in frames) and not rest:
        return "imports"
    if rest:
        return f"pre:{rest[0][0]}:{rest[0][1]}"
    if mods:
        return f"entry:{mods[-1][0]}:{mods[-1][1]}"
    return "non-apm"


def _category(frames: list[tuple[str, str]]) -> str:
    if any(f == "_find_and_load" for f, _ in frames):
        return "imports"
    for _func, path in reversed(frames):
        mod = _apm(path)
        if mod is None:
            continue
        for prefix, name in _CATEGORIES:
            if mod.startswith(prefix):
                return name
        return "other:" + mod
    return "non-apm"


def _child(frames: list[tuple[str, str]], phase: str) -> str:
    """The first apm_cli frame below the phase frame (what the phase spends on)."""
    seen = False
    for func, path in frames:
        mod = _apm(path)
        if mod is None:
            continue
        if not seen:
            if (phase == "phase:integrate" and func == "_run_integration_phase") or (
                mod.startswith("install/phases/") and ("phase:" + mod[15:-3]) == phase
            ):
                seen = True
            continue
        if not mod.startswith("install/phases/" + phase[6:]):
            return f"{mod}:{func}"
    return "(self)"


def summarize(text: str, rate: int) -> list[str]:
    phases: Counter = Counter()
    cats: Counter = Counter()
    cross: defaultdict = defaultdict(Counter)
    children: defaultdict = defaultdict(Counter)
    total = 0
    for line in text.splitlines():
        stack, _, count = line.rpartition(" ")
        if not stack or not count.isdigit():
            continue
        n = int(count)
        frames = _frames(stack)
        phase = _phase(frames)
        cat = _category(frames)
        total += n
        phases[phase] += n
        cats[cat] += n
        cross[phase][cat] += n
        if phase.startswith("phase:"):
            children[phase][_child(frames, phase)] += n
    sec = lambda n: f"{n / rate:.2f}s"  # noqa: E731
    out = [f"TOTAL samples={total} ~{sec(total)}"]
    for phase, n in phases.most_common(25):
        top = ", ".join(f"{c} {sec(k)}" for c, k in cross[phase].most_common(4))
        out.append(f"PHASE {phase} {sec(n)} {100 * n / max(total, 1):.1f}% | {top}")
    for cat, n in cats.most_common(20):
        out.append(f"CAT {cat} {sec(n)} {100 * n / max(total, 1):.1f}%")
    for phase, _n in phases.most_common(6):
        if phase in children:
            top = ", ".join(f"{c} {sec(k)}" for c, k in children[phase].most_common(8))
            out.append(f"CHILD {phase}: {top}")
    return out
