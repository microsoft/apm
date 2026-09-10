"""Regression traps for contract ownership and legacy execution bypasses."""

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.contract_leaf_runtime import check_contract_owners
from scripts.architecture_linter.facts import FactsProvider

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("frontend.py", "from ..core.script_runner import ScriptRunner\n"),
        ("engine.py", "subprocess.Popen(command)\n"),
        ("workspace.py", "digest = compute_file_hash(path)\n"),
        ("engine.py", "def reduce_outcome():\n    return 0\n"),
        ("engine.py", "RuntimeFactory.get_best_available_runtime()\n"),
    ],
)
def test_contract_owner_bypasses_are_rejected(tmp_path: Path, filename: str, source: str) -> None:
    path = f"src/apm_cli/contracts/{filename}"
    provider = FactsProvider(tmp_path, (path,), None, source_overrides={path: source})
    findings = check_contract_owners(provider)
    assert len(findings) == 1
    assert findings[0].path == path
    assert findings[0].line >= 1


def test_contract_owner_routes_are_accepted(tmp_path: Path) -> None:
    sources = {
        "src/apm_cli/contracts/process.py": "subprocess.Popen(request.argv)\n",
        "src/apm_cli/contracts/records.py": "def reduce_outcome():\n    return result\n",
        "src/apm_cli/contracts/engine.py": "result = records.reduce_outcome()\n",
    }
    provider = FactsProvider(tmp_path, tuple(sources), None, source_overrides=sources)
    assert check_contract_owners(provider) == ()


@pytest.mark.parametrize(
    "path",
    ["src/apm_cli/apmx.py", "src/apm_cli/install/contract_source.py"],
)
@pytest.mark.parametrize(
    "source",
    [
        "subprocess.Popen(command)\n",
        "subprocess.run(command)\n",
        "run_install_pipeline(project)\n",
        "def reduce_outcome():\n    return 0\n",
    ],
)
def test_packaged_entrypoint_bypasses_are_rejected(tmp_path: Path, path: str, source: str) -> None:
    provider = FactsProvider(tmp_path, (path,), None, source_overrides={path: source})
    findings = check_contract_owners(provider)
    assert len(findings) == 1
    assert findings[0].path == path


def test_packaged_entrypoint_delegation_is_accepted(tmp_path: Path) -> None:
    sources = {
        "src/apm_cli/apmx.py": "invoke_contract(ctx, contract, source=source)\n",
        "src/apm_cli/install/contract_source.py": "downloader.download_package(reference, target)\n",
    }
    provider = FactsProvider(tmp_path, tuple(sources), None, source_overrides=sources)
    assert check_contract_owners(provider) == ()


def test_packaged_entrypoint_cannot_duplicate_harness_choices(tmp_path: Path) -> None:
    path = "src/apm_cli/apmx.py"
    provider = FactsProvider(
        tmp_path,
        (path,),
        None,
        source_overrides={path: 'click.option("--on", type=click.Choice(["copilot"]))\n'},
    )
    findings = check_contract_owners(provider)
    assert len(findings) == 1
    assert findings[0].path == path
