"""Verbose install names the package in its per-package diagnostics (#4334).

Two subdirectory packages from one repository, each declaring
``targets: [copilot]``, used to print identical
``Package target restriction`` and ``[i] perf: materialize`` lines under
``apm install --verbose``. Each line now names its package, so every
distinct line appears exactly once and the output oracle (I9) stays quiet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.lifecycle_evidence import output_violations
from tests.utils.lifecycle_scenario import new_lifecycle_scenario

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-INV-I9"),
]

_OWNER = "apm-fixture-org"
_PACKAGES = ("alpha", "beta")
_PER_PACKAGE_PREFIXES = ("Package target restriction", "[i] perf: materialize")


def _skill(name: str) -> str:
    return f"---\nname: {name}\ndescription: Verbose diagnostics fixture {name}\n---\n# {name}\n"


def test_verbose_install_prints_each_package_diagnostic_once(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = new_lifecycle_scenario(tmp_path / "verbose-diagnostics", apm_binary_path)
    source = scenario.isolated.package_root / "mono"
    for name in _PACKAGES:
        package = source / "packages" / name
        skill = package / ".apm" / "skills" / f"{name}-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(_skill(f"{name}-skill"), encoding="utf-8")
        dump_yaml({"name": name, "version": "1.0.0", "targets": ["copilot"]}, package / "apm.yml")
    repository = scenario.repositories.create("mono", source_tree=source)
    commit = scenario.repositories.commit(repository, message="seed mono")
    remote_url = f"https://github.com/{_OWNER}/mono"
    environment = scenario.repositories.url_rewrite_subprocess_env(repository, remote_url)
    # Wide output keeps each diagnostic on one physical line.
    environment["COLUMNS"] = "400"
    consumer = scenario.consumers.create(
        "verbose-consumer",
        dependencies=tuple(
            {"git": remote_url, "path": f"packages/{name}", "ref": commit.sha} for name in _PACKAGES
        ),
        targets=("copilot", "claude"),
    )

    args = ("install", "--target", "copilot", "--verbose", "--parallel-downloads", "0")
    result = scenario.runner.run(
        args, cwd=consumer.root, env=environment, scenario_id="install-verbose-diagnostics"
    )

    evidence = f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, evidence
    lines = [line.strip() for line in result.stdout.splitlines()]
    for prefix in _PER_PACKAGE_PREFIXES:
        emitted = [line for line in lines if line.startswith(prefix)]
        assert len(emitted) == len(_PACKAGES), evidence
        assert len(set(emitted)) == len(emitted), f"{prefix!r} repeats\n{evidence}"
        for name in _PACKAGES:
            assert sum(f"mono/packages/{name}" in line for line in emitted) == 1, evidence
    repeats = [
        violation.message
        for violation in output_violations(args, result)
        if violation.law == "I9"
        and any(prefix in violation.message for prefix in _PER_PACKAGE_PREFIXES)
    ]
    assert not repeats, repeats
    for name in _PACKAGES:
        assert (consumer.root / ".agents" / "skills" / f"{name}-skill" / "SKILL.md").is_file()
