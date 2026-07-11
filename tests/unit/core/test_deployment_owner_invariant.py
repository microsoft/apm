"""Source invariant for canonical deployment-state mutation."""

import re
from pathlib import Path

_ASSIGNMENT = re.compile(
    r"\.(?:deployed_files|deployed_file_hashes|local_deployed_files|"
    r"local_deployed_file_hashes|mcp_target_servers)\s*="
)
_ALLOWED = {
    Path("core/deployment_state.py"),
    Path("core/deployment_ledger.py"),
    Path("deps/lockfile.py"),
}


def test_only_canonical_owner_assigns_legacy_deployment_fields() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "apm_cli"
    violations: list[str] = []
    for source in root.rglob("*.py"):
        relative = source.relative_to(root)
        if relative in _ALLOWED:
            continue
        for line_number, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if _ASSIGNMENT.search(line):
                violations.append(f"{relative.as_posix()}:{line_number}")
    assert violations == []
