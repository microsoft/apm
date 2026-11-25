"""Acceptance differential: deployed bytes and lockfile of base vs candidate CLI, per fixture."""
import hashlib, os, stat, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
from tests.benchmarks.journeys import Context, open_world, prepare_fixture  # noqa: E402

FIXTURES = os.environ.get("DIFF_FIXTURES", "tree-50,monorepo-4,tree-10-mcp,tree-10-multi,tree-50-policy").split(",")
SKIP = {"apm_modules", ".git"}
SKIP_FILES = {".apm-local/installation"}  # per-install id, random by design
import difflib
TEXT = {}
VOLATILE = ("generated_at", "generated-at", "timestamp", "installed_at")


def snapshot(project: Path) -> dict[str, str]:
    out = {}
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for name in files:
            path = Path(root) / name
            rel = path.relative_to(project).as_posix()
            if rel in SKIP_FILES or rel.endswith("/.apm-local/installation"):
                continue
            data = path.read_bytes() if not path.is_symlink() else b"LINK:" + os.readlink(path).encode()
            # Each side installs into its own project dir; #5418 embeds that absolute
            # path in the lockfile, so fold it to a token before comparing.
            for form in {str(project), project.as_posix()}:
                data = data.replace(form.encode(), b"<PROJECT>")
            if name.endswith(".lock.yaml") or name == "apm.lock.yaml":
                data = b"\n".join(l for l in data.splitlines() if not any(v.encode() in l for v in VOLATILE))
            mode = stat.S_IMODE(os.lstat(path).st_mode)
            # Path + mode + bytes; on Windows only the read-only bit is meaningful.
            tag = (b"ro" if not mode & stat.S_IWRITE else b"rw") if os.name == "nt" else oct(mode).encode()
            out[rel] = hashlib.sha256(tag + b"\0" + data).hexdigest()
            if len(data) < 400_000:
                TEXT[(str(project), rel)] = data.decode("utf-8", "replace")
    return out


failures = 0
SUMMARY = []
work = Path(tempfile.mkdtemp()).resolve()  # W: long form, as in the bench world
def _one(name):
    global failures
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
            shots[side + "_root"] = str(ctx.project)
    base, new = shots["base"], shots["new"]
    broot, nroot = shots["base_root"], shots["new_root"]
    diffs = sorted(k for k in base.keys() | new.keys() if base.get(k) != new.get(k))
    line = f"DIFF {name}: files base={len(base)} new={len(new)} differing={len(diffs)}"
    SUMMARY.append(line + ("" if not diffs else " first=" + ",".join(diffs[:3])))
    print(line, flush=True)
    for rel in diffs[:40]:
        print(f"   {rel}: base={base.get(rel, '-')[:12]} new={new.get(rel, '-')[:12]}")
    for rel in diffs[:3]:
        a = TEXT.get((broot, rel), "").splitlines()
        b = TEXT.get((nroot, rel), "").splitlines()
        for line in list(difflib.unified_diff(a, b, "base/" + rel, "new/" + rel, n=2, lineterm=""))[:60]:
            print("      " + line[:220])
    failures += bool(diffs)


for name in FIXTURES:
    try:
        _one(name)
    except BaseException as exc:  # a failed install is unequal output, not a pass
        failures += 1
        SUMMARY.append(f"DIFF {name}: ERROR {type(exc).__name__}: {str(exc)[-200:]}")
        print(SUMMARY[-1], flush=True)
for line in SUMMARY:
    print("SUMMARY " + line.replace("\n", " "), flush=True)
print("DIFF RESULT:", "IDENTICAL" if not failures else f"{failures} fixture(s) differ")
sys.exit(1 if failures else 0)
