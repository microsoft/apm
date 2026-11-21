"""Every third-party import in ``src/apm_cli`` resolves to a declared runtime dependency.

Tests run with the ``dev`` extra installed, so an import of a package that only
arrives through dev or build tooling (``packaging`` in #3908) passes the suite
and then fails a plain ``pip install apm-cli``. This scan reads the source
statically, so it holds regardless of what the test environment has installed.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPO_ROOT / "src" / "apm_cli"

# Import root -> declared distribution in ``[project].dependencies``.
_DECLARED_IMPORT_ROOTS = {
    "click": "click",
    "colorama": "colorama",
    "filelock": "filelock",
    "frontmatter": "python-frontmatter",
    "git": "gitpython",
    "llm": "llm",
    "llm_github_models": "llm-github-models",
    "requests": "requests",
    "rich": "rich",
    "rich_click": "rich-click",
    "ruamel": "ruamel.yaml",
    "toml": "toml",
    "tomlkit": "tomlkit",
    "truststore": "truststore",
    "watchdog": "watchdog",
    "websockets": "websockets",
    "yaml": "pyyaml",
}

# Import roots provided by a declared dependency's own hard requirements.
_TRANSITIVE_IMPORT_ROOTS = {
    "certifi": "requests",
    "urllib3": "requests",
    # Python < 3.8 fallback in version.py; unreachable under requires-python >= 3.11.
    "importlib_metadata": None,
}


def _declared_distributions() -> set[str]:
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = set()
    for requirement in project["project"]["dependencies"]:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        assert match, requirement
        names.add(match.group(0).lower())
    return names


def _third_party_import_roots() -> dict[str, set[str]]:
    roots: dict[str, set[str]] = {}
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                root = module.split(".")[0]
                if root == "apm_cli" or root in sys.stdlib_module_names:
                    continue
                rel = path.relative_to(_REPO_ROOT).as_posix()
                roots.setdefault(root, set()).add(rel)
    return roots


def test_every_third_party_import_is_a_declared_runtime_dependency() -> None:
    declared = _declared_distributions()
    undeclared = {
        root: sorted(files)
        for root, files in _third_party_import_roots().items()
        if root not in _TRANSITIVE_IMPORT_ROOTS
        and _DECLARED_IMPORT_ROOTS.get(root, "").lower() not in declared
    }
    assert not undeclared, (
        "src/apm_cli imports packages that a plain 'pip install apm-cli' does not "
        f"install: {undeclared}. Declare them in [project].dependencies (and map "
        "them here) or use the standard library."
    )


@pytest.mark.parametrize("root", sorted(r for r, d in _TRANSITIVE_IMPORT_ROOTS.items() if d))
def test_transitive_import_roots_come_from_a_declared_dependency(root: str) -> None:
    assert _TRANSITIVE_IMPORT_ROOTS[root].lower() in _declared_distributions()
