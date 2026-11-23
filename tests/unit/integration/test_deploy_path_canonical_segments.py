"""The deploy-path gate admits only canonical paths: no "." segment (#5096).

PROMISE: P-INV-I5 -- every path APM deploys or removes is checked in canonical
form: a "." segment in the leading, middle or trailing position is refused for
every target, like "..", and no path APM records carries one (source: #5096
scope record, path canonicalization hardening).
owner: integration/base_integrator.py BaseIntegrator.validate_deploy_path
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.yaml_io import load_yaml

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I5")]

_REPO = Path(__file__).resolve().parents[3]
_PREFIXES = sorted(
    {target.prefix for target in KNOWN_TARGETS.values() if target.user_root_resolver is None}
)


@pytest.mark.parametrize("prefix", _PREFIXES)
def test_every_target_refuses_a_dot_segment_in_any_position(prefix: str, tmp_path: Path) -> None:
    canonical = f"{prefix}demo/file.md"
    assert BaseIntegrator.validate_deploy_path(canonical, tmp_path)

    for dotted in (f"./{canonical}", f"{prefix}./demo/file.md", f"{prefix}demo/.", f"{prefix}."):
        assert not BaseIntegrator.validate_deploy_path(dotted, tmp_path), dotted


def test_a_dot_segment_is_refused_in_windows_spelling(tmp_path: Path) -> None:
    assert not BaseIntegrator.validate_deploy_path(".github\\.\\prompts\\p.md", tmp_path)


_RECORDED_KEYS = frozenset(
    {"deployed_files", "local_deployed_files", "deployed_file_hashes", "local_deployed_file_hashes"}
)


def _recorded_paths(node: object) -> list[str]:
    """Every path a lockfile records: deployed_files lists and hash-map keys."""
    paths: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _RECORDED_KEYS and isinstance(value, (list, dict)):
                paths.extend(str(item) for item in value)
            else:
                paths.extend(_recorded_paths(value))
    elif isinstance(node, list):
        for item in node:
            paths.extend(_recorded_paths(item))
    return paths


def test_no_recorded_path_in_the_lockfile_corpus_has_a_dot_segment() -> None:
    locks = sorted((_REPO / "tests").rglob("apm.lock.yaml"))
    recorded = [(lock, path) for lock in locks for path in _recorded_paths(load_yaml(lock))]

    assert len(locks) >= 10 and recorded, locks
    dotted = [
        (lock.relative_to(_REPO).as_posix(), path)
        for lock, path in recorded
        if "." in path.replace("\\", "/").split("/")
    ]
    assert dotted == []
