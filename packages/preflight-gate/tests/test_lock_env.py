"""env.lock-sync: drift detection, self-provisioning, machine-local tolerance."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import preflight
import preflight_lib as lib
import preflight_policy as policy
import pytest
from conftest import Sandbox

CLEAN = (
    "Would use project environment at: .venv\nChecked 96 packages in 40ms\nWould make no changes\n"
)
FILELOCK_DRIFT = (
    "Would use project environment at: .venv\nWould install 1 package\n"
    " - filelock==4.2.9\n + filelock==4.2.10\n"
)
CLICK_DRIFT = (
    "Would use project environment at: .venv\nWould install 2 packages\n"
    " - click==8.5.0\n + click==8.2.1\n"
    " ~ apm-cli==0.35.0 (from file:///repo)\n"
)


class FakeUv:
    """Scripted `uv`: responses keyed by the first argv words."""

    def __init__(self, responses: dict[str, list[tuple[int, str]]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
        self.calls.append(list(args))
        key = "dry-run" if "--dry-run" in args else " ".join(args[:2])
        if args[0] == "export":
            out = Path(args[args.index("-o") + 1])
            out.write_text(
                "click==8.2.1\n"
                "filelock==3.29.0 ; python_full_version < '3.11'\n"
                "filelock==4.2.10 ; python_full_version >= '3.11'\n"
                "    # via apm-cli\n",
                encoding="utf-8",
            )
            return 0, ""
        queue = self.responses.get(key, [(0, "")])
        return queue.pop(0) if len(queue) > 1 else queue[0]


def test_parse_drift_ignores_editable_project_reinstall() -> None:
    clean, drift = lib.parse_drift(CLICK_DRIFT)
    assert not clean
    assert drift == {"click": {"locked": "8.2.1", "installed": "8.5.0"}}
    assert lib.parse_drift(CLEAN) == (True, {})


@pytest.mark.parametrize(
    ("output", "tolerate", "ok", "tolerated", "untolerated"),
    [
        (CLEAN, set(), True, [], []),
        (CLICK_DRIFT, set(), False, [], ["click"]),
        (FILELOCK_DRIFT, {"filelock"}, True, ["filelock"], []),
        (CLICK_DRIFT, {"filelock"}, False, [], ["click"]),
        ("error: something broke", set(), False, [], []),
    ],
)
def test_check_lock(output, tolerate, ok, tolerated, untolerated, workdir: Path) -> None:
    fake = FakeUv({"dry-run": [(0, output)]})
    result = lib.check_lock(workdir, tolerate, runner=fake)
    assert result["ok"] is ok
    assert [r["name"] for r in result["tolerated"]] == tolerated
    assert [r["name"] for r in result["untolerated"]] == untolerated
    assert fake.calls[0][:3] == ["sync", "--frozen", "--extra"]


def test_tolerance_from_git_config_and_env_override(
    sandbox: Sandbox, workdir: Path, monkeypatch
) -> None:
    gitconfig = Path(__import__("os").environ["GIT_CONFIG_GLOBAL"])
    gitconfig.write_text(
        gitconfig.read_text(encoding="utf-8") + '[apm "preflight"]\n\tlockTolerate = filelock\n',
        encoding="utf-8",
    )
    assert lib.tolerated_names({}, sandbox.root) == {"filelock"}
    assert lib.tolerated_names({lib.TOLERATE_ENV: "click"}, sandbox.root) == {"click"}
    assert lib.tolerated_names({lib.TOLERATE_ENV: ""}, sandbox.root) == set()


def test_tolerate_env_is_normalized() -> None:
    names = lib.tolerated_names({lib.TOLERATE_ENV: " FileLock, typing_extensions ,"})
    assert names == {"filelock", "typing-extensions"}
    assert lib.tolerated_names({}) == set()


def test_provision_prefers_frozen_uv_sync(workdir: Path) -> None:
    fake = FakeUv({"sync --frozen": [(0, "Installed 3 packages")]})
    assert lib.provision_env(workdir, workdir / "w", set(), lambda _t: None, runner=fake)
    assert fake.calls == [["sync", "--frozen", "--extra", "dev"]]


def _venv(root: Path, filelock: str | None = "4.2.9") -> Path:
    site = root / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (root / ".venv" / "bin").mkdir()
    (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    if filelock:
        (site / f"filelock-{filelock}.dist-info").mkdir()
    return root / ".venv"


def test_provision_falls_back_to_export_and_pip_sync(workdir: Path) -> None:
    _venv(workdir)
    fake = FakeUv({"sync --frozen": [(2, "Failed to download filelock")]})
    log: list[str] = []
    assert lib.provision_env(workdir, workdir / "w", {"filelock"}, log.append, runner=fake)
    verbs = [" ".join(c[:2]) for c in fake.calls]
    assert verbs == ["sync --frozen", "export --frozen", "pip sync", "pip install", "pip install"]
    assert fake.calls[3][-4:-2] == ["--upgrade-package", "filelock"]
    assert fake.calls[4][-3:] == ["--no-deps", "-e", "."]
    upgraded = (workdir / "w" / "lock-requirements.tolerated.txt").read_text(encoding="utf-8")
    assert upgraded.splitlines() == [
        "filelock==3.29.* ; python_full_version < '3.11'",
        "filelock==4.2.* ; python_full_version >= '3.11'",
    ]
    synced = (workdir / "w" / "lock-requirements.sync.txt").read_text(encoding="utf-8")
    assert "filelock==4.2.* ; python_full_version >= '3.11'" in synced
    assert "filelock==3.29.* ; python_full_version < '3.11'" in synced
    assert "click==8.2.1" in synced
    assert any("pip sync" in line for line in log)


def test_tolerated_package_is_left_as_installed_when_relaxing_fails(workdir: Path) -> None:
    _venv(workdir, filelock="4.1.0")
    fake = FakeUv(
        {"sync --frozen": [(2, "blocked")], "pip sync": [(1, "no filelock 4.2.*"), (0, "ok")]}
    )
    assert lib.provision_env(workdir, workdir / "w", {"filelock"}, lambda _t: None, runner=fake)
    synced = (workdir / "w" / "lock-requirements.sync.txt").read_text(encoding="utf-8")
    assert "filelock==4.1.0 ; python_full_version >= '3.11'" in synced


def test_untolerated_pins_are_never_relaxed(workdir: Path) -> None:
    _venv(workdir)
    fake = FakeUv({"sync --frozen": [(2, "blocked")], "pip sync": [(1, "missing click")]})
    assert not lib.provision_env(workdir, workdir / "w", set(), lambda _t: None, runner=fake)
    synced = (workdir / "w" / "lock-requirements.sync.txt").read_text(encoding="utf-8")
    assert "filelock==4.2.10 ; python_full_version >= '3.11'" in synced
    assert [" ".join(c[:2]) for c in fake.calls].count("pip sync") == 1


# --- runner integration ----------------------------------------------------------


def _policy(fake_policy) -> SimpleNamespace:
    def build(tier: str, _ctx) -> list[policy.Check]:
        if tier != "T0":
            return []
        return [
            policy.Check("t0.ok", "T0", cmd="true"),
            policy.Check("env.lock-sync", "T0", internal="lock_sync"),
        ]

    return SimpleNamespace(**{**vars(fake_policy), "build_checks": build})


def _script(monkeypatch, results: list[dict], provision_ok: bool = True) -> list[str]:
    calls: list[str] = []

    def check_lock(_root, tolerate, runner=None):
        calls.append(f"check:{sorted(tolerate)}")
        return results.pop(0) if len(results) > 1 else results[0]

    def provision_env(_root, _work, _tolerate, log, runner=None, python=None):
        calls.append("provision")
        log("fake provisioning")
        return provision_ok

    monkeypatch.setattr(lib, "check_lock", check_lock)
    monkeypatch.setattr(lib, "provision_env", provision_env)
    return calls


def _result(ok: bool, tolerated=(), untolerated=()) -> dict:
    return {
        "ok": ok,
        "tolerated": list(tolerated),
        "untolerated": list(untolerated),
        "output": "uv says",
        "exit": 0,
    }


CLICK = {"name": "click", "locked": "8.2.1", "installed": "8.5.0"}
FILELOCK = {"name": "filelock", "locked": "4.2.10", "installed": "4.2.9"}


def test_drift_triggers_provisioning_then_passes(
    sandbox: Sandbox, fake_policy, monkeypatch
) -> None:
    calls = _script(monkeypatch, [_result(False, untolerated=[CLICK]), _result(True)])
    assert preflight.run(["--no-fetch"], policy=_policy(fake_policy), cwd=sandbox.root) == 0
    assert calls == ["check:[]", "provision", "check:[]"]
    receipt = sandbox.receipt()
    assert receipt["env_provisioned"] is True
    assert receipt["lock_drift"] == []
    assert receipt["status"] == "pass"
    log = Path(next(c["log"] for c in receipt["checks"] if c["id"] == "env.lock-sync"))
    assert "fake provisioning" in log.read_text(encoding="utf-8")


def test_tolerated_drift_is_recorded_and_eligible(
    sandbox: Sandbox, fake_policy, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(lib.TOLERATE_ENV, "filelock")
    calls = _script(monkeypatch, [_result(True, tolerated=[FILELOCK])])
    assert preflight.run(["--no-fetch"], policy=_policy(fake_policy), cwd=sandbox.root) == 0
    assert calls == ["check:['filelock']"]
    receipt = sandbox.receipt()
    assert receipt["lock_drift"] == [FILELOCK]
    assert receipt["gate_eligible"] is True
    assert receipt["status"] == "pass"
    assert "[!] tolerated lock drift" in capsys.readouterr().err


def test_untolerated_drift_fails_when_provisioning_cannot_fix_it(
    sandbox: Sandbox, fake_policy, monkeypatch
) -> None:
    _script(monkeypatch, [_result(False, untolerated=[CLICK])], provision_ok=False)
    assert preflight.run(["--no-fetch"], policy=_policy(fake_policy), cwd=sandbox.root) == 1
    receipt = sandbox.receipt()
    assert receipt["status"] == "fail"
    assert [c["id"] for c in receipt["checks"] if c["exit"]] == ["env.lock-sync"]
    assert receipt["env_provisioned"] is False


def test_no_provision_flag(sandbox: Sandbox, fake_policy, monkeypatch) -> None:
    calls = _script(monkeypatch, [_result(False, untolerated=[CLICK])])
    args = ["--no-fetch", "--no-provision"]
    assert preflight.run(args, policy=_policy(fake_policy), cwd=sandbox.root) == 1
    assert "provision" not in calls


def test_lock_check_runs_before_phase_a(sandbox: Sandbox, fake_policy, monkeypatch, capsys) -> None:
    _script(monkeypatch, [_result(True)])
    assert preflight.run(["--no-fetch"], policy=_policy(fake_policy), cwd=sandbox.root) == 0
    err = capsys.readouterr().err
    assert err.index("env.lock-sync") < err.index("phase A")
