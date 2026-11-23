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


# Invariant buckets, first match wins (functions anywhere in the stack, then modules).
_INVARIANTS = (
    ("imports", {"_find_and_load"}, ()),
    ("policy", set(), ("policy/",)),
    (
        "checkout-verify",
        {"verify_checkout", "require_verified_copy", "require_verified_checkout",
         "read_verified_file", "verify_checkout_sha"},
        ("cache/checkout_verify",),
    ),  # fmt: skip
    (
        "content-hash",
        {"compute_package_hash", "compute_package_digests", "verify_package_hash",
         "compute_native_bundle_hash", "locked_content_mismatch"},
        ("install/phases/content_hash_stream",),
    ),  # fmt: skip
    (
        "fetch-clone",
        {"_ensure_bare_repo", "_create_checkout", "_clone_with_fallback", "clone_from",
         "_fetch_into_bare", "_materialize_checkout"},
        (),
    ),  # fmt: skip
    (
        "ref-resolve",
        {"resolve_git_reference", "_resilient_get", "ls_remote", "_ls_remote"},
        ("deps/tiered_ref_resolver", "deps/git_reference_resolver"),
    ),
    ("copy-to-modules", {"robust_copytree", "robust_copy_entry"}, ()),
    ("receipts-ledger", set(), ("core/deployment_ledger", "cache/deploy_store",
                                "install/deploy_snapshot", "install/receipts")),  # fmt: skip
    ("path-containment", set(), ("utils/path_security", "utils/paths")),
    ("cache-lookup", set(), ("cache/",)),
    ("manifest-validate", {"validate_apm_package", "from_apm_yml", "load_manifest"}, ()),
    ("lockfile", set(), ("deps/lockfile", "install/phases/lockfile",
                         "deps/lockfile_generator", "install/cache_pin")),  # fmt: skip
    (
        "discovery",
        {"find_files_by_glob", "find_primitive_files", "discover_primitives"},
        ("primitives/", "compilation/inventory"),
    ),
    ("deploy-writes", set(), ("integration/", "install/template", "install/services",
                              "install/deployment_plan", "install/commit_gate")),  # fmt: skip
    ("resolver-graph", set(), ("deps/",)),
)


def _leaf_owner(frames: list[tuple[str, str]], default: str) -> str:
    for func, path in reversed(frames):
        mod = _apm(path) or ""
        for bucket, wanted_funcs, wanted_mods in _INVARIANTS:
            if func in wanted_funcs or (mod and wanted_mods and mod.startswith(wanted_mods)):
                return bucket
    return default


def invariants(text: str, rate: int) -> list[str]:
    """Samples per invariant; one stack counts once, in the first bucket it matches."""
    buckets: Counter = Counter()
    total = 0
    for line in text.splitlines():
        stack, _, count = line.rpartition(" ")
        if not stack or not count.isdigit():
            continue
        n = int(count)
        total += n
        frames = _frames(stack)
        funcs = {func for func, _path in frames}
        mods = [_apm(path) or "" for _func, path in frames]
        name = "other-apm" if any(mods) else "interpreter"
        if "_find_and_load" in funcs:
            name = "imports"
        else:
            # Leaf first: the innermost frame that some invariant owns decides.
            name = _leaf_owner(frames, name)
        buckets[name] += n
    out = [f"INV TOTAL samples={total} ~{total / rate:.2f}s"]
    for name, n in buckets.most_common():
        out.append(f"INV {name} {n / rate:.2f}s {100 * n / max(total, 1):.1f}%")
    return out
