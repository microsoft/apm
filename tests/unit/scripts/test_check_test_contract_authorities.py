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
        "import os\ndef choose():\n    env = os.environ\n    return env.get('APM_BINARY_PATH')\n",
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
        "def run_command(apm_binary_path):\n    return str(apm_binary_path)\n",
        encoding="utf-8",
    )

    assert _load_checker().find_binary_selection_violations(tmp_path) == []


def test_direct_os_getenv_binary_read_is_rejected(tmp_path: Path) -> None:
    """Deleting the os.getenv detector must fail independently."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "getenv_duplicate.py"
    duplicate.write_text(
        "import os\ndef choose():\n    return os.getenv('APM_BINARY_PATH')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct APM_BINARY_PATH read" in violations[0]


def test_path_lookup_without_env_or_venv_is_rejected(tmp_path: Path) -> None:
    """Deleting the PATH detector must fail independently."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "path_duplicate.py"
    duplicate.write_text(
        "import shutil\ndef choose():\n    return shutil.which('apm')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct PATH lookup for apm" in violations[0]


def test_aliased_path_lookup_is_rejected(tmp_path: Path) -> None:
    """Import aliases cannot evade the PATH selector boundary."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "path_alias_duplicate.py"
    duplicate.write_text(
        "from shutil import which as locate\ndef choose():\n    return locate('apm')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct PATH lookup for apm" in violations[0]


def test_assigned_path_lookup_alias_is_rejected(tmp_path: Path) -> None:
    """A simple assignment alias must preserve shutil.which authority."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "path_assignment.py"
    duplicate.write_text(
        "import shutil\nlocate = shutil.which\ndef choose():\n    return locate('apm')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct PATH lookup for apm" in violations[0]


def test_shadowed_shutil_binding_does_not_duplicate_diagnostics(tmp_path: Path) -> None:
    """Parameter/local shadowing must not inherit the outer import binding."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "shadowed.py"
    duplicate.write_text(
        "import shutil\n"
        "outer = shutil.which('apm')\n"
        "def parameter_shadow(shutil):\n"
        "    return shutil.which('apm')\n"
        "def local_shadow():\n"
        "    shutil = object()\n"
        "    return shutil.which('apm')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct PATH lookup for apm" in violations[0]


def test_shadowed_os_binding_does_not_duplicate_diagnostics(tmp_path: Path) -> None:
    """Only the genuine outer os binding may own an environment read."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "shadowed_os.py"
    duplicate.write_text(
        "import os\n"
        "outer = os.getenv('APM_BINARY_PATH')\n"
        "def parameter_shadow(os):\n"
        "    return os.getenv('APM_BINARY_PATH')\n"
        "def local_shadow():\n"
        "    os = object()\n"
        "    return os.getenv('APM_BINARY_PATH')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct APM_BINARY_PATH read" in violations[0]


def test_venv_fallback_without_env_or_which_is_rejected(tmp_path: Path) -> None:
    """Deleting the .venv detector must fail independently."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "venv_duplicate.py"
    duplicate.write_text(
        "from pathlib import Path\n"
        "def choose():\n"
        "    candidate = Path('.venv') / 'bin' / 'apm'\n"
        "    return str(candidate) if candidate.exists() else None\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct .venv apm fallback" in violations[0]


@pytest.mark.parametrize(
    "selector",
    (
        "Path('.venv', 'bin', 'apm')",
        "PurePath('.venv', 'bin', 'apm')",
        "Path('.venv').joinpath('Scripts', 'apm.exe')",
        "PurePosixPath('.venv', 'bin', 'apm')",
        "PureWindowsPath('.venv', 'Scripts', 'apm.exe')",
        "os.path.join('.venv', 'bin', 'apm')",
    ),
)
def test_standalone_venv_constructor_forms_are_rejected(
    tmp_path: Path,
    selector: str,
) -> None:
    """Every path-construction form is rejected without another selector."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "venv_constructor.py"
    duplicate.write_text(
        "import os\n"
        "from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath\n"
        f"def choose():\n    return {selector}\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct .venv apm fallback" in violations[0]


def test_venv_fallback_used_by_subprocess_is_reported_once(tmp_path: Path) -> None:
    """The selector owns one diagnostic; subprocess options must not duplicate it."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "venv_subprocess.py"
    duplicate.write_text(
        "import subprocess\nfrom pathlib import Path\n"
        "apm_path = Path('.venv') / 'bin' / 'apm'\n"
        "subprocess.run([str(apm_path), '--encoding', 'utf-8'])\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 2
    assert sum("direct .venv apm fallback" in item for item in violations) == 1
    assert sum("direct apm subprocess selection" in item for item in violations) == 1


def test_unrelated_tool_lookup_is_allowed(tmp_path: Path) -> None:
    """The binary boundary must preserve discovery for non-APM tools."""
    _write_owner_stubs(tmp_path)
    consumer = tmp_path / "tests" / "integration" / "tools.py"
    consumer.write_text(
        "import shutil\n"
        "def tools():\n"
        "    return shutil.which('az'), shutil.which('uv'), shutil.which('git')\n",
        encoding="utf-8",
    )

    assert _load_checker().find_binary_selection_violations(tmp_path) == []


def test_interpreter_relative_apm_selector_is_rejected(tmp_path: Path) -> None:
    """Deleting the interpreter-sibling detector must fail independently."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "sibling_duplicate.py"
    duplicate.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "def choose():\n"
        "    return Path(sys.executable).with_name('apm')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "interpreter-relative apm selection" in violations[0]


def test_interpreter_parent_apm_selector_is_rejected(tmp_path: Path) -> None:
    """Parent-directory construction from sys.executable is still selection."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "parent_duplicate.py"
    duplicate.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "def choose():\n"
        "    return Path(sys.executable).parent / 'apm'\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "interpreter-relative apm selection" in violations[0]


@pytest.mark.parametrize(
    "source",
    (
        "import subprocess\nsubprocess.run(['apm', '--version'])\n",
        "import subprocess\nsubprocess.Popen(['apm', '--version'])\n",
        "import subprocess\nsubprocess.run('apm --version', shell=True)\n",
        "import subprocess\ncommand = 'apm --version'\nsubprocess.run(command, shell=True)\n",
        "import subprocess\ncommand = ['apm', '--version']\nsubprocess.run(command)\n",
        "import subprocess\ncommand = ['apm', '--version']\nsubprocess.Popen(command)\n",
        "import subprocess\nsubprocess.run(['uv', 'run', 'apm'])\n",
        "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'apm_cli'])\n",
        "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'uv', 'run', 'apm'])\n",
    ),
)
def test_direct_subprocess_selection_is_rejected(
    tmp_path: Path,
    source: str,
) -> None:
    """Each direct launcher syntax independently requires the fixture."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "launcher_duplicate.py"
    duplicate.write_text(source, encoding="utf-8")

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct apm subprocess selection" in violations[0]


def test_shadowed_subprocess_binding_does_not_duplicate_diagnostics(
    tmp_path: Path,
) -> None:
    """Only the genuine imported subprocess binding may launch APM."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "shadowed_subprocess.py"
    duplicate.write_text(
        "import subprocess\n"
        "outer = subprocess.run(['apm', '--version'])\n"
        "def parameter_shadow(subprocess):\n"
        "    return subprocess.run(['apm', '--version'])\n"
        "def local_shadow():\n"
        "    subprocess = object()\n"
        "    return subprocess.run(['apm', '--version'])\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct apm subprocess selection" in violations[0]


@pytest.mark.parametrize(
    "source",
    (
        "import shutil\n"
        "def choose(flag):\n"
        "    if flag:\n"
        "        locate = shutil.which\n"
        "    else:\n"
        "        locate = shutil.which\n"
        "    return locate('apm')\n",
        "import os\n"
        "def choose(items):\n"
        "    for _ in items:\n"
        "        env = os.environ\n"
        "    return env.get('APM_BINARY_PATH')\n",
        "import subprocess\n"
        "def choose():\n"
        "    try:\n"
        "        runner = subprocess\n"
        "    finally:\n"
        "        pass\n"
        "    return runner.run(['apm', '--version'])\n",
    ),
)
def test_nested_control_flow_aliases_are_rejected(
    tmp_path: Path,
    source: str,
) -> None:
    """Aliases assigned inside compound statements retain their authority."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "nested_alias.py"
    duplicate.write_text(source, encoding="utf-8")

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1


def test_command_assignment_does_not_leak_between_scopes(tmp_path: Path) -> None:
    """Common command variable names remain isolated per function."""
    _write_owner_stubs(tmp_path)
    consumer = tmp_path / "tests" / "integration" / "commands.py"
    consumer.write_text(
        "import subprocess\n"
        "def unrelated_tool():\n"
        "    command = ['ls', '-la']\n"
        "    return subprocess.run(command)\n"
        "def apm_data_only():\n"
        "    command = ['apm', '--version']\n"
        "    return command\n",
        encoding="utf-8",
    )

    assert _load_checker().find_binary_selection_violations(tmp_path) == []


@pytest.mark.parametrize(
    "source",
    (
        "import subprocess\n"
        "command = ['apm', '--version']\n"
        "def run_it():\n"
        "    return subprocess.run(command)\n",
        "import subprocess\n"
        "def outer():\n"
        "    command = ['apm', '--version']\n"
        "    def inner():\n"
        "        return subprocess.run(command)\n"
        "    return inner()\n",
        "import subprocess\n"
        "command = 'apm --version'\n"
        "def run_it():\n"
        "    return subprocess.run(command, shell=True)\n",
    ),
)
def test_command_assignment_inherits_into_nested_scopes(
    tmp_path: Path,
    source: str,
) -> None:
    """Module and closure command values remain visible to child scopes."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "inherited_command.py"
    duplicate.write_text(source, encoding="utf-8")

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct apm subprocess selection" in violations[0]


def test_probe_list_selector_is_rejected(tmp_path: Path) -> None:
    """Probe-loop selection must use the canonical fixture."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "probe_duplicate.py"
    duplicate.write_text(
        "import subprocess\n"
        "def probe():\n"
        "    possible = ['apm', './apm', './dist/apm']\n"
        "    for path in possible:\n"
        "        result = subprocess.run([path, '--version'])\n"
        "        if result.returncode == 0:\n"
        "            return path\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "direct apm subprocess selection" in violations[0]


def test_fixture_forwarding_facade_is_rejected(tmp_path: Path) -> None:
    """Consumers must inject the canonical fixture without another fixture."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "facade.py"
    duplicate.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def binary(apm_binary_path):\n"
        "    return str(apm_binary_path)\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "local apm binary fixture or facade" in violations[0]


def test_multistatement_forwarding_fixture_is_rejected(tmp_path: Path) -> None:
    """Extra statements cannot disguise a duplicate forwarding fixture."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "facade.py"
    duplicate.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def binary(apm_binary_path):\n"
        "    selected = str(apm_binary_path)\n"
        "    return selected\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "local apm binary fixture or facade" in violations[0]


def test_nested_binary_owner_definitions_are_rejected(tmp_path: Path) -> None:
    """Resolver and fixture ownership cannot hide in nested scopes."""
    _write_owner_stubs(tmp_path)
    duplicate = tmp_path / "tests" / "integration" / "nested.py"
    duplicate.write_text(
        "import pytest\n"
        "def outer():\n"
        "    def _resolve_apm_binary():\n"
        "        return None\n"
        "    @pytest.fixture\n"
        "    def apm_binary_path():\n"
        "        return None\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert any("local apm binary fixture or facade" in item for item in violations)
    assert len(violations) >= 2


def test_nested_integration_files_are_scanned_recursively(tmp_path: Path) -> None:
    """Replacing rglob with top-level glob must miss this violation and fail."""
    _write_owner_stubs(tmp_path)
    nested = tmp_path / "tests" / "integration" / "nested" / "consumer.py"
    nested.parent.mkdir()
    nested.write_text(
        "import os\ndef choose():\n    return os.getenv('APM_BINARY_PATH')\n",
        encoding="utf-8",
    )

    violations = _load_checker().find_binary_selection_violations(tmp_path)

    assert len(violations) == 1
    assert "nested/consumer.py" in violations[0]
    assert "direct APM_BINARY_PATH read" in violations[0]


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
