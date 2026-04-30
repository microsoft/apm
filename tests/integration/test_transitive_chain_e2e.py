"""End-to-end coverage for APM transitive dependency chains (gap G5).

Builds a 3-level local chain (pkg-a -> pkg-b -> pkg-c) using file-system
path dependencies and exercises the install + uninstall cascade through the
real CLI binary.  Local paths keep the test deterministic (no network) while
still flowing through the same resolver/lockfile/integration code that
remote APM deps use.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

TIMEOUT = 180


@pytest.fixture
def apm_command():
    """Resolve the APM CLI executable (PATH or local venv)."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


def _write_pkg(pkg_dir: Path, name: str, deps: list, primitive_name: str) -> None:
    """Create a minimal APM package with one instructions primitive."""
    pkg_dir.mkdir(parents=True)
    manifest = {"name": name, "version": "1.0.0", "description": f"{name} test package"}
    if deps:
        manifest["dependencies"] = {"apm": deps}
    (pkg_dir / "apm.yml").write_text(yaml.dump(manifest))
    instructions = pkg_dir / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / f"{primitive_name}.instructions.md").write_text(
        f"---\napplyTo: '**'\n---\n# {primitive_name}\nFrom {name}.\n"
    )


@pytest.fixture
def chain_workspace(tmp_path):
    """Build workspace/{consumer, pkg-a, pkg-b, pkg-c} with a 3-level chain."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    consumer = workspace / "consumer"
    consumer.mkdir()
    (consumer / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "consumer-project",
                "version": "1.0.0",
                "dependencies": {"apm": []},
            }
        )
    )
    (consumer / ".github").mkdir()

    # Sibling layout: ../pkg-x from consumer resolves under workspace/.
    # Transitive local paths are resolved against the consumer's project_root
    # (see _copy_local_package), so chain hops also use ../pkg-y.
    _write_pkg(workspace / "pkg-c", "pkg-c", [], "leaf-skill")
    _write_pkg(workspace / "pkg-b", "pkg-b", ["../pkg-c"], "middle-skill")
    _write_pkg(workspace / "pkg-a", "pkg-a", ["../pkg-b"], "root-skill")

    return workspace


def _load_lockfile(consumer: Path) -> dict:
    lock_path = consumer / "apm.lock.yaml"
    assert lock_path.exists(), "Lockfile not created"
    with open(lock_path) as f:
        return yaml.safe_load(f) or {}


def _deps_by_name(lockfile: dict) -> dict:
    """Index lockfile dependency entries by their unique key (repo_url)."""
    out = {}
    for dep in lockfile.get("dependencies", []) or []:
        key = dep.get("repo_url") or dep.get("name") or ""
        out[key] = dep
    return out


def test_three_level_apm_chain_resolves_all_levels(chain_workspace, apm_command):
    """A->B->C chain installs all three packages and records the dep graph."""
    consumer = chain_workspace / "consumer"

    result = subprocess.run(
        [apm_command, "install", "../pkg-a"],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert result.returncode == 0, f"Install failed: {result.stderr}\n{result.stdout}"

    modules_local = consumer / "apm_modules" / "_local"
    for name in ("pkg-a", "pkg-b", "pkg-c"):
        assert (modules_local / name / "apm.yml").exists(), (
            f"Transitive package {name} not materialised under apm_modules/_local/"
        )

    deps = _deps_by_name(_load_lockfile(consumer))
    for key in ("_local/pkg-a", "_local/pkg-b", "_local/pkg-c"):
        assert key in deps, f"Lockfile missing {key}: have {sorted(deps)}"

    # Direct deps default to depth=1 (omitted), transitives carry depth>=2 + resolved_by.
    assert deps["_local/pkg-a"].get("depth", 1) == 1
    assert deps["_local/pkg-a"].get("resolved_by") in (None, "")
    assert deps["_local/pkg-b"].get("depth", 1) >= 2
    assert deps["_local/pkg-b"].get("resolved_by") == "_local/pkg-a"
    assert deps["_local/pkg-c"].get("depth", 1) >= 3
    assert deps["_local/pkg-c"].get("resolved_by") == "_local/pkg-b"

    deployed = consumer / ".github" / "instructions"
    for fname in (
        "root-skill.instructions.md",
        "middle-skill.instructions.md",
        "leaf-skill.instructions.md",
    ):
        assert (deployed / fname).exists(), (
            f"Primitive {fname} not deployed. Present: {sorted(p.name for p in deployed.glob('*'))}"
        )


def test_three_level_chain_uninstall_root_cascades(chain_workspace, apm_command):
    """Uninstalling the root drops orphaned transitive deps and their primitives."""
    consumer = chain_workspace / "consumer"

    install = subprocess.run(
        [apm_command, "install", "../pkg-a"],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert install.returncode == 0, f"Install failed: {install.stderr}"

    uninstall = subprocess.run(
        [apm_command, "uninstall", "../pkg-a"],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert uninstall.returncode == 0, f"Uninstall failed: {uninstall.stderr}"

    modules_local = consumer / "apm_modules" / "_local"
    for name in ("pkg-a", "pkg-b", "pkg-c"):
        assert not (modules_local / name).exists(), (
            f"Transitive orphan {name} not cleaned from apm_modules/_local/"
        )

    # Lockfile may be deleted entirely when no deps remain; otherwise it must
    # contain no references to the cascaded chain.
    lock_path = consumer / "apm.lock.yaml"
    if lock_path.exists():
        deps = _deps_by_name(yaml.safe_load(lock_path.read_text()) or {})
        for key in ("_local/pkg-a", "_local/pkg-b", "_local/pkg-c"):
            assert key not in deps, f"Lockfile still references {key} after cascade"

    deployed = consumer / ".github" / "instructions"
    for fname in (
        "root-skill.instructions.md",
        "middle-skill.instructions.md",
        "leaf-skill.instructions.md",
    ):
        assert not (deployed / fname).exists(), f"Primitive {fname} survived cascade uninstall"
