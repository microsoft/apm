"""Installed-CLI receipts for OpenAPM manifest requirements (req-mf-*).

Runs the packaged `apm` binary in a hermetic world. In-process manifest
receipts live in test_manifest_behavior_reqs.py.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from tests.integration.conftest import apm_binary_path as apm_binary_path
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("req-mf-002", "req-mf-003", "req-mf-004", "req-mf-006"),
]

# semver 2.0.0 Section 9 reference regular expression (spec req-mf-004).
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


@pytest.mark.req("req-mf-002", "req-mf-003", "req-mf-004")
def test_init_emits_named_project_with_semver_version_string(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """`apm init --yes` writes a non-empty string name and a semver string version."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "init", base_env=os.environ)
    project = isolated.work_root / "demo-project"
    project.mkdir(parents=True)

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("init", "--yes"),
        scenario_id="producer-init-identity",
        cwd=project,
        env=isolated.subprocess_env(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
    assert isinstance(manifest["name"], str) and manifest["name"].strip()
    assert isinstance(manifest["version"], str)
    assert _SEMVER.fullmatch(manifest["version"]), manifest["version"]


# --- req-mf-006: rewrite preserves unknown top-level keys ---------------


@pytest.mark.req("req-mf-006")
def test_install_rewrite_preserves_unknown_top_level_keys(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """`apm install <path>` rewrites apm.yml and keeps keys it does not understand."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "rewrite", base_env=os.environ)
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("helper")
    factory.add_instruction(
        dependency, "style", "---\napplyTo: '**'\n---\n# Style\nUse short names.\n"
    )
    consumer = factory.create("consumer", targets=("copilot",))
    with consumer.manifest_path.open("a", encoding="utf-8") as handle:
        handle.write("future-field:\n  mode: strict\n  levels: [1, 2]\nx-acme-meta: keep-me\n")

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "../helper", "--target", "copilot"),
        scenario_id="manifest-rewrite-unknown-keys",
        cwd=consumer.root,
        env=isolated.subprocess_env(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    rewritten = yaml.safe_load(consumer.manifest_path.read_text(encoding="utf-8"))
    assert rewritten["dependencies"]["apm"] == ["../helper"]
    assert rewritten["future-field"] == {"mode": "strict", "levels": [1, 2]}
    assert rewritten["x-acme-meta"] == "keep-me"


# --- req-mf-021 / req-rs-013: keys reserved for v0.2 -----------------------


@pytest.mark.req("req-mf-021")
def test_top_level_workspaces_key_warns_reserved_and_installs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """req-mf-021: a v0.1 `workspaces:` key gets a non-blocking reserved-for-v0.2 diagnostic."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "ws", base_env=os.environ)
    factory = LocalPackageFactory(isolated.work_root)
    consumer = factory.create("consumer", targets=("copilot",))
    with consumer.manifest_path.open("a", encoding="utf-8") as handle:
        handle.write("workspaces:\n  - packages/*\n")

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install",), scenario_id="mf-021", cwd=consumer.root, env=isolated.subprocess_env()
    )

    text = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 0, text
    assert "[!]" in text, text
    assert "'workspaces'" in text and "reserved for OpenAPM v0.2" in text, text
    assert text.count("reserved for OpenAPM v0.2") == 1, text
    assert "Unknown top-level key" not in text, text


@pytest.mark.req("req-rs-013")
def test_conflict_resolution_nest_refuses_install_and_writes_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """req-rs-013: `conflict_resolution: nest` refuses, cites Section 7.2 (3), writes nothing."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "nest", base_env=os.environ)
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("helper")
    factory.add_instruction(
        dependency, "style", "---\napplyTo: '**'\n---\n# Style\nUse short names.\n"
    )
    consumer = factory.create("consumer", dependencies=("../helper",), targets=("copilot",))
    manifest = yaml.safe_load(consumer.manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"]["conflict_resolution"] = "nest"
    consumer.manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), "utf-8")
    before = sorted(p.relative_to(consumer.root) for p in consumer.root.rglob("*"))

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install",), scenario_id="rs-013", cwd=consumer.root, env=isolated.subprocess_env()
    )

    text = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, text
    assert "conflict_resolution: nest" in text and "reserved for OpenAPM v0.2" in text, text
    assert "Section 7.2" in text and "clause (3)" in text, text
    assert sorted(p.relative_to(consumer.root) for p in consumer.root.rglob("*")) == before
