"""Regenerate the T1 upgrade fixtures from a released APM build.

Usage (from the repository root, one version at a time)::

    PYTHONPATH=. python tests/fixtures/upgrade_0361/t1/generate.py <apm-executable> <version-label>

The executable is the released CLI (for 0.35.1 a wrapper running the
``v0.35.1`` tag sources; for 0.36.0 the ``2769573f40`` release build). Each
run installs, against hermetic fixtures, one Azure DevOps dependency
(``ADO_APM_PAT``) and one registry dependency, then copies the produced
``apm.yml`` / ``apm.lock.yaml`` into ``<version-label>/{ado,registry}/``.
The fixture repositories and the registry archive are deterministic, so
``test_auth_upgrade_lock_replay_lifecycle.py`` can recreate the same
sources and replay these locks.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from tests.integration._ado_auth_kit import INSTALL, AdoAuthScenario, ado_dependency_entry, manifest
from tests.utils.local_scripted_https_proxy import basic_authorization

HERE = Path(__file__).resolve().parent
PAT = "upgrade-fixture-pat-sentinel-0d1e"


def _generate_ado(binary: Path, out: Path, work: Path) -> None:
    scenario = AdoAuthScenario(work / "ado", binary)
    scenario.ado.accepted_authorizations.add(basic_authorization("", PAT))
    project = scenario.project(manifest=manifest(ado_dependency_entry()))
    with scenario.serve() as proxy:
        result = scenario.run(
            INSTALL, cwd=project, env=scenario.env(proxy, {"ADO_APM_PAT": PAT}), step="gen-ado"
        )
    if result.returncode != 0:
        raise SystemExit(result.stdout + result.stderr)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("apm.yml", "apm.lock.yaml"):
        shutil.copyfile(project / name, out / name)


def _generate_registry(binary: Path, out: Path, work: Path) -> None:
    from tests.integration.test_auth_upgrade_lock_replay_lifecycle import RegistryUpgradeScenario

    scenario = RegistryUpgradeScenario(work / "registry", binary)
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.enable(env)
        result = scenario.run(env, "install", "--target", "copilot", "--no-policy", step="gen")
    if result.returncode != 0:
        raise SystemExit(result.stdout + result.stderr)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("apm.yml", "apm.lock.yaml"):
        shutil.copyfile(scenario.project / name, out / name)


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    label = sys.argv[2]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        _generate_ado(binary, HERE / label / "ado", work)
        _generate_registry(binary, HERE / label / "registry", work)


if __name__ == "__main__":
    main()
