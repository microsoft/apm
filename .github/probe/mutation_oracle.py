"""S93 input-mutation oracle for the no-op fast paths (E2 red team, research harness).

For each fixture: converge an installed world, trace every file and directory a
full (kill-switched) no-op install reads, then for each one mutate it and run the
fast-path install. A replay/attested HIT on a mutated input is a finding when a
full run of the same mutated state prints different output or writes anything.
"""
import json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
from tests.benchmarks.journeys import Context, open_world, prepare_fixture  # noqa: E402

FIXTURES = os.environ.get("ORACLE_FIXTURES", "tree-10-mcp").split(",")
LIMIT = int(os.environ.get("ORACLE_LIMIT", "320"))
NEW_APM = os.environ["NEW_APM"]
PY = str(Path(NEW_APM).parent / "python")
NO_HIT = ("CI", "GITHUB_ACTIONS", "TF_BUILD", "JENKINS_URL", "BUILD_ID", "TEAMCITY_VERSION",
          "CODEBUILD_BUILD_ID", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "BITBUCKET_BUILD_NUMBER",
          "APPVEYOR", "DRONE", "RUNNER_TEMP")
ELAPSED = re.compile(r"\d+\.\d+s")
TRACER = r'''
import json, os, sys
reads = set()
def hook(event, args):
    if event == "open":
        path, mode = args[0], args[1]
        if isinstance(path, (str, bytes)) and (mode is None or "r" in str(mode) or str(mode).isdigit()):
            reads.add(("file", os.fsdecode(path)))
    elif event in ("os.listdir", "os.scandir") and args and args[0] is not None:
        reads.add(("dir", os.fsdecode(args[0]) if not isinstance(args[0], int) else str(args[0])))
sys.addaudithook(hook)
_stat, _lstat = os.stat, os.lstat
def _probe(real):
    def wrapped(path, *a, **k):
        try:
            return real(path, *a, **k)
        except FileNotFoundError:
            if isinstance(path, (str, bytes, os.PathLike)):
                reads.add(("absent", os.fsdecode(path)))
            raise
    return wrapped
os.stat, os.lstat = _probe(_stat), _probe(_lstat)
sys.argv = ["apm", "install"]
from apm_cli.entrypoint import main
try:
    main()
finally:
    with open(os.environ["ORACLE_READS"], "w") as out:
        json.dump(sorted(reads), out)
'''


def env(world, **extra):
    e = {k: v for k, v in world.env.items() if k not in NO_HIT}
    e.update(extra)
    return e


def run(world, cwd, **extra):
    r = subprocess.run([NEW_APM, "install"], cwd=cwd, env=env(world, **extra), capture_output=True,
                       text=True, timeout=600)
    return r.returncode, ELAPSED.sub("N.Ns", r.stdout), r.stderr


def state(roots):
    out = {}
    for base in roots:
        for here, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ("verdicts",)]
            for name in files:
                p = Path(here) / name
                try:
                    out[str(p)] = p.read_bytes() if not p.is_symlink() else b"L" + os.readlink(p).encode()
                except OSError:
                    out[str(p)] = b"?"
    return out


def converge(world, cwd):
    for _ in range(4):
        code, _o, err = run(world, cwd, APM_INSTALL_FAST_PATH="verdict")
        if "verdict record: hit" in err:
            return True
    return False


def mutate(path: str, kind: str):
    p = Path(path)
    if kind == "delete":
        if p.is_file() and not p.is_symlink():
            data = p.read_bytes()
            p.unlink()
            return lambda: p.write_bytes(data)
        return None
    if kind == "link-swap":
        if p.is_dir() and not p.is_symlink():
            outside = Path(tempfile.mkdtemp()) / p.name
            shutil.copytree(p, outside, symlinks=True)
            shutil.rmtree(p)
            p.symlink_to(outside, target_is_directory=True)

            def undo():
                p.unlink()
                shutil.copytree(outside, p, symlinks=True)
            return undo
        return None
    if kind == "dir":
        if p.is_dir():
            probe = p / "zz-oracle-probe.md"
            probe.write_text("oracle\n")
            return lambda: probe.unlink(missing_ok=True)
        return None
    if kind == "absent-dir":
        if not p.exists() and p.parent.is_dir():
            p.mkdir()
            (p / "oracle.md").write_text("oracle\n")
            return lambda: shutil.rmtree(p, ignore_errors=True)
        return None
    if kind == "absent":
        if not p.exists() and p.parent.is_dir():
            p.write_text("oracle\n")
            return lambda: p.unlink(missing_ok=True)
        return None
    if p.is_file() and not p.is_symlink():
        data = p.read_bytes()
        p.write_bytes(data + (b"\n# oracle\n" if not data.endswith(b"\0") else b"\0"))
        return lambda: p.write_bytes(data)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True) if p.parent.exists() else None
        if p.parent.is_dir():
            p.write_text("oracle\n")
            return lambda: p.unlink(missing_ok=True)
    return None


findings = []
KNOWN = {
    "#5438 governed root -> link": lambda f: f[1] == "link-swap" and f[2].endswith("/.github"),
    "#5434 root carrier AGENTS.md/CLAUDE.md": lambda f: f[2].endswith(("/AGENTS.md", "/CLAUDE.md"))
    and "/project/" in f[2] and f[2].count("/", f[2].index("/project/")) == 2,
}


