"""Regression traps for per-dependency hook target selection."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.utils.diagnostics import DiagnosticCollector


class _NoopSkillIntegrator:
    def integrate_package_skill(self, package_info, project_root: Path, **kwargs):
        return SimpleNamespace(
            target_paths=[],
            skill_created=False,
            sub_skills_promoted=0,
            bin_deployed=0,
            bin_skipped_reason="",
        )


def _hook_only_target(name: str):
    target = KNOWN_TARGETS[name]
    return replace(target, primitives={"hooks": target.primitives["hooks"]})


def _package_info(package_path: Path) -> PackageInfo:
    return PackageInfo(
        package=APMPackage(name="codex-only-hooks", version="1.0.0"),
        install_path=package_path,
    )


def test_codex_only_dep_does_not_leak_hooks_to_claude(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    package_path = tmp_path / "pkg"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [{"type": "command", "command": "echo codex"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = integrate_package_primitives(
        _package_info(package_path),
        project,
        targets=[_hook_only_target("claude"), _hook_only_target("codex")],
        integrators=IntegratorBundle(
            prompt=None,
            agent=None,
            skill=_NoopSkillIntegrator(),
            instruction=None,
            command=None,
            hook=HookIntegrator(),
            canvas=None,
        ),
        force=False,
        managed_files=set(),
        diagnostics=DiagnosticCollector(),
        package_name="codex-only-hooks",
        dep_target_subset=["codex"],
    )

    assert result["hooks"] == 1
    assert not (project / ".claude" / "settings.json").exists()
    codex = json.loads((project / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert set(codex["hooks"]) == {"SessionStart"}
