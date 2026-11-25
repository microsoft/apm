"""S93 input-mutation oracle for the no-op fast paths (research harness, v5).

For one fixture and one mode: converge an installed world, trace every file,
directory, absent probe and environment variable a full (kill-switched) install
of the mode's argv reads, then mutate each one and run the fast-path install.
A fast HIT on a mutated input is a finding when a full run of the same mutated
state exits differently, prints different output or writes anything.

Modes (ORACLE_MODE):
  replay          converged output record, argv [install] (the no-op replay)
  attested        fresh attested record, argv [install] (the attested hit)
  attested-frozen fresh plain attested record, argv [install, --frozen] (#4770)

A fixture name may end in "+seed": compile once before converging, so a root
AGENTS.md carrier (and a CLAUDE.md importing it) is part of the world (#5434).
"""
import json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, os.getcwd())
from tests.benchmarks.journeys import Context, open_world, prepare_fixture  # noqa: E402

FIXTURE = os.environ.get("ORACLE_FIXTURE", "tree-10-claude+seed")
MODE = os.environ.get("ORACLE_MODE", "replay")
LIMIT = int(os.environ.get("ORACLE_LIMIT", "320"))
ENV_LIMIT = int(os.environ.get("ORACLE_ENV_LIMIT", "40"))
NEW_APM = os.environ["NEW_APM"]
PY = str(Path(NEW_APM).parent / "python")
ARGV = ["install", "--frozen"] if MODE == "attested-frozen" else ["install"]
NO_HIT = ("CI", "GITHUB_ACTIONS", "TF_BUILD", "JENKINS_URL", "BUILD_ID", "TEAMCITY_VERSION",
          "CODEBUILD_BUILD_ID", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "BITBUCKET_BUILD_NUMBER",
          "APPVEYOR", "DRONE", "RUNNER_TEMP", "SSL_CERT_DIR")
# Never mutated: process plumbing, the oracle's own switches, and the no-hit set.
ENV_SKIP = set(NO_HIT) | {"PATH", "HOME", "USERPROFILE", "APM_INSTALL_FAST_PATH", "ORACLE_READS",
                          "PYTHONPATH", "VIRTUAL_ENV", "TMPDIR", "TEMP", "TMP", "PWD", "SHELL"}
ELAPSED = re.compile(r"\d+\.\d+s")
SERVED = ("verdict record: hit", "verdict record: attested hit")
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
_getitem = os._Environ.__getitem__
def _env_read(self, key):
    reads.add(("env", str(key)))
    return _getitem(self, key)
os._Environ.__getitem__ = _env_read
sys.argv = ["apm", *json.loads(os.environ["ORACLE_ARGV"])]
from apm_cli.entrypoint import main
try:
    main()
finally:
    with open(os.environ["ORACLE_READS"], "w") as out:
        json.dump(sorted(reads), out)
