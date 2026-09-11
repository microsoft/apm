"""Installed apmx acceptance with real files/checks and a hermetic native actor.

This is NOT live inference or frozen-distribution certification. Only Copilot's
external protocol is faked; selection, private local-skill preparation, capture,
checks, reduction, and retained evidence run through production subprocesses.
The remote test additionally replaces the canonical downloader transport.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(os.name != "posix", reason="Native-advisory execution is POSIX-only."),
]

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples/contracts/packaged-job"
_CONTRACT = "contracts/handoff.contract.md"
_CAPTURED_SOURCE = "_apmx_source/contract.contract.md"
_ACTOR = r"""
import json
import os
import re
import sys
from pathlib import Path

log = Path(os.environ["APMX_ACTOR_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:], "cwd": str(Path.cwd())}) + "\n")
if sys.argv[-3:] == ["mcp", "list", "--json"]:
    print(json.dumps({"mcpServers": {}}))
    raise SystemExit(0)
if "-p" not in sys.argv:
    raise SystemExit("unexpected hermetic Copilot invocation")
mode = os.environ.get("APMX_ACTOR_MODE", "valid")
if mode != "missing":
    ids = re.findall(r"^- ([a-z][a-z0-9_-]*): ", Path("notes.md").read_text(), re.MULTILINE)
    candidate = [
        {"source_id": name, "summary": "Fixture summary", "caution": "Check first: fixture"}
        for name in ids
    ]
    if mode == "invalid":
        candidate[0]["caution"] = "wrong style"
    Path("handoff.json").write_text(json.dumps(candidate) + "\n")
if mode == "poison-checker":
    Path("checks/check_handoff.py").write_text("raise SystemExit(1)\n")
print(json.dumps({"type": "assistant.message", "data": {
    "messageId": "hermetic", "content": "Hermetic protocol fixture, not inference",
    "model": "fixture-model"
}}), flush=True)
print(json.dumps({"type": "result", "exitCode": 0, "sessionId": "hermetic", "usage": {}}),
      flush=True)
"""


@dataclass
class PackagedJob:
    """One isolated caller/source pair and its installed console entrypoint."""

    isolation: IsolatedApmEnvironment
    caller: Path
    package: Path
    executable: Path
    env: dict[str, str]
    actor_log: Path

    def run(self, *args: str, consent: bool = True) -> subprocess.CompletedProcess[str]:
        """Launch actual installed argv with piped stdin/stdout and a hard bound."""
        command = [str(self.executable), *args, "--on", "copilot", "--model", "fixture-model"]
        if consent:
            command.append("--allow-host-access")
        return subprocess.run(
            command,
            cwd=self.caller,
            env=self.env,
            input="",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def packaged(self, *args: str, consent: bool = True) -> subprocess.CompletedProcess[str]:
        """Select the explicit package-relative contract, never a guessed job."""
        return self.run("--from", str(self.package), _CONTRACT, *args, consent=consent)

    def record(self) -> tuple[Path, dict[str, Any]]:
        """Read the sole completed run, failing rather than accepting no evidence."""
        records = list((self.caller / ".apm/runs").glob("*/record.json"))
        assert len(records) == 1, records
        record = json.loads(records[0].read_bytes())
        assert record["complete"] is True
        return records[0].parent, record

    def startup_hook(self, code: str) -> None:
        """Instrument only this test's installed Python launcher, not production."""
        guard = self.isolation.root / "network_guard/sitecustomize.py"
        with guard.open("a", encoding="utf-8") as stream:
            stream.write("\nif os.path.basename(sys.argv[0]) == 'apmx':\n")
            stream.write("    exec(" + repr(code) + ")\n")


@pytest.fixture
def job(tmp_path: Path) -> PackagedJob:
    """Reuse the hermetic HOME/network guard; never use ambient native credentials."""
    isolation = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    (isolation.config_root / "config.json").write_text(
        '{"experimental": {"contracts": true}}\n',
        encoding="utf-8",
    )
    package = isolation.package_root / "job"
    shutil.copytree(_EXAMPLE, package)
    caller = isolation.work_root / "caller"
    caller.mkdir()
    shutil.copyfile(package / "caller/notes.md", caller / "notes.md")
    # Unowned configuration must survive even when private dependency setup runs.
    (caller / ".claude").mkdir()
    (caller / ".claude/settings.json").write_text('{"unowned": true}\n', encoding="utf-8")
    tools = isolation.root / "tools"
    tools.mkdir()
    actor = tools / "copilot"
    actor.write_text(f"#!{sys.executable}\n" + _ACTOR, encoding="utf-8")
    actor.chmod(0o755)
    # The contract's original checker uses python3; bind it to the test interpreter.
    (tools / "python3").symlink_to(sys.executable)
    executable = Path(sysconfig.get_path("scripts")) / "apmx"
    assert executable.is_file(), "Install this checkout's console entrypoints before acceptance."
    env = isolation.subprocess_env(
        overrides={
            "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
            "APMX_ACTOR_LOG": str(isolation.root / "actor.jsonl"),
            "COPILOT_HOME": str(isolation.root / "copilot-profile"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
        }
    )
    env.pop("APM_NO_SCRIPTS", None)
    return PackagedJob(isolation, caller, package, executable, env, Path(env["APMX_ACTOR_LOG"]))


def _without_import(job: PackagedJob) -> None:
    """Reduce to a zero-dependency package using the same original contract grammar."""
    (job.package / "apm.yml").write_text(
        "name: packaged-handoff\nversion: 0.1.0\ndependencies:\n  apm: []\n",
        encoding="utf-8",
    )
    contract = job.package / _CONTRACT
    contract.write_text(
        contract.read_text(encoding="utf-8").replace("imports:\n  - handoff-style\n", ""),
        encoding="utf-8",
    )


def _assert_only_evidence_added(before: ArtifactSnapshot, caller: Path) -> None:
    """Prove no deletion/overwrite and no new setup/output outside retained runs."""
    diff = before.diff(ArtifactSnapshot.capture(caller))
    assert diff.changed == frozenset()
    assert diff.removed == frozenset()
    assert diff.added
    assert all(
        path in {".apm", ".apm/runs"} or path.startswith(".apm/runs/") for path in diff.added
    ), diff.added


def test_packaged_job_resolves_one_skill_and_freezes_caller_source_and_checks(
    job: PackagedJob,
) -> None:
    """Private acquisition is real, and assessment does not trust the producer tree."""
    job.env["APMX_ACTOR_MODE"] = "poison-checker"
    before = ArtifactSnapshot.capture(job.caller)
    source_before = ArtifactSnapshot.capture(job.package)
    home_before = ArtifactSnapshot.capture(job.isolation.home)
    temp_before = ArtifactSnapshot.capture(job.isolation.temp_root)
    result = job.packaged()
    assert result.returncode == 0, result.stdout + result.stderr
    run, record = job.record()
    assert record["profile"] == "native-advisory"
    assert (
        record["source"]["sha256"]
        == hashlib.sha256((job.package / _CONTRACT).read_bytes()).hexdigest()
    )
    assert not Path(record["source"]["package"]["root"]).exists()
    retained = record["source"]["retained"]
    assert (
        Path(retained["contract.contract.md"]).read_bytes()
        == (job.package / _CONTRACT).read_bytes()
    )
    assert Path(retained["apm.yml"]).read_bytes() == (job.package / "apm.yml").read_bytes()
    assert (
        record["lock_sha256"]
        == hashlib.sha256(Path(retained["apm.lock.yaml"]).read_bytes()).hexdigest()
    )
    assert len(record["imports"]) == 1
    imported = record["imports"][0]
    assert imported["name"] == "handoff-style"
    assert (
        imported["sha256"]
        == hashlib.sha256((job.package / "skills/handoff-style/SKILL.md").read_bytes()).hexdigest()
    )
    assert imported["lock_identity"] == "./skills/handoff-style"
    assert (run / "baseline" / _CAPTURED_SOURCE).read_bytes() == (
        job.package / _CONTRACT
    ).read_bytes()
    assert (run / "baseline/notes.md").read_bytes() == (job.caller / "notes.md").read_bytes()
    assert (run / "baseline/notes.md").read_bytes() != (job.package / "notes.md").read_bytes()
    artifact = Path(record["artifact"]["path"])
    assert artifact.is_relative_to(run)
    raw = artifact.read_bytes()
    assert record["artifact"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert {row["source_id"] for row in json.loads(raw)} == {"caller_build", "caller_review"}
    assert record["checks"][0]["normalized"] == 0
    assert record["checks"][0]["subject_digest"] == record["artifact"]["sha256"]
    assert record["checks"][0]["process"]["pid"] is not None
    assessment = next((run / "assessments").iterdir())
    assert (assessment / "handoff.json").read_bytes() == raw
    assert (assessment / "checks/check_handoff.py").read_bytes() == (
        job.package / "checks/check_handoff.py"
    ).read_bytes()
    assert (run / "producer/checks/check_handoff.py").read_bytes() != (
        assessment / "checks/check_handoff.py"
    ).read_bytes()
    calls = [json.loads(line) for line in job.actor_log.read_text().splitlines()]
    producer = next(call for call in calls if "-p" in call["argv"])
    prompt = producer["argv"][producer["argv"].index("-p") + 1]
    assert 'Imported context "handoff-style"' in prompt
    assert "Check first: " in prompt
    _assert_only_evidence_added(before, job.caller)
    assert_unchanged(source_before, ArtifactSnapshot.capture(job.package))
    assert_unchanged(home_before, ArtifactSnapshot.capture(job.isolation.home))
    assert_unchanged(temp_before, ArtifactSnapshot.capture(job.isolation.temp_root))


@pytest.mark.parametrize(
    ("mode", "exit_code", "outcome"),
    [
        ("invalid", 20, "REJECTED"),
        ("missing", 21, "UNPROVEN"),
    ],
)
def test_failed_check_or_missing_artifact_never_claims_success(
    job: PackagedJob,
    mode: str,
    exit_code: int,
    outcome: str,
) -> None:
    """Both unsuccessful result paths retain evidence without source/caller setup writes."""
    job.env["APMX_ACTOR_MODE"] = mode
    if mode == "missing":
        # An earlier caller artifact must not masquerade as this attempt's output.
        (job.caller / "handoff.json").write_text('[{"stale": true}]\n', encoding="utf-8")
    before = ArtifactSnapshot.capture(job.caller)
    source_before = ArtifactSnapshot.capture(job.package)
    result = job.packaged()
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert outcome in result.stdout
    _, record = job.record()
    if mode == "invalid":
        assert record["checks"][0]["normalized"] == 1
        assert Path(record["artifact"]["path"]).is_file()
    else:
        assert record["artifact"] is None
        assert record["result"]["checks"] == []
    _assert_only_evidence_added(before, job.caller)
    assert_unchanged(source_before, ArtifactSnapshot.capture(job.package))


def test_local_contract_without_manifest_uses_installed_entrypoint(job: PackagedJob) -> None:
    """A standalone caller needs no synthetic apm.yml or package setup."""
    _without_import(job)
    shutil.copyfile(job.package / _CONTRACT, job.caller / "local.contract.md")
    shutil.copytree(job.package / "checks", job.caller / "checks")
    before = ArtifactSnapshot.capture(job.caller)
    result = job.run("local.contract.md")
    assert result.returncode == 0, result.stdout + result.stderr
    _, record = job.record()
    assert record["source"]["path"] == str(job.caller / "local.contract.md")
    assert not (job.caller / "apm.yml").exists()
    _assert_only_evidence_added(before, job.caller)


def test_sibling_skill_resolves_from_original_package_not_caller(job: PackagedJob) -> None:
    """Private preparation retains the original parent anchor for relative imports."""
    sibling = job.package.parent / "handoff-style"
    shutil.move(str(job.package / "skills/handoff-style"), sibling)
    manifest = job.package / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("./skills/handoff-style", "../handoff-style"),
        encoding="utf-8",
    )
    decoy = job.caller.parent / "handoff-style"
    decoy.mkdir()
    (decoy / "SKILL.md").write_text("Wrong caller-parent skill, not valid.\n", encoding="utf-8")
    caller_before = ArtifactSnapshot.capture(job.caller)
    source_before = ArtifactSnapshot.capture(job.isolation.package_root)
    decoy_before = ArtifactSnapshot.capture(decoy)
    result = job.packaged()
    assert result.returncode == 0, result.stdout + result.stderr
    _, record = job.record()
    assert len(record["imports"]) == 1
    assert record["imports"][0]["lock_identity"] == "../handoff-style"
    assert (
        record["imports"][0]["sha256"]
        == hashlib.sha256((sibling / "SKILL.md").read_bytes()).hexdigest()
    )
    _assert_only_evidence_added(caller_before, job.caller)
    assert_unchanged(source_before, ArtifactSnapshot.capture(job.isolation.package_root))
    assert_unchanged(decoy_before, ArtifactSnapshot.capture(decoy))


@pytest.mark.parametrize(
    "selection",
    [
        "contracts/missing.contract.md",
        "../outside.contract.md",
        "contracts/linked.contract.md",
    ],
)
def test_missing_traversal_and_symlink_selections_never_infer(
    job: PackagedJob, selection: str
) -> None:
    """An explicit package-relative source is mandatory and cannot escape its root."""
    outside = job.isolation.package_root / "outside.contract.md"
    shutil.copyfile(job.package / _CONTRACT, outside)
    if selection == "contracts/linked.contract.md":
        (job.package / selection).symlink_to(outside)
    before = ArtifactSnapshot.capture(job.caller)
    source_before = ArtifactSnapshot.capture(job.package)
    result = job.run("--from", str(job.package), selection)
    assert result.returncode in {21, 22}, result.stdout + result.stderr
    assert not job.actor_log.exists()
    assert_unchanged(before, ArtifactSnapshot.capture(job.caller))
    assert_unchanged(source_before, ArtifactSnapshot.capture(job.package))


@pytest.mark.parametrize("collision", ["checks/check_handoff.py", _CAPTURED_SOURCE])
def test_caller_collision_is_not_overwritten(job: PackagedJob, collision: str) -> None:
    """Neither package checks nor reserved source capture may replace caller bytes."""
    path = job.caller / collision
    path.parent.mkdir(parents=True)
    path.write_text("unowned collision sentinel\n", encoding="utf-8")
    before = ArtifactSnapshot.capture(job.caller)
    result = job.packaged()
    assert result.returncode in {21, 22}, result.stdout + result.stderr
    assert not job.actor_log.exists()
    assert_unchanged(before, ArtifactSnapshot.capture(job.caller))


def test_caller_policy_cannot_be_bypassed_by_private_package_preparation(job: PackagedJob) -> None:
    """A caller's configured governance still blocks this otherwise runnable package."""
    (job.caller / "apm.yml").write_text(
        "name: governed-caller\nversion: 0.1.0\npolicy:\n  hash: wrong\n",
        encoding="utf-8",
    )
    before = ArtifactSnapshot.capture(job.isolation.root)
    result = job.packaged()
    assert result.returncode == 21, result.stdout + result.stderr
    assert "policy" in (result.stdout + result.stderr).lower()
    assert not job.actor_log.exists()
    assert_unchanged(before, ArtifactSnapshot.capture(job.isolation.root))


def test_piped_execution_without_consent_never_infers_or_writes(job: PackagedJob) -> None:
    """Piped stdin is not implicit acceptance of native-advisory controls."""
    before = ArtifactSnapshot.capture(job.isolation.root)
    result = job.packaged(consent=False)
    assert result.returncode == 21, result.stdout + result.stderr
    assert "--allow-host-access" in result.stdout + result.stderr
    assert "available login details" in result.stdout
    assert "***" not in result.stdout
    assert not job.actor_log.exists()
    assert_unchanged(before, ArtifactSnapshot.capture(job.isolation.root))


def test_local_plan_is_offline_and_has_no_filesystem_effects(job: PackagedJob) -> None:
    """An executable local zero-import source previews without runtime/network probes."""
    _without_import(job)
    before = ArtifactSnapshot.capture(job.isolation.root)
    result = job.packaged("--plan", consent=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VERIFIED" not in result.stdout
    assert not job.actor_log.exists()
    assert_unchanged(before, ArtifactSnapshot.capture(job.isolation.root))


def test_unresolved_remote_plan_never_calls_downloader(job: PackagedJob) -> None:
    """Offline planning does not turn an unresolved Git reference into a fetch."""
    job.startup_hook("""
from apm_cli.deps.github_downloader import GitHubPackageDownloader
def forbidden(*args, **kwargs):
    raise AssertionError("offline plan called downloader")
GitHubPackageDownloader.__init__ = forbidden
""")
    before = ArtifactSnapshot.capture(job.isolation.root)
    result = job.run("--from", "example/packaged-job#v1", _CONTRACT, "--plan", consent=False)
    assert result.returncode == 21, result.stdout + result.stderr
    assert "unresolved" in (result.stdout + result.stderr).lower()
    assert "offline plan called downloader" not in result.stdout + result.stderr
    assert not job.actor_log.exists()
    assert_unchanged(before, ArtifactSnapshot.capture(job.isolation.root))


def test_changed_source_after_planning_blocks_before_inference(job: PackagedJob) -> None:
    """Deterministic boundary fault: mutate selected source after its first plan."""
    _without_import(job)
    job.startup_hook("""
from apm_cli.contracts import frontend
original = frontend.plan_contract
changed = False
def tamper(*args, **kwargs):
    global changed
    plan = original(*args, **kwargs)
    if not changed:
        changed = True
        with plan.contract.path.open("a") as stream:
            stream.write("\\nChanged after admission.\\n")
    return plan
frontend.plan_contract = tamper
""")
    before = ArtifactSnapshot.capture(job.caller)
    result = job.packaged()
    assert result.returncode in {21, 22}, result.stdout + result.stderr
    assert "changed" in (result.stdout + result.stderr).lower()
    assert (job.package / _CONTRACT).read_text().endswith("Changed after admission.\n")
    assert not job.actor_log.exists()
    assert_unchanged(before, ArtifactSnapshot.capture(job.caller))


def test_remote_transport_fixture_retains_evidence_after_preparation_cleanup(
    job: PackagedJob,
) -> None:
    """Mock transport only: real remote-reference selection, checks, and temp lifecycle."""
    _without_import(job)
    download_log = job.isolation.root / "download.json"
    job.env["APMX_TRANSPORT_SOURCE"] = str(job.package)
    job.env["APMX_DOWNLOAD_LOG"] = str(download_log)
    job.startup_hook("""
import json
import shutil
from pathlib import Path
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference
from apm_cli.utils.yaml_io import load_yaml_str
def fixture_download(self, repo_ref, target_path, *args, **kwargs):
    target_path = Path(target_path)
    shutil.copytree(os.environ["APMX_TRANSPORT_SOURCE"], target_path, dirs_exist_ok=True)
    package = APMPackage.from_mapping(
        load_yaml_str((target_path / "apm.yml").read_text()),
        package_path=target_path, source_path=target_path, create_config=False,
    )
    Path(os.environ["APMX_DOWNLOAD_LOG"]).write_text(json.dumps({
        "target": str(target_path), "reference": str(repo_ref),
    }))
    return PackageInfo(
        package=package, install_path=target_path,
        resolved_reference=ResolvedReference("v1", GitReferenceType.TAG, "a" * 40, "v1"),
    )
GitHubPackageDownloader.__init__ = lambda self, *args, **kwargs: setattr(self, "auth_resolver", None)
GitHubPackageDownloader.download_package = fixture_download
""")
    before = ArtifactSnapshot.capture(job.caller)
    source_before = ArtifactSnapshot.capture(job.package)
    temp_before = ArtifactSnapshot.capture(job.isolation.temp_root)
    result = job.run("--from", "example/packaged-job#v1", _CONTRACT)
    assert result.returncode == 0, result.stdout + result.stderr
    downloaded = json.loads(download_log.read_bytes())
    assert not Path(downloaded["target"]).exists()
    run, record = job.record()
    artifact = Path(record["artifact"]["path"])
    assert artifact.is_relative_to(run)
    assert record["artifact"]["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert record["source"]["package"]["resolved_commit"] == "a" * 40
    assert record["source"]["package"]["package_ref"] == "example/packaged-job#v1"
    assert (run / "baseline" / _CAPTURED_SOURCE).read_bytes() == (
        job.package / _CONTRACT
    ).read_bytes()
    assert record["checks"][0]["normalized"] == 0
    _assert_only_evidence_added(before, job.caller)
    assert_unchanged(source_before, ArtifactSnapshot.capture(job.package))
    assert_unchanged(temp_before, ArtifactSnapshot.capture(job.isolation.temp_root))
