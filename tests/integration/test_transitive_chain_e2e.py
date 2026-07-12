"""End-to-end coverage for APM transitive dependency chains (gap G5).

Builds a 3-level local chain (pkg-a -> pkg-b -> pkg-c) using file-system
path dependencies and exercises the install + uninstall cascade through the
real CLI binary.  Local paths keep the test deterministic (no network) while
still flowing through the same resolver/lockfile/integration code that
remote APM deps use.
"""

import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requires_apm_binary

TIMEOUT = 180
DEEP_CHAIN_LENGTH = 8


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


def _write_embedded_manifest(package: Path, relative_dir: str, name: str) -> Path:
    """Add parent-owned source content that resembles an APM package."""
    embedded = package / relative_dir
    embedded.mkdir(parents=True)
    (embedded / "apm.yml").write_text(
        yaml.dump({"name": name, "version": "1.0.0"}),
        encoding="utf-8",
    )
    payload = embedded / "payload.txt"
    payload.write_text(f"{name} content\n", encoding="utf-8")
    return payload


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
                "target": "copilot",
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


@pytest.fixture
def deep_chain_workspace(tmp_path: Path) -> Path:
    """Build a local eight-package chain with embedded parent-owned manifests."""
    workspace = tmp_path / "deep-workspace"
    workspace.mkdir()
    consumer = workspace / "consumer"
    consumer.mkdir()
    (consumer / ".github").mkdir()
    (consumer / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "deep-consumer",
                "version": "1.0.0",
                "target": "copilot",
                "dependencies": {"apm": []},
            }
        ),
        encoding="utf-8",
    )

    package_names = [f"pkg-depth-{depth}" for depth in range(1, DEEP_CHAIN_LENGTH + 1)]
    for index, name in enumerate(package_names):
        dependencies = [f"../{package_names[index + 1]}"] if index + 1 < len(package_names) else []
        _write_pkg(workspace / name, name, dependencies, f"primitive-{index + 1}")

    root_package = workspace / package_names[0]
    _write_embedded_manifest(root_package, "examples/embedded-shallow", "embedded-shallow")
    _write_embedded_manifest(
        root_package,
        "vendor/fixtures/nested/embedded-deep",
        "embedded-deep",
    )
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
    assert (modules_local / "pkg-a" / "apm.yml").exists()
    for name in ("pkg-b", "pkg-c"):
        matches = list(modules_local.glob(f"*/{name}/apm.yml"))
        assert len(matches) == 1, (
            f"Transitive package {name} not materialised in its parent-scoped slot"
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


def test_deps_commands_follow_full_lock_graph_and_ignore_embedded_manifests(
    deep_chain_workspace: Path,
    apm_command: str,
) -> None:
    """CLI inventory follows every resolved edge but ignores parent-owned manifests."""
    consumer = deep_chain_workspace / "consumer"
    package_names = [f"pkg-depth-{depth}" for depth in range(1, DEEP_CHAIN_LENGTH + 1)]
    package_keys = [f"_local/{name}" for name in package_names]
    tree_keys = [f"../{name}" for name in package_names]
    embedded_names = ("embedded-shallow", "embedded-deep")

    installed = subprocess.run(
        [apm_command, "install", f"../{package_names[0]}"],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert installed.returncode == 0, f"Install failed: {installed.stderr}\n{installed.stdout}"

    locked = _deps_by_name(_load_lockfile(consumer))
    assert list(locked) == package_keys
    for depth, key in enumerate(package_keys, start=1):
        assert locked[key].get("depth", 1) == depth
        expected_parent = package_keys[depth - 2] if depth > 1 else None
        assert locked[key].get("resolved_by") in (expected_parent, "")

    listed = subprocess.run(
        [apm_command, "deps", "list"],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert listed.returncode == 0, f"deps list failed: {listed.stderr}\n{listed.stdout}"
    list_output = listed.stdout + listed.stderr
    for key in package_keys:
        assert key in list_output
    for embedded_name in embedded_names:
        assert embedded_name not in list_output
    assert "orphaned package(s) found" not in list_output

    tree = subprocess.run(
        [apm_command, "deps", "tree"],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert tree.returncode == 0, f"deps tree failed: {tree.stderr}\n{tree.stdout}"
    tree_output = tree.stdout + tree.stderr
    tree_lines = [line for line in tree_output.splitlines() if "../pkg-depth-" in line]
    assert len(tree_lines) == DEEP_CHAIN_LENGTH, tree_output
    assert all(key in line for key, line in zip(tree_keys, tree_lines, strict=True))
    key_columns = [line.index(key) for key, line in zip(tree_keys, tree_lines, strict=True)]
    assert all(left < right for left, right in pairwise(key_columns))
    for embedded_name in embedded_names:
        assert embedded_name not in tree_output

    pruned = subprocess.run(
        [apm_command, "prune"],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert pruned.returncode == 0, f"prune failed: {pruned.stderr}\n{pruned.stdout}"
    prune_output = pruned.stdout + pruned.stderr
    for embedded_name in embedded_names:
        assert embedded_name not in prune_output
    # Direct dep (depth-1) lands in a flat ``_local/pkg`` slot; transitive
    # local deps are materialised in parent-scoped hashed slots
    # (``_local/<hash>/pkg``) per #2155, so locate them by glob the same way
    # test_three_level_apm_chain_resolves_all_levels does.
    modules_local = consumer / "apm_modules" / "_local"
    assert (modules_local / package_names[0] / "apm.yml").is_file()
    for package_name in package_names[1:]:
        matches = list(modules_local.glob(f"*/{package_name}/apm.yml"))
        assert len(matches) == 1, (
            f"Transitive package {package_name} not materialised in its parent-scoped slot"
        )
    assert (
        consumer
        / "apm_modules"
        / "_local"
        / package_names[0]
        / "examples"
        / "embedded-shallow"
        / "payload.txt"
    ).is_file()
    assert (
        consumer
        / "apm_modules"
        / "_local"
        / package_names[0]
        / "vendor"
        / "fixtures"
        / "nested"
        / "embedded-deep"
        / "payload.txt"
    ).is_file()


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
        assert not list(modules_local.rglob(name)), (
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


def test_asymmetric_layout_anchors_on_declaring_pkg(tmp_path, apm_command):
    """Regression for #857: a transitive ../sibling resolves against the
    DECLARING package's directory, not the consumer's project root.

    Layout (asymmetric — old behaviour would look for /tmp/.../base which
    is OUTSIDE the consumer root and fail):

        consumer/
            apm.yml             -> ./packages/specialized
            packages/
                specialized/
                    apm.yml     -> ../base       (resolves to packages/base)
                base/
                    apm.yml
    """
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    pkgs = consumer / "packages"
    pkgs.mkdir()

    _write_pkg(pkgs / "base", "base-pkg", [], "base-skill")
    _write_pkg(pkgs / "specialized", "specialized-pkg", ["../base"], "specialized-skill")

    (consumer / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "consumer",
                "version": "1.0.0",
                "target": "copilot",
                "dependencies": {"apm": ["./packages/specialized"]},
            }
        )
    )

    result = subprocess.run(
        [apm_command, "install"],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert result.returncode == 0, (
        f"install failed (#857 regression?):\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # Both packages must be materialized — the transitive ../base proves the
    # anchor is on specialized/, not on consumer/. Install path uses the
    # source-dir basename (NOT the apm.yml `name` field).
    assert (consumer / "apm_modules" / "_local" / "specialized").exists()
    assert len(list((consumer / "apm_modules" / "_local").glob("*/base"))) == 1
    # No "outside the project root" rejection should appear in either stream.
    combined = result.stdout + result.stderr
    assert "outside the project root" not in combined, combined
