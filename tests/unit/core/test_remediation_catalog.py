"""Every remediation APM can advise has exactly one executed proof (#4090)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from apm_cli.core.remediation import (
    LOCKFILE_MISSING,
    RESTORE_EDITED_FORCE,
    TRUST_PACKAGE,
    catalog,
    either,
    restore_edited,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_PROOF_MODULES = (
    "tests/integration/test_remediation_advice_lifecycle.py",
    *sorted(
        path.relative_to(_ROOT).as_posix()
        for path in (_ROOT / "tests/integration").glob("test_remediation_proofs_*.py")
    ),
)


def _proves(path: str) -> tuple[str, ...]:
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"), filename=path)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and [getattr(target, "id", None) for target in node.targets] == ["PROVES"]
            and isinstance(node.value, ast.Tuple)
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"{path} declares no PROVES tuple")


def test_every_catalog_entry_has_exactly_one_executed_proof() -> None:
    proven = [entry for path in _PROOF_MODULES for entry in _proves(path)]
    duplicates = sorted({entry for entry in proven if proven.count(entry) > 1})
    assert not duplicates, f"proven more than once: {duplicates}"
    assert set(proven) == set(catalog()), {
        "unproven": sorted(set(catalog()) - set(proven)),
        "unknown": sorted(set(proven) - set(catalog())),
    }


def test_every_entry_names_the_state_it_clears_and_runs_apm() -> None:
    for remediation in catalog().values():
        assert remediation.clears, remediation.id
        assert remediation.command.startswith("apm "), remediation.id
        assert remediation.command.isascii() and remediation.fixes_promise.isascii()


def test_rendering_and_binding() -> None:
    assert LOCKFILE_MISSING.sentence() == "Run 'apm install' to generate apm.lock.yaml."
    bound = TRUST_PACKAGE.bind(package="org/pkg")
    assert bound.argv() == ("apm", "approve", "org/pkg")
    assert bound.to_json()["command"] == "apm approve org/pkg"
    with pytest.raises(ValueError, match="unbound placeholders"):
        TRUST_PACKAGE.clause()
    restore = restore_edited("the modified files")
    assert either(restore, RESTORE_EDITED_FORCE).startswith(
        "delete the modified files, then run 'apm install' to restore the locked content"
    )
