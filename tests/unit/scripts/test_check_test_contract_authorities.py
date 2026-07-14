"""Adversarial tests for test contract authority boundaries."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


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


@pytest.mark.parametrize(
    "source",
    (
        "import os\ndef renamed():\n    return os.environ.get('APM_BINARY_PATH') or 'apm'\n",
        "import os as _os\ndef renamed():\n    return _os.environ.get('APM_BINARY_PATH')\n",
        "import os\ndef choose():\n    return os.getenv('APM_BINARY_PATH')\n",
        "import os\ndef choose():\n    return os.environ['APM_BINARY_PATH']\n",
        "from os import environ\ndef choose():\n    return environ.get('APM_BINARY_PATH')\n",
        "from os import environ as env\ndef choose():\n    return env['APM_BINARY_PATH']\n",
        "def choose():\n"
        "    import os as nested_os\n"
        "    return nested_os.environ.get('APM_BINARY_PATH')\n",
    ),
)
def test_any_direct_binary_environment_read_is_rejected(
    tmp_path: Path,
    source: str,
) -> None:
    """Names and fallback syntax cannot evade canonical binary selection."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "duplicate.py"
    duplicate.write_text(source, encoding="utf-8")

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct APM_BINARY_PATH read" in violations[0]


def test_binary_fixture_delegation_is_allowed(tmp_path: Path) -> None:
    """A consumer accepting the canonical fixture is not a second owner."""
    _write_owner_stubs(tmp_path)
    consumer = tmp_path / "tests" / "integration" / "consumer.py"
    consumer.write_text(
        "def apm_command(apm_binary_path):\n    return str(apm_binary_path)\n",
        encoding="utf-8",
    )

    assert _load_checker().find_binary_selection_violations(tmp_path) == []


