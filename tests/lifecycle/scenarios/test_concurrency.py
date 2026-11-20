import dataclasses
import shutil
import stat

from tests.lifecycle import driver


def test_scratch(lifecycle_world):
    w = lifecycle_world
    real = shutil.which("git", path=w.env["PATH"])
    shim = w.root / "shim"
    shim.mkdir()
    log = w.root / "git.log"
    (shim / "git").write_text(f'#!/bin/sh\necho "$PPID $*" >> {log}\nexec {real} "$@"\n')
    (shim / "git").chmod(0o755)
    env = dict(w.env, PATH=f"{shim}:{w.env['PATH']}")
    w2 = dataclasses.replace(w, env=env)
    for line in (["install", w.packages["p-skill"].spec], ["update", "--yes"], ["uninstall", w.packages["p-skill"].spec], ["approve", w.packages["p-exec"].spec], ["config", "set", "self-update.channel", "prerelease"], ["install", "--global", w.packages["p-skill"].spec]):
        r = driver.run(w2, line)
        print(line[0], r.returncode, r.stdout[-300:], r.stderr[-300:])
        print("LOG", log.read_text() if log.exists() else "")
        log.unlink(missing_ok=True)
    assert 0
