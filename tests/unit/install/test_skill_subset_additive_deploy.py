"""Additive skill-subset deployment into a project tree."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.install.deployment_plan import prepare_package_deployment
from apm_cli.install.services import integrate_package_primitives

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Unit: the additive-union helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unit: the CLI-subset normalizer + skill-pin applier
# ---------------------------------------------------------------------------


class TestEffectiveDependencySelection:
    """The single deploy-selection owner shared by install, audit and uninstall (#3760)."""

    @pytest.mark.parametrize(
        ("deploy", "extra"),
        [
            (
                integrate_package_primitives,
                {"integrators": MagicMock(), "force": False, "managed_files": None},
            ),
            (prepare_package_deployment, {}),
        ],
        ids=["integrate_package_primitives", "prepare_package_deployment"],
    )
    def test_deploy_without_skill_subset_is_a_type_error(self, deploy, extra, tmp_path):
        with pytest.raises(
            TypeError, match=r"missing 1 required keyword-only argument: 'skill_subset'"
        ):
            deploy(MagicMock(), tmp_path, targets=[], diagnostics=None, **extra)


# ---------------------------------------------------------------------------
# E2E fixtures: a local directory skill bundle + a consumer project
# ---------------------------------------------------------------------------


def _make_skill_bundle(base: Path, skills: tuple[str, ...]) -> Path:
    bundle = base / "azure-skills"
    for name in skills:
        skill_dir = bundle / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_bytes(
            (
                f"---\nname: {name}\ndescription: Demo skill {name} for additive "
                f"deploy parity.\n---\n# {name}\nBody for {name}.\n"
            ).encode()
        )
    (bundle / "apm.yml").write_bytes(
        yaml.dump({"name": "azure-skills", "version": "1.0.0"}).encode()
    )
    return bundle


def _make_project(base: Path) -> Path:
    project = base / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_bytes(yaml.dump({"name": "consumer", "version": "1.0.0"}).encode())
    return project


def _install(project: Path, monkeypatch, *args: str):
    from apm_cli.cli import cli
    from apm_cli.models.apm_package import clear_apm_yml_cache

    # Each real `apm` command runs in a fresh process with an empty parse
    # cache. CliRunner reuses this process, so clear the module-level
    # apm.yml cache to mirror production isolation (see clear_apm_yml_cache
    # docstring -- "Call in tests for isolation").
    clear_apm_yml_cache()
    monkeypatch.chdir(project)
    return CliRunner().invoke(cli, ["install", *args], catch_exceptions=False)


def _deployed_skill_names(project: Path) -> set[str]:
    skills_root = project / ".agents" / "skills"
    if not skills_root.is_dir():
        return set()
    return {p.name for p in skills_root.iterdir() if (p / "SKILL.md").exists()}


def _lock_skill_subset(project: Path) -> list[str]:
    lock = project / "apm.lock.yaml"
    if not lock.exists():
        return []
    data = yaml.safe_load(lock.read_text()) or {}
    deps = data.get("dependencies", [])
    # Lockfile serializes dependencies as a list of entries.
    entries = deps.values() if isinstance(deps, dict) else deps
    for dep in entries:
        if isinstance(dep, dict) and dep.get("package_type") == "skill_bundle":
            return sorted(dep.get("skill_subset") or [])
    return []


# ---------------------------------------------------------------------------
# E2E: the core additive-deployment regression
# ---------------------------------------------------------------------------


class TestAdditiveSkillDeployment:
    def test_second_skill_install_deploys_on_top_without_erasing_first(self, tmp_path, monkeypatch):
        bundle = _make_skill_bundle(tmp_path / "src", ("cosmos-db", "functions", "aks"))
        project = _make_project(tmp_path / "dst")

        r1 = _install(
            project, monkeypatch, str(bundle), "--skill", "cosmos-db", "--target", "agent-skills"
        )
        assert r1.exit_code == 0, r1.output
        assert _deployed_skill_names(project) == {"cosmos-db"}

        r2 = _install(
            project, monkeypatch, str(bundle), "--skill", "functions", "--target", "agent-skills"
        )
        assert r2.exit_code == 0, r2.output

        # THE FIX: functions deployed ON TOP of cosmos-db, not replacing it.
        assert _deployed_skill_names(project) == {"cosmos-db", "functions"}

        # apm.yml persists the union (pre-existing #1786 behavior).
        manifest = yaml.safe_load((project / "apm.yml").read_text())
        entry = manifest["dependencies"]["apm"][0]
        assert sorted(entry["skills"]) == ["cosmos-db", "functions"]

        # Lockfile reflects the union for both skill_subset and deployed_files.
        assert _lock_skill_subset(project) == ["cosmos-db", "functions"]

    def test_bare_reinstall_keeps_full_union(self, tmp_path, monkeypatch):
        bundle = _make_skill_bundle(tmp_path / "src", ("cosmos-db", "functions"))
        project = _make_project(tmp_path / "dst")
        _install(
            project, monkeypatch, str(bundle), "--skill", "cosmos-db", "--target", "agent-skills"
        )
        _install(
            project, monkeypatch, str(bundle), "--skill", "functions", "--target", "agent-skills"
        )

        r = _install(project, monkeypatch, "--target", "agent-skills")
        assert r.exit_code == 0, r.output
        assert _deployed_skill_names(project) == {"cosmos-db", "functions"}

    def test_star_resets_to_all_skills(self, tmp_path, monkeypatch):
        bundle = _make_skill_bundle(tmp_path / "src", ("cosmos-db", "functions", "aks"))
        project = _make_project(tmp_path / "dst")
        _install(
            project, monkeypatch, str(bundle), "--skill", "cosmos-db", "--target", "agent-skills"
        )

        r = _install(project, monkeypatch, str(bundle), "--skill", "*", "--target", "agent-skills")
        assert r.exit_code == 0, r.output
        # '*' deploys the full bundle.
        assert _deployed_skill_names(project) == {"cosmos-db", "functions", "aks"}
        # Manifest reverts to the full bundle (no skills: pin).
        manifest = yaml.safe_load((project / "apm.yml").read_text())
        entry = manifest["dependencies"]["apm"][0]
        skills = entry.get("skills") if isinstance(entry, dict) else None
        assert not skills

    def test_manifest_reduction_cleans_dropped_skill(self, tmp_path, monkeypatch):
        bundle = _make_skill_bundle(tmp_path / "src", ("cosmos-db", "functions"))
        project = _make_project(tmp_path / "dst")
        _install(
            project, monkeypatch, str(bundle), "--skill", "cosmos-db", "--target", "agent-skills"
        )
        _install(
            project, monkeypatch, str(bundle), "--skill", "functions", "--target", "agent-skills"
        )
        assert _deployed_skill_names(project) == {"cosmos-db", "functions"}

        # Manually reduce the manifest to a single skill, then bare reinstall.
        manifest_path = project / "apm.yml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["dependencies"]["apm"][0]["skills"] = ["cosmos-db"]
        manifest_path.write_bytes(yaml.dump(manifest).encode())

        r = _install(project, monkeypatch, "--target", "agent-skills")
        assert r.exit_code == 0, r.output
        assert _deployed_skill_names(project) == {"cosmos-db"}

    def test_uninstall_removes_full_union(self, tmp_path, monkeypatch):
        from apm_cli.cli import cli

        bundle = _make_skill_bundle(tmp_path / "src", ("cosmos-db", "functions"))
        project = _make_project(tmp_path / "dst")
        _install(
            project, monkeypatch, str(bundle), "--skill", "cosmos-db", "--target", "agent-skills"
        )
        _install(
            project, monkeypatch, str(bundle), "--skill", "functions", "--target", "agent-skills"
        )
        assert _deployed_skill_names(project) == {"cosmos-db", "functions"}

        monkeypatch.chdir(project)
        # Local-path bundles are addressed by their path in apm.yml/uninstall.
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        r = CliRunner().invoke(cli, ["uninstall", str(bundle)], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        # Whole bundle removed from disk -- deployed_files held the full union.
        assert _deployed_skill_names(project) == set()