def test_renamed_binary_resolver_is_rejected(tmp_path: Path) -> None:
    """A renamed resolver still fails because it reads the owned variable."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "renamed.py"
    duplicate.write_text(
        "import os\ndef resolve_tool():\n    return os.environ.get('APM_BINARY_PATH')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct APM_BINARY_PATH read" in violations[0]


def test_comment_only_parity_owner_definitions_are_rejected(tmp_path: Path) -> None:
    """Owner presence must come from AST definitions, not comments."""
    _write_owner_stubs(tmp_path)
    owner = tmp_path / "scripts" / "check_cli_docs.py"
    owner.write_text(
        "# def public_top_level_commands():\n"
        "# def rendered_cli_reference_pages():\n"
        "# def registry_docs_mismatches():\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_rendered_parity_violations(tmp_path)

    assert len(violations) == 3
    assert all("must define rendered parity owner function" in item for item in violations)


def test_internal_projection_import_is_rejected(tmp_path: Path) -> None:
    """External consumers may import only the parity facade."""
    _write_owner_stubs(tmp_path)
    consumer = tmp_path / "tests" / "consumer.py"
    consumer.parent.mkdir(exist_ok=True)
    consumer.write_text(
        "from scripts.check_cli_docs import public_top_level_commands\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_rendered_parity_violations(tmp_path)

    assert len(violations) == 1
    assert "internal rendered parity projection imported" in violations[0]


def test_direct_registry_projection_is_rejected_without_comparison(tmp_path: Path) -> None:
    """Registry projection alone is owned, independent of set-difference syntax."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "scripts" / "registry_projection.py"
    duplicate.write_text(
        "def renamed(group):\n"
        "    return {name for name, command in group.commands.items() "
        "if not command.hidden}\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_rendered_parity_violations(tmp_path)

    assert len(violations) == 1
    assert "direct Click command registry projection" in violations[0]


def test_unrelated_recursive_registry_walk_is_allowed(tmp_path: Path) -> None:
    """A recursive help audit is not a rendered parity projection."""
    _write_owner_stubs(tmp_path)
    consumer = tmp_path / "tests" / "help_audit.py"
    consumer.parent.mkdir(exist_ok=True)
    consumer.write_text(
        "def walk(group):\n"
        "    for name, command in group.commands.items():\n"
        "        yield name, command\n",
        encoding="utf-8",
    )

    assert _load_checker().find_rendered_parity_violations(tmp_path) == []


def test_loop_based_registry_projection_is_rejected(tmp_path: Path) -> None:
    """A loop that builds the public command set is still a projection."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "scripts" / "loop_projection.py"
    duplicate.write_text(
        "def project(group):\n"
        "    result = set()\n"
        "    for name, command in group.commands.items():\n"
        "        if not command.hidden:\n"
        "            result.add(name)\n"
        "    return result\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_rendered_parity_violations(tmp_path)

    assert len(violations) == 1
    assert "direct Click command registry projection" in violations[0]


def test_direct_rendered_inventory_is_rejected_with_imported_registry(
    tmp_path: Path,
) -> None:
    """Importing one side cannot permit recomputing the other side."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "scripts" / "page_projection.py"
    duplicate.write_text(
        "from scripts.check_cli_docs import public_top_level_commands\n"
        "def renamed(dist):\n"
        "    cli_dir = dist / 'reference' / 'cli'\n"
        "    return {child.name for child in cli_dir.iterdir() "
        "if (child / 'index.html').is_file()}\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_rendered_parity_violations(tmp_path)

    assert len(violations) >= 2
    assert any("internal rendered parity projection imported" in item for item in violations)
    assert any("direct rendered CLI route inventory" in item for item in violations)


def test_split_path_rendered_inventory_is_rejected(tmp_path: Path) -> None:
    """Splitting reference/cli across assignments cannot evade ownership."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "scripts" / "split_page_projection.py"
    duplicate.write_text(
        "def project(dist):\n"
        "    base = dist / 'reference'\n"
        "    cli_dir = base / 'cli'\n"
        "    return {child.name for child in cli_dir.iterdir() "
        "if (child / 'index.html').is_file()}\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_rendered_parity_violations(tmp_path)

    assert any("direct rendered CLI route inventory" in item for item in violations)


def test_direct_registry_projection_is_rejected_with_imported_pages(
    tmp_path: Path,
) -> None:
    """Importing page projection cannot permit recomputing the registry side."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "scripts" / "registry_projection.py"
    duplicate.write_text(
        "from scripts.check_cli_docs import rendered_cli_reference_pages\n"
        "def renamed(group):\n"
        "    return {name for name, command in group.commands.items() "
        "if not command.hidden}\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_rendered_parity_violations(tmp_path)

    assert len(violations) == 2
    assert any("internal rendered parity projection imported" in item for item in violations)
    assert any("direct Click command registry projection" in item for item in violations)


def test_set_difference_parity_reimplementation_is_rejected(tmp_path: Path) -> None:
    """Method-form set differences cannot evade projection ownership."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "scripts" / "difference_parity.py"
    duplicate.write_text(
        "def compare(group, dist):\n"
        "    commands = {name for name, command in group.commands.items() "
        "if not command.hidden}\n"
        "    cli_dir = dist / 'reference' / 'cli'\n"
        "    pages = {child.name for child in cli_dir.iterdir() "
        "if (child / 'index.html').is_file()}\n"
        "    return commands.difference(pages), pages.difference(commands)\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_rendered_parity_violations(tmp_path)

    assert any("direct Click command registry projection" in item for item in violations)
    assert any("direct rendered CLI route inventory" in item for item in violations)


def test_rendered_parity_facade_delegation_is_allowed(tmp_path: Path) -> None:
    """Consumers may import and call the canonical comparison facade."""
    _write_owner_stubs(tmp_path)
    consumer = tmp_path / "tests" / "consumer.py"
    consumer.parent.mkdir(exist_ok=True)
    consumer.write_text(
        "from scripts.check_cli_docs import registry_docs_mismatches\n"
        "def check(group, dist):\n"
        "    return registry_docs_mismatches(group, dist)\n",
        encoding="utf-8",
    )

    assert _load_checker().find_rendered_parity_violations(tmp_path) == []


@pytest.mark.parametrize(
    "source",
    (
        "import scripts.check_cli_docs\n",
        "from scripts import check_cli_docs\n",
    ),
)
def test_direct_parity_module_import_is_rejected(
    tmp_path: Path,
    source: str,
) -> None:
    """Direct module imports cannot bypass facade-only consumption."""
    _write_owner_stubs(tmp_path)
    consumer = tmp_path / "tests" / "consumer.py"
    consumer.parent.mkdir(exist_ok=True)
    consumer.write_text(source, encoding="utf-8")

    violations = _load_checker().find_rendered_parity_violations(tmp_path)

    assert len(violations) == 1
    assert "rendered parity module imported directly" in violations[0]
