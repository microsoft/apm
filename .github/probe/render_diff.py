"""Acceptance differential: deployed bytes and lockfile of base vs candidate CLI, per fixture."""
import hashlib, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
from tests.benchmarks.journeys import Context, open_world, prepare_fixture  # noqa: E402

FIXTURES = os.environ.get("DIFF_FIXTURES", "tree-50,monorepo-4,tree-10-mcp,tree-10-multi").split(",")
SKIP = {"apm_modules", ".git"}
VOLATILE = ("generated_at", "generated-at", "timestamp", "installed_at")


def snapshot(project: Path) -> dict[str, str]:
    out = {}
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for name in files:
            path = Path(root) / name
            rel = path.relative_to(project).as_posix()
            data = path.read_bytes() if not path.is_symlink() else b"LINK:" + os.readlink(path).encode()
            if name.endswith(".lock.yaml") or name == "apm.lock.yaml":
                data = b"\n".join(l for l in data.splitlines() if not any(v.encode() in l for v in VOLATILE))
            out[rel] = hashlib.sha256(data).hexdigest()
    return out


failures = 0
work = Path(tempfile.mkdtemp())
for name in FIXTURES:
    fixture = prepare_fixture(name, work)
    with open_world(fixture, work / "world" / name) as world:
        shots = {}
        for side, apm in (("base", os.environ["BASE_APM"]), ("new", os.environ["NEW_APM"])):
            ctx = Context(fixture=fixture, world=world, apm=[apm], work=work / side / name)
            world.clear_cache()
            ctx.fresh_project()
            for root in ctx.roots():
                ctx.checked(["install"], root)
            shots[side] = snapshot(ctx.project)
    base, new = shots["base"], shots["new"]
    diffs = sorted(k for k in base.keys() | new.keys() if base.get(k) != new.get(k))
    print(f"DIFF {name}: files base={len(base)} new={len(new)} differing={len(diffs)}", flush=True)
    for rel in diffs[:40]:
        print(f"   {rel}: base={base.get(rel, '-')[:12]} new={new.get(rel, '-')[:12]}")
    failures += bool(diffs)
print("DIFF RESULT:", "IDENTICAL" if not failures else f"{failures} fixture(s) differ")
sys.exit(1 if failures else 0)
