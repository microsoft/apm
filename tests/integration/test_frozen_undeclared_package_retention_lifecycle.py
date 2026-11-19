"""``apm install --frozen`` keeps packages apm.yml no longer declares (#3662).

Frozen never rewrites apm.lock.yaml, so it must not delete what that lock
still claims: after a dependency is dropped from apm.yml, a frozen install
exits 0, leaves every file, the module checkout and the lockfile byte for
byte, and names the kept package. A normal install then removes it and the
audit is clean.
"""

from __future__ import annotations

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._lockfile_chain_kit import build_kit, output, tree

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_KEPT = (
    "Kept packages no longer in apm.yml because --frozen does not change apm.lock.yaml: test/kit"
)


def test_frozen_keeps_undeclared_package_until_normal_install(tmp_path, apm_binary_path) -> None:
    kit = build_kit(tmp_path, apm_binary_path)
    project = kit.project
    manifest = load_yaml(project / "apm.yml")
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, project / "apm.yml")
    before = tree(project)
    deployed = [path for path in before if path.startswith((".claude/", ".agents/", ".github/"))]
    assert deployed

    for _ in range(2):
        frozen = kit.run("install", "--no-policy", "--frozen")
        text = output(frozen)
        assert frozen.returncode == 0, text
        assert _KEPT in text and "Run 'apm install' to remove them." in text, text
        assert tree(project) == before
        assert (project / "apm_modules/test/kit").is_dir()

    installed = kit.run("install", "--no-policy")
    assert installed.returncode == 0, output(installed)
    after = tree(project)
    assert not [path for path in deployed if path in after]
    lock_path = project / "apm.lock.yaml"
    if lock_path.exists():
        assert not (load_yaml(lock_path).get("dependencies") or [])

    audit = kit.run("audit", "--ci", "--no-policy")
    assert audit.returncode == 0, output(audit)
