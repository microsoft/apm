"""A refused ``apm uninstall`` keeps apm.lock.yaml truthful (#3656).

When uninstall stops because a managed file was edited or a user file sits
inside a managed skill directory, the files it already removed must leave
the lockfile too: every path the lock still claims is on disk, apm.yml keeps
the package, and a retry after resolving the listed files completes.
"""

from __future__ import annotations

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._lockfile_chain_kit import (
    build_kit,
    evict_machine_state,
    output,
    tree,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_USER_FILE = ".claude/skills/alpha/mine.md"
_EDITED = ".github/instructions/style.instructions.md"


def _lock_claims(project) -> set[str]:
    lock = load_yaml(project / "apm.lock.yaml")
    claims = {
        path for dep in lock.get("dependencies") or [] for path in dep.get("deployed_files") or []
    }
    claims.update(row["value"] for row in lock.get("deployments") or [])
    return claims


@pytest.mark.parametrize("receipts", ["warm", "apm-home-wiped"])
def test_refused_uninstall_lock_claims_only_files_on_disk(
    tmp_path, apm_binary_path, receipts
) -> None:
    kit = build_kit(tmp_path, apm_binary_path)
    project = kit.project
    if receipts == "apm-home-wiped":
        evict_machine_state(kit, apm_home=True)
    (project / _USER_FILE).write_bytes(b"Mine.\n")
    (project / _EDITED).write_bytes(b"Edited.\n")
    before = _lock_claims(project)

    refused = kit.run("uninstall", "test/kit")

    text = output(refused)
    assert refused.returncode != 0, text
    assert (project / _USER_FILE).read_bytes() == b"Mine.\n"
    assert (project / _EDITED).read_bytes() == b"Edited.\n"
    assert "test/kit" in str(load_yaml(project / "apm.yml")["dependencies"]["apm"])
    claims = _lock_claims(project)
    assert claims, "the retained files must stay claimed"
    assert claims < before, "removed files must leave the lockfile"
    missing = sorted(path for path in claims if not (project / path).exists())
    assert missing == [], f"lockfile claims removed files: {missing}"
    assert _EDITED in claims
    assert "records only the files still on disk" in text, text
    assert f"[!] Retained user-edited file {_EDITED}" in text, text

    (project / _USER_FILE).unlink()
    (project / _EDITED).unlink()
    retried = kit.run("uninstall", "test/kit")
    assert retried.returncode == 0, output(retried)
    assert not load_yaml(project / "apm.yml")["dependencies"]["apm"]
    leftovers = [path for path in tree(project) if path.startswith((".claude/", ".agents/"))]
    assert leftovers == [], leftovers
    lock_path = project / "apm.lock.yaml"
    assert not lock_path.exists() or not _lock_claims(project)
