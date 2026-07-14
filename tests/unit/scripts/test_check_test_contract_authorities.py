"""Mutation-style tests for test contract authority boundaries."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "check_test_contract_authorities.py"
    spec = importlib.util.spec_from_file_location("check_test_contract_authorities", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_owner_stubs(root: Path) -> None:
    binary_owner = root / "tests" / "integration" / "conftest.py"
    binary_owner.parent.mkdir(parents=True)
    binary_owner.write_text("def _resolve_apm_binary():\n    return None\n", encoding="utf-8")
    parity_owner = root / "scripts" / "check_cli_docs.py"
    parity_owner.parent.mkdir(parents=True)
    parity_owner.write_text(
        "def public_top_level_commands():\n    pass\n"
        "def rendered_cli_reference_pages():\n    pass\n"
        "def registry_docs_mismatches():\n    pass\n",
        encoding="utf-8",
    )


def test_known_binary_fallback_chain_is_rejected(tmp_path: Path) -> None:
    """The retired silent-adopt resolver shape must trip the boundary."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "duplicate.py"
    duplicate.write_text(
        "import os\n"
        "import shutil\n"
        "from pathlib import Path\n"
        "def apm_command():\n"
        "    for env_var in ('APM_TEST_BINARY', 'APM_BINARY_PATH'):\n"
        "        override = os.environ.get(env_var)\n"
        "        if override and Path(override).exists():\n"
        "            return override\n"
        "    candidate = Path('.venv') / 'bin' / 'apm'\n"
        "    return str(candidate) if candidate.exists() else shutil.which('apm')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "duplicate integration binary selection" in violations[0]


def test_binary_fixture_delegation_is_allowed(tmp_path: Path) -> None:
    """A consumer accepting the canonical fixture is not a second owner."""
    _write_owner_stubs(tmp_path)
    consumer = tmp_path / "tests" / "integration" / "consumer.py"
    consumer.write_text(
        "def apm_command(apm_binary_path):\n    return str(apm_binary_path)\n",
        encoding="utf-8",
    )

    assert _load_checker().find_binary_selection_violations(tmp_path) == []


def test_binary_environment_subscript_fallback_is_rejected(tmp_path: Path) -> None:
    """Subscript syntax must not evade the binary-selection boundary."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "subscript_duplicate.py"
    duplicate.write_text(
        "import os\n"
        "import shutil\n"
        "def apm_command():\n"
        "    configured = os.environ['APM_BINARY_PATH']\n"
        "    return configured or shutil.which('apm')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "duplicate integration binary selection" in violations[0]


def test_known_rendered_parity_reimplementation_is_rejected(tmp_path: Path) -> None:
    """A second registry/page set comparison must trip the boundary."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "scripts" / "duplicate_parity.py"
    duplicate.write_text(
        "def duplicate(group, dist):\n"
        "    commands = {name for name, command in group.commands.items() "
        "if not command.hidden}\n"
        "    cli_dir = dist / 'reference' / 'cli'\n"
        "    pages = {child.name for child in cli_dir.iterdir() "
        "if (child / 'index.html').is_file()}\n"
        "    return sorted(commands - pages), sorted(pages - commands)\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_rendered_parity_violations(tmp_path)

    assert len(violations) == 1
    assert "duplicate rendered CLI parity computation" in violations[0]


def test_rendered_parity_helper_delegation_is_allowed(tmp_path: Path) -> None:
    """A consumer importing the canonical comparison is not a second owner."""
    _write_owner_stubs(tmp_path)
    consumer = tmp_path / "scripts" / "consumer.py"
    consumer.write_text(
        "from scripts.check_cli_docs import registry_docs_mismatches\n"
        "def check(group, dist):\n"
        "    return registry_docs_mismatches(group, dist)\n",
        encoding="utf-8",
    )

    assert _load_checker().find_rendered_parity_violations(tmp_path) == []