def seed(world, root):
    """Claude active plus a compiled root AGENTS.md carrier imported by CLAUDE.md (#5434 world)."""
    (Path(root) / ".claude").mkdir(exist_ok=True)
    r = subprocess.run([NEW_APM, "compile"], cwd=root, env=env(world), capture_output=True, text=True, timeout=600)
    agents = Path(root) / "AGENTS.md"
    if agents.exists() and not (Path(root) / "CLAUDE.md").exists():
        (Path(root) / "CLAUDE.md").write_text("@AGENTS.md\n")
    print(f"ORACLE seed: compile rc={r.returncode} AGENTS.md={agents.exists()} "
          f"CLAUDE.md={(Path(root) / 'CLAUDE.md').exists()} .github={(Path(root) / '.github').is_dir()}", flush=True)
for name in FIXTURES:
    work = Path(tempfile.mkdtemp())
    fixture = prepare_fixture(name, work)
    with open_world(fixture, work / "world" / name) as world:
        ctx = Context(fixture=fixture, world=world, apm=[NEW_APM], work=work / "w")
        ctx.fresh_project()
        root = ctx.roots()[0]
        run(world, root)
        if os.environ.get("ORACLE_SEED", "1") == "1":
            seed(world, root)
            run(world, root)
        if not converge(world, root):
            _c, _o, err = run(world, root, APM_INSTALL_FAST_PATH="verdict")
            print(f"ORACLE {name}: baseline never hits: {err.strip()[-400:]}", flush=True)
            continue
        reads_file = work / "reads.json"
        subprocess.run([PY, "-c", TRACER], cwd=root, timeout=600, capture_output=True, text=True,
                       env=env(world, APM_INSTALL_FAST_PATH="0", ORACLE_READS=str(reads_file)))
        reads = json.loads(reads_file.read_text()) if reads_file.exists() else []
        roots = [str(root), str(world.apm_home), str(Path(world.env.get("HOME", root)))]
        cands = [(k, p) for k, p in reads if any(p.startswith(r) for r in roots)
                 and "/verdicts/" not in p and not p.endswith((".pyc", ".py")) and "/.venv/" not in p]
        proj = [(k, p) for k, p in cands if p.startswith(str(root)) and "/apm_modules/" not in p]
        extra = [("delete", p) for k, p in proj if k == "file"]
        extra += [("link-swap", p) for k, p in proj if k == "dir" and p != str(root)]
        extra += [(k, str(Path(root) / n)) for n in ("AGENTS.md", "CLAUDE.md") for k in ("file", "delete")]
        extra += [("link-swap", str(Path(root) / ".github"))]
        extra += [("absent-dir", p) for k, p in cands if k == "absent" and "." not in os.path.basename(p)]
        rank = lambda c: (0 if c[1].startswith(str(root)) else 1 if c[1].startswith(str(world.apm_home)) is False else 2, c)
        cands = sorted(set(cands) | set(extra), key=rank)[:LIMIT]
        counts = {"hit": 0, "miss": 0}
        skips: dict[str, int] = {}
        print(f"ORACLE {name}: {len(reads)} reads, {len(cands)} candidates", flush=True)
        watched = [str(root), str(world.apm_home), str(Path(world.env.get("HOME", root)))]
        for kind, path in cands:
            undo = mutate(path, kind)
            if undo is None:
                skips[kind] = skips.get(kind, 0) + 1
                continue
            try:
                code, out, err = run(world, root, APM_INSTALL_FAST_PATH="verdict")
                hit = "verdict record: hit" in err or "attested hit" in err
                counts["hit" if hit else "miss"] += 1
                if os.environ.get("ORACLE_VERBOSE"):
                    print(f"ORACLE {'HIT ' if hit else 'miss'} {kind} {path}", flush=True)
                if hit:
                    before = state(watched)
                    fcode, fout, _ferr = run(world, root, APM_INSTALL_FAST_PATH="0")
                    after = state(watched)
                    wrote = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
                    # The engine declines a linked deployed path by contract (diff_apply._within).
                    if kind == "link-swap" or fout != out or fcode != code or wrote:
                        findings.append((name, kind, path))
                        print(f"ORACLE FINDING {name}: {kind} {path} | hit while full run "
                              f"{'prints different output' if fout != out else ''}"
                              f"{' writes ' + ', '.join(wrote[:3]) if wrote else ''}", flush=True)
                    else:
                        print(f"ORACLE same {name}: {kind} {path}", flush=True)
            finally:
                undo()
                if not converge(world, root):
                    print(f"ORACLE {name}: could not reconverge after {path}", flush=True)
                    break
    print(f"ORACLE {name} counts: {counts} skipped-by-kind: {skips}", flush=True)
    for label, known in KNOWN.items():
        print(f"ORACLE positive {label}: {'FLAGGED' if any(known(f) for f in findings) else 'MISSED'}", flush=True)
print(f"ORACLE RESULT: {len(findings)} finding(s)")
for f in findings:
    print("ORACLE ROW", *f)
