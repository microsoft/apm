from __future__ import annotations

import os
from pathlib import Path

import yaml

from tests.lifecycle import driver


def _tree(root: Path) -> list[str]:
    out = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not rel.startswith((".git/", "apm_modules/")):
            out.append(rel)
    return out


def _run(world, line, scope="project"):
    r = driver.run(world, line, scope=scope)
    print(f"\n=== {line} [{scope}] rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def _show(world, root):
    print("FILES:", _tree(root))
    for rel in (".mcp.json", ".github/lsp.json", "apm.yml", ".claude/settings.json"):
        p = root / rel
        if p.is_file():
            print(f"--- {rel}\n{p.read_text()}")
    lock = world.project / "apm.lock.yaml"
    if lock.is_file():
        print("--- lock\n" + lock.read_text())


def test_probe(lifecycle_world):
    world = lifecycle_world
    which = os.environ.get("PROBE", "exec")
    if which == "exec":
        spec = world.packages["p-exec"].spec
        _run(world, f"install {spec}")
        _run(world, f"approve {spec}")
        _run(world, "install")
        _show(world, world.project)
        hook = Path(spec) / ".apm" / "hooks" / "session.json"
        print("HOOKSRC", hook, hook.is_file())
        hook.write_text(hook.read_text().replace("echo p-exec", "echo p-exec-v2"))
        _run(world, "install")
        _show(world, world.project)
    elif which == "native":
        data = yaml.safe_load((world.project / "apm.yml").read_text())
        data["targets"] = ["claude"]
        (world.project / "apm.yml").write_text(yaml.safe_dump(data))
        spec = world.packages["p-native-claude"].spec
        _run(world, f"install {spec}")
        _show(world, world.project)
        _run(world, f"approve {spec}")
        _run(world, "install")
        _show(world, world.project)
        _run(world, f"deny {spec}")
        _run(world, "install")
        _show(world, world.project)
    elif which == "canvas":
        spec = world.packages["p-canvas"].spec
        _run(world, f"install {spec}")
        _show(world, world.project)
        _run(world, f"approve {spec}")
        _run(world, "install")
        _show(world, world.project)
        _run(world, f"deny {spec}")
        _run(world, "install")
        _show(world, world.project)
    elif which == "bin":
        spec = world.packages["p-exec"].spec
        data = yaml.safe_load((world.apm_home / "apm.yml").read_text())
        data["targets"] = ["claude"]
        (world.apm_home / "apm.yml").write_text(yaml.safe_dump(data))
        _run(world, f"install {spec}", "user")
        _run(world, f"approve {spec}", "user")
        _run(world, "install", "user")
        print("HOME", _tree(world.home))
        _run(world, f"deny {spec}", "user")
        _run(world, "install", "user")
        print("HOME", _tree(world.home))
