"""Installed-CLI refusal must precede every dependency's native skill writes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_TARGETS = ("claude", "copilot", "codex")
_NATIVE_ROOTS = (".claude", ".agents", ".github", ".codex")
_CHILD_PROVENANCE = """
import atexit as _receipt_atexit
import hashlib as _receipt_hashlib
import json as _receipt_json
from pathlib import Path as _ReceiptPath


def _record_apm_child():
    modules = {}
    for name, module in sorted(sys.modules.items()):
        if name == "apm_cli" or name.startswith("apm_cli."):
            location = getattr(module, "__file__", None)
            if location and _ReceiptPath(location).is_file():
                content = _ReceiptPath(location).read_bytes()
                modules[name] = {
                    "path": location,
                    "sha256": _receipt_hashlib.sha256(content).hexdigest(),
                }
    _ReceiptPath(os.environ["APM_TEST_CHILD_RECEIPT"]).write_text(
        _receipt_json.dumps(
            {
                "executable": sys.executable,
                "argv": sys.argv,
                "pythonpath": os.environ.get("PYTHONPATH"),
                "modules": modules,
            },
            indent=2,
            sort_keys=True,
        ) + "\\n",
        encoding="utf-8",
    )


_receipt_atexit.register(_record_apm_child)
"""


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Capture fixture bytes independently of APM's deployment inventory."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _native_bytes(root: Path) -> dict[str, bytes]:
    """Include unexpected output anywhere under the selected native roots."""
    return {
        f"{native}/{relative}": content
        for native in _NATIVE_ROOTS
        for relative, content in _tree_bytes(root / native).items()
    }


def _lock_receipt(root: Path) -> dict[str, object]:
    """Read canonical and compatibility ownership without using deployed bytes."""
    receipt: dict[str, object] = {}
    for name in ("apm.lock.yaml", "apm.lock"):
        path = root / name
        if not path.exists():
            receipt[name] = None
            continue
        document = load_yaml(path)
        receipt[name] = {
            "bytes": path.read_bytes().hex(),
            "deployments": document.get("deployments", []),
            "dependencies": [
                {
                    key: dependency.get(key)
                    for key in (
                        "repo_url",
                        "path",
                        "deployed_files",
                        "deployed_file_hashes",
                    )
                }
                for dependency in document.get("dependencies", [])
            ],
            "local_deployed_files": document.get("local_deployed_files", []),
            "local_deployed_file_hashes": document.get("local_deployed_file_hashes", {}),
        }
    return receipt


def _hex_bytes(files: dict[str, bytes]) -> dict[str, str]:
    """Keep the complete captured bytes in a JSON-readable evidence receipt."""
    return {name: content.hex() for name, content in files.items()}


def test_later_unmanaged_skill_conflict_refuses_complete_plan_before_native_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A later review collision must not commit an earlier dependency or lint."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    packages = LocalPackageFactory(isolated.work_root)
    earlier = packages.create("a-before-conflict")
    conflicting = packages.create("z-review-lint")
    for package, names in ((earlier, ("prepare",)), (conflicting, ("review", "lint"))):
        for name in names:
            packages.add_skill(
                package,
                name,
                f"---\nname: {name}\ndescription: Fixture {name}\n---\n"
                f"# {name}\nSkill from {package.name}.\n",
            )
        (package.root / ".apm").mkdir()
        (package.root / "skills").rename(package.root / ".apm" / "skills")

    consumer = packages.create(
        "consumer",
        targets=_TARGETS,
        dependencies=({"path": "../a-before-conflict"}, {"path": "../z-review-lint"}),
    )
    local_skill = consumer.root / ".claude" / "skills" / "review" / "SKILL.md"
    local_skill.parent.mkdir(parents=True)
    local_content = (
        b"---\nname: review\ndescription: Project-owned review\n---\n"
        b"# Review\nHand-authored project skill.\n"
    )
    local_skill.write_bytes(local_content)
    unrelated = consumer.root / ".agents" / "skills" / "personal" / "notes.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"Unrelated user-owned content.\n")

    source_before = {package.name: _tree_bytes(package.root) for package in (earlier, conflicting)}
    manifest_before = consumer.manifest_path.read_bytes()
    native_before = _native_bytes(consumer.root)
    home_before = _native_bytes(isolated.home)
    ownership_before = _lock_receipt(consumer.root)
    child_receipt = tmp_path / "installed-cli-child.json"
    env = isolated.subprocess_env(
        overrides={"APM_TEST_CHILD_RECEIPT": str(child_receipt), "NO_COLOR": "1"}
    )
    # Observe the actual CLI child without importing product code at startup.
    guard = Path(env["PYTHONPATH"]) / "sitecustomize.py"
    guard.write_text(guard.read_text(encoding="utf-8") + _CHILD_PROVENANCE, encoding="utf-8")
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy"),
        scenario_id="3202-later-unmanaged-conflict-before-any-native-write",
        cwd=consumer.root,
        env=env,
    )
    native_after = _native_bytes(consumer.root)
    home_after = _native_bytes(isolated.home)
    ownership_after = _lock_receipt(consumer.root)
    source_after = {package.name: _tree_bytes(package.root) for package in (earlier, conflicting)}
    manifest_after = consumer.manifest_path.read_bytes()
    output = result.stdout + result.stderr
    checks = {
        "nonzero_exit": result.returncode != 0,
        "local_skill_preserved": local_skill.read_bytes() == local_content,
        "no_native_writes_including_earlier_dependency": native_after == native_before,
        "no_user_scope_native_writes": home_after == home_before,
        "manifest_intent_preserved": manifest_after == manifest_before,
        "canonical_and_compatibility_ownership_preserved": ownership_after == ownership_before,
        "dependency_sources_preserved": source_after == source_before,
        "ordinary_output_names_skill": "review" in output,
        "ordinary_output_names_dependency": "z-review-lint" in output,
        "ordinary_output_names_conflicting_path": ".claude/skills/review" in output,
        "ordinary_output_names_affected_targets": all(target in output for target in _TARGETS),
        "ordinary_output_states_failure": "[x]" in output,
        "ordinary_output_offers_nondestructive_action": (
            "rename" in output.lower() or "selection" in output.lower()
        ),
    }
    evidence = {
        "scenario": "3202-later-unmanaged-conflict-before-any-native-write",
        "command": result.command,
        "cwd": str(result.cwd),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "checks": checks,
        "native_before": _hex_bytes(native_before),
        "native_after": _hex_bytes(native_after),
        "home_before": _hex_bytes(home_before),
        "home_after": _hex_bytes(home_after),
        "manifest_before": manifest_before.hex(),
        "manifest_after": manifest_after.hex(),
        "ownership_before": ownership_before,
        "ownership_after": ownership_after,
        "source_before": {name: _hex_bytes(files) for name, files in source_before.items()},
        "source_after": {name: _hex_bytes(files) for name, files in source_after.items()},
        "child_provenance": (
            json.loads(child_receipt.read_text(encoding="utf-8"))
            if child_receipt.exists()
            else None
        ),
    }
    evidence_path = tmp_path / "first-red-evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    failures = [name for name, passed in checks.items() if not passed]
    assert failures == [], f"{failures}\nEvidence: {evidence_path}\n{output}"