'''


def env(world, **extra):
    e = {k: v for k, v in world.env.items() if k not in NO_HIT}
    for k, v in extra.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return e


def run(world, cwd, argv=None, **extra):
    r = subprocess.run([NEW_APM, *(argv or ARGV)], cwd=cwd, env=env(world, **extra),
                       capture_output=True, text=True, timeout=600)
    return r.returncode, ELAPSED.sub("N.Ns", r.stdout), r.stderr


def served(err: str) -> bool:
    return any(s in err for s in SERVED)


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


def records(world) -> list[Path]:
    base = Path(world.apm_home)
    return [p for p in base.rglob("*.json") if "verdict" in str(p.relative_to(base))]


def drop_records(world) -> None:
    for p in records(world):
        p.unlink(missing_ok=True)


def record_kind(world) -> str:
    kinds = []
    for p in records(world):
        try:
            kinds.append(str(json.loads(p.read_text()).get("kind")))
        except (OSError, ValueError):
            kinds.append("?")
    return ",".join(kinds) or "none"


def converge(world, cwd) -> bool:
    """The mode's baseline: the unmutated world is served by the fast path."""
    if MODE == "replay":
        for _ in range(4):
            if "verdict record: hit" in run(world, cwd, APM_INSTALL_FAST_PATH="verdict")[2]:
                return True
        return False
    # Only a committing install attests (a proven no-op records output), so drop the
    # record and one deployed file: the plain install restores it and attests.
    victim = _victim(cwd)
    for _ in range(2):
        drop_records(world)
        if victim is not None:
            victim.unlink(missing_ok=True)
        run(world, cwd, ["install"], APM_INSTALL_FAST_PATH="verdict")
        if record_kind(world) == "attested":
            return True
    return False


def _victim(cwd) -> Path | None:
    import yaml

    try:
        lock = yaml.safe_load((Path(cwd) / "apm.lock.yaml").read_text())
    except (OSError, ValueError):
        return None
    files = sorted(f for d in lock.get("dependencies") or [] for f in d.get("deployed_files") or [])
    for rel in files:
        path = Path(cwd) / rel
        if path.is_file() and not path.is_symlink():
            return path
    return None


def baseline_served(world, cwd) -> tuple[bool, str]:
    if MODE == "replay":
        return True, ""
    code, _o, err = run(world, cwd, APM_INSTALL_FAST_PATH="verdict")
    ok = served(err)
    converge(world, cwd)  # an attested hit on [install] may record output; start afresh
    return ok, err


def mutate(path: str, kind: str):
    p = Path(path)
    if kind == "env":
        return None  # applied per run, see probe()
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
    if kind == "rewrite":
        # #5451 control: a carrier content edit that flips Claude routing.
        if p.is_file() and not p.is_symlink():
            data = p.read_bytes()
            if p.name == "AGENTS.md":
                p.write_text("# Hand-authored project notes\n")
            else:
                p.write_text("# Notes without the import\n")
            return lambda: p.write_bytes(data)
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
    if not p.exists() and p.parent.is_dir():
        p.write_text("oracle\n")
        return lambda: p.unlink(missing_ok=True)
    return None


def env_value(world, name: str) -> str:
    current = world.env.get(name)
    return "oracle" if current is None else current + "-oracle"


KNOWN = {
    "#5451 carrier content edit": lambda f: f[1] == "rewrite",
    "#5438 governed root -> link": lambda f: f[1] == "link-swap"
    and f[2].endswith(("/.github", "/.claude")),
    "#5434 root carrier AGENTS.md/CLAUDE.md": lambda f: f[2].endswith(("/AGENTS.md", "/CLAUDE.md"))
    and "/project/" in f[2] and f[2].count("/", f[2].index("/project/")) == 2,
}


def seed(world, root):
    """Claude active plus a compiled root AGENTS.md carrier imported by CLAUDE.md (#5434 world)."""
    (Path(root) / ".claude").mkdir(exist_ok=True)
    r = subprocess.run([NEW_APM, "compile"], cwd=root, env=env(world), capture_output=True,
                       text=True, timeout=600)
    agents = Path(root) / "AGENTS.md"
    if agents.exists() and not (Path(root) / "CLAUDE.md").exists():
        (Path(root) / "CLAUDE.md").write_text("@AGENTS.md\n")
    print("ORACLE seed out: " + " | ".join((r.stdout + r.stderr).strip().splitlines()[-6:]), flush=True)
    print(f"ORACLE seed: compile rc={r.returncode} AGENTS.md={agents.exists()} "
          f"CLAUDE.md={(Path(root) / 'CLAUDE.md').exists()} rules="
          f"{len(list((Path(root) / '.claude').rglob('*.md')))}", flush=True)


def main() -> int:
    findings = []
    name, _, flavour = FIXTURE.partition("+")
    label = f"{FIXTURE}/{MODE}"
    started = time.time()
    work = Path(tempfile.mkdtemp())
    fixture = prepare_fixture(name, work)
    with open_world(fixture, work / "world" / name) as world:
        ctx = Context(fixture=fixture, world=world, apm=[NEW_APM], work=work / "w")
        ctx.fresh_project()
        root = ctx.roots()[0]
        code, out, err = run(world, root, ["install"])
        print(f"ORACLE {label}: first install rc={code} {err.strip()[-300:]}", flush=True)
        if flavour == "seed":
            seed(world, root)
            run(world, root, ["install"])
        if flavour == "carriers":
            # A hand-authored root AGENTS.md and a CLAUDE.md importing it (#5451 world).
            (Path(root) / "AGENTS.md").write_text(
                "<!-- Generated by APM CLI from .apm/ primitives -->\n# Compiled\n")
            (Path(root) / "CLAUDE.md").write_text("@AGENTS.md\n")
            run(world, root, ["install"])
        if not converge(world, root):
            _c, _o, err = run(world, root, ["install"], APM_INSTALL_FAST_PATH="verdict")
            print(f"ORACLE {label}: baseline never converges (record={record_kind(world)}): "
                  f"{err.strip()[-600:]}", flush=True)
            print("ORACLE RESULT: 0 finding(s) [NO BASELINE]")
            return 0
        ok, err = baseline_served(world, root)
        print(f"ORACLE {label}: baseline served={ok} record={record_kind(world)}", flush=True)
        if not ok:
            print(f"ORACLE {label}: baseline not served: {err.strip()[-600:]}", flush=True)
            print("ORACLE RESULT: 0 finding(s) [NO BASELINE]")
            return 0
        reads_file = work / "reads.json"
        subprocess.run([PY, "-c", TRACER], cwd=root, timeout=600, capture_output=True, text=True,
                       env=env(world, APM_INSTALL_FAST_PATH="0", ORACLE_READS=str(reads_file),
                               ORACLE_ARGV=json.dumps(ARGV)))
        reads = json.loads(reads_file.read_text()) if reads_file.exists() else []
        if MODE != "replay":
            converge(world, root)  # the traced full run may have rewritten the record
        roots = [str(root), str(world.apm_home), str(Path(world.env.get("HOME", root)))]
        env_rank = lambda n: (0 if n.startswith("APM_") else 1 if n.startswith(("GIT", "GH_")) else 2, n)
        envs = sorted({p for k, p in reads if k == "env" and p not in ENV_SKIP}, key=env_rank)[:ENV_LIMIT]
        cands = [(k, p) for k, p in reads if k != "env" and any(p.startswith(r) for r in roots)
                 and "/verdicts/" not in p and not p.endswith((".pyc", ".py")) and "/.venv/" not in p]
        proj = [(k, p) for k, p in cands if p.startswith(str(root)) and "/apm_modules/" not in p]
        extra = [(k, str(Path(root) / n)) for n in ("AGENTS.md", "CLAUDE.md") for k in ("rewrite", "file", "delete")]
        extra += [("link-swap", str(Path(root) / ".github")), ("link-swap", str(Path(root) / ".claude"))]
        extra += [("delete", p) for k, p in proj if k == "file"]
        extra += [("link-swap", p) for k, p in proj if k == "dir" and p != str(root)]
        extra += [("absent-dir", p) for k, p in cands if k == "absent" and "." not in os.path.basename(p)]
        rank = lambda c: (0 if c[1].startswith(str(root)) else 1 if c[1].startswith(str(world.apm_home)) is False else 2, c)
        live = [c for c in set(cands) if c[0] != "absent" or Path(c[1]).parent.is_dir()]
        first = list(dict.fromkeys(extra[:8] + [c for c in extra if c[0] == "link-swap"]))
        cands = first + [c for c in sorted(set(live) | set(extra), key=rank) if c not in first]
        cands = cands[:LIMIT] + [("env", n) for n in envs]
        print(f"ORACLE {label}: {len(reads)} reads, {len(live)} live, {len(envs)} env, "
              f"{len(cands)} candidates", flush=True)
        counts = {"hit": 0, "miss": 0}
        skips: dict[str, int] = {}
        watched = [str(root), str(world.apm_home), str(Path(world.env.get("HOME", root)))]
        for kind, path in cands:
            extra_env = {}
            if kind == "env":
                extra_env = {path: env_value(world, path)}
                undo = lambda: None
            else:
                undo = mutate(path, kind)
                if undo is None:
                    skips[kind] = skips.get(kind, 0) + 1
                    continue
            try:
                code, out, err = run(world, root, APM_INSTALL_FAST_PATH="verdict", **extra_env)
                hit = served(err)
                counts["hit" if hit else "miss"] += 1
                if os.environ.get("ORACLE_VERBOSE"):
                    print(f"ORACLE {'HIT ' if hit else 'miss'} {kind} {path}", flush=True)
                if hit:
                    before = state(watched)
                    fcode, fout, _ferr = run(world, root, APM_INSTALL_FAST_PATH="0", **extra_env)
                    after = state(watched)
                    wrote = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
                    # The engine declines a linked deployed path by contract (diff_apply._within).
                    if kind == "link-swap" or fout != out or fcode != code or wrote:
                        findings.append((FIXTURE, kind, path, MODE))
                        why = (f"exit {code} vs full {fcode}" if fcode != code else "") + (
                            " prints different output" if fout != out else "") + (
                            " writes " + ", ".join(wrote[:3]) if wrote else "")
                        print(f"ORACLE FINDING {label}: {kind} {path} | served while full run{why}",
                              flush=True)
                    else:
                        print(f"ORACLE same {label}: {kind} {path}", flush=True)
            finally:
                undo()
                if not converge(world, root):
                    print(f"ORACLE {label}: could not reconverge after {kind} {path}", flush=True)
                    break
    print(f"ORACLE {label} counts: {counts} skipped-by-kind: {skips} "
          f"elapsed={time.time() - started:.0f}s", flush=True)
    for name_, known in KNOWN.items():
        print(f"ORACLE positive {name_}: {'FLAGGED' if any(known(f) for f in findings) else 'MISSED'}",
              flush=True)
    print(f"ORACLE RESULT: {len(findings)} finding(s)")
    for f in findings:
        print("ORACLE ROW", *f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
