"""Static ownership and least-privilege contracts for the docs workflow."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "docs.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_docs_contract_runs_for_every_cli_registration_owner() -> None:
    """Command names and visibility cannot change without scheduling parity."""
    workflow = _workflow()
    pull_request = workflow[
        workflow.index("  pull_request:") : workflow.index("  workflow_dispatch:")
    ]

    assert "'docs/**'" in pull_request
    assert "'src/apm_cli/cli.py'" in pull_request
    assert "'src/apm_cli/commands/**'" in pull_request
    assert "'scripts/check_cli_docs.py'" in pull_request
    assert "'tests/unit/test_cli_docs_contract.py'" in pull_request


def test_docs_build_has_read_only_permissions() -> None:
    """Pages write and OIDC minting belong only to the deploy job."""
    workflow = _workflow()
    top_level = workflow[workflow.index("permissions:") : workflow.index("concurrency:")]
    build_job = workflow[workflow.index("  build:") : workflow.index("  deploy:")]
    deploy_job = workflow[workflow.index("  deploy:") :]

    assert "contents: read" in top_level
    assert "pages: write" not in top_level
    assert "id-token: write" not in top_level
    assert "pages: write" not in build_job
    assert "id-token: write" not in build_job
    assert "pages: write" in deploy_job
    assert "id-token: write" in deploy_job


def test_docs_python_operations_are_frozen() -> None:
    """The docs owner must consume the reviewed lock without mutation."""
    workflow = _workflow()
    build_job = workflow[workflow.index("  build:") : workflow.index("  deploy:")]

    assert "uv sync --frozen --extra dev" in build_job
    assert "uv run --frozen pytest tests/unit/test_cli_docs_contract.py -q" in build_job
    assert "uv run --frozen python scripts/check_cli_docs.py docs/dist" in build_job
