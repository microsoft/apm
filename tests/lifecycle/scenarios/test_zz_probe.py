import json
from pathlib import Path

import pytest

from tests.lifecycle import driver
from tests.lifecycle.world import use_policy

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary, pytest.mark.promise("P-INV-I1")]
OUT = Path(
    "/Users/danielmeppiel/.copilot/session-state/b208fd4c-77c5-41bb-b19a-02c7aa735031/files/probe-t2.txt"
)


def _w(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="ascii")


@pytest.mark.parametrize("scope", ["project", "user"])
def test_native(lifecycle_world, scope):
    w = lifecycle_world
    use_policy(w, "allow")
    root = w.root / "p-native-hooks"
    _w(
        root / ".claude-plugin/plugin.json",
        {"name": "p-native-hooks", "version": "1.0.0", "description": "x"},
    )
    _w(
        root / "hooks/hooks.json",
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "echo p-native-hooks"}]}]
            }
        },
    )
    _w(
        root / ".lsp.json",
        {
            "p-native-hooks-server": {
                "command": "apm-lifecycle-inert-server",
                "args": ["--stdio"],
                "extensionToLanguage": {".fixture": "fixture"},
            }
        },
    )
    manifest_root = w.project if scope == "project" else w.apm_home
    import yaml

    m = manifest_root / "apm.yml"
    d = yaml.safe_load(m.read_text())
    d["targets"] = ["claude"]
    m.write_text(yaml.safe_dump(d))
    out = []
    for argv in (("install", str(root)), ("approve", str(root)), ("install",)):
        r = driver.run(w, argv, scope=scope)
        out.append(f"=== {scope} {argv[0]} rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    base = w.project if scope == "project" else w.home
    out.append(
        "files: "
        + " ".join(
            sorted(
                str(p.relative_to(base))
                for p in base.rglob("*")
                if p.is_file()
                and "apm_modules" not in p.parts
                and ".git/" not in str(p)
                and ".cache" not in p.parts
            )
        )
    )
    for f in (".claude/settings.json",):
        if (base / f).exists():
            out.append(f"{f}: {(base / f).read_text()}")
    with OUT.open("a") as fh:
        fh.write("\n".join(out) + "\n")
