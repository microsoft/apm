"""Installed-CLI contract: a stale recorded exec_status fails frozen and audit (#4053).

On a merged-config target (claude ``settings.json``) a hook the executables
gate now refuses leaves no recorded file missing, so presence checks cannot
see it. When ``apm deny`` or an org policy tightening changes the gate's
decision for a locked package, ``apm install --frozen`` and ``apm audit --ci``
must both fail, naming the package with its recorded and current status,
until a plain ``apm install`` records the new decision.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_PACKAGE = "./pkgs/hooky"
_COMMAND = "echo hi-hooky"
_OPEN_POLICY = "name: p\nversion: 1.0.0\nenforcement: block\n"
_DENY_ALL_POLICY = _OPEN_POLICY + "executables:\n  deny_all: true\n"
_REPAIR = "run 'apm install' without --frozen"


def _chain(tmp_path: Path, binary: Path, target: str) -> Chain:
    chain = Chain(tmp_path / "world", binary, dirs=(f".{target}",))
    hooks = chain.project / "pkgs" / "hooky" / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    (hooks.parents[1] / "apm.yml").write_text("name: hooky\nversion: 1.0.0\n", encoding="utf-8")
    document = {
        "hooks": {
            "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": _COMMAND}]}]
        }
    }
    (hooks / "hooky.json").write_text(json.dumps(document), encoding="utf-8")
    chain.seed_org_policy(_OPEN_POLICY)
    chain.write_manifest(
        [],
        apm=[_PACKAGE],
        targets=[target],
        executables={"allow": {_PACKAGE: {"hooks": True}}},
    )
    return chain


def _tighten_org_policy(chain: Chain) -> None:
    """Replace the cached org policy with one that denies every executable."""
    script = (
        "import sys\nfrom pathlib import Path\n"
        "from apm_cli.policy.discovery import _write_cache\n"
        "from apm_cli.policy.parser import load_policy\n"
        "policy, _ = load_policy(sys.argv[2])\n"
        "_write_cache('acme/.github-private', policy, Path(sys.argv[1]))\n"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(chain.project), _DENY_ALL_POLICY],
        env=chain.env,
        check=True,
    )


def _hook_deployed(chain: Chain) -> bool:
    return any(
        _COMMAND in path.read_text(encoding="utf-8")
        for path in chain.project.rglob("*.json")
        if "pkgs" not in path.relative_to(chain.project).parts
        and "apm_modules" not in path.relative_to(chain.project).parts
    )


def _exec_status(chain: Chain) -> str | None:
    (dependency,) = chain.lock()["dependencies"]
    return dependency.get("exec_status")


def _fails(chain: Chain, *args: str) -> str:
    result = chain.run(*args)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, f"{args}\n{output}"
    return output


@pytest.mark.parametrize("revocation", ["deny", "org-tightening"])
def test_stale_exec_status_fails_frozen_and_audit_until_install(
    tmp_path: Path, apm_binary_path: Path, revocation: str
) -> None:
    chain = _chain(tmp_path, apm_binary_path, "claude")
    chain.ok("install")
    assert _hook_deployed(chain)
    assert _exec_status(chain) == "deployed"

    if revocation == "deny":
        chain.ok("deny", _PACKAGE)
    else:
        _tighten_org_policy(chain)
    lock_path = chain.project / "apm.lock.yaml"
    lock_bytes = lock_path.read_bytes()

    frozen = _fails(chain, "install", "--frozen")
    assert "hooky" in frozen, frozen
    assert "recorded deployed, now denied" in frozen, frozen
    assert _REPAIR in frozen, frozen
    assert "commit apm.lock.yaml" in frozen, frozen
    assert not _hook_deployed(chain)
    assert lock_path.read_bytes() == lock_bytes

    audit = _fails(chain, "audit", "--ci")
    assert "hooky" in audit, audit
    assert "recorded deployed, now denied" in audit, audit

    chain.ok("install")
    assert _exec_status(chain) == "denied"
    chain.ok("install", "--frozen")
    chain.ok("audit", "--ci")


@pytest.mark.parametrize("target", ["claude", "copilot"])
def test_unchanged_exec_decision_keeps_frozen_and_audit_green(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    chain = _chain(tmp_path, apm_binary_path, target)
    chain.ok("install")
    assert _hook_deployed(chain)
    lock_bytes = (chain.project / "apm.lock.yaml").read_bytes()

    chain.ok("install", "--frozen")
    chain.ok("audit", "--ci")
    assert _hook_deployed(chain)
    assert (chain.project / "apm.lock.yaml").read_bytes() == lock_bytes
    assert yaml.safe_load(lock_bytes)["dependencies"][0]["exec_status"] == "deployed"


def _org_denied(tmp_path: Path, binary: Path) -> Chain:
    """A project whose last plain install recorded an org ``deny_all`` decision."""
    chain = _chain(tmp_path, binary, "claude")
    _tighten_org_policy(chain)
    chain.ok("install")
    assert _exec_status(chain) == "denied"
    assert not _hook_deployed(chain)
    return chain


def test_no_policy_audit_does_not_judge_org_derived_decisions(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``--no-policy`` cannot see the org decision, so the check is not evaluated."""
    chain = _org_denied(tmp_path, apm_binary_path)

    audit = " ".join(chain.ok("audit", "--ci", "--no-policy", "--no-drift").stdout.split())
    assert "now deployed" not in audit, audit


def test_unreachable_policy_without_cache_is_not_a_stale_decision(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Discovery failing with no cache leaves the decision unknown, not stale."""
    chain = _org_denied(tmp_path, apm_binary_path)
    script = (
        "import shutil, sys\nfrom pathlib import Path\n"
        "from apm_cli.policy.discovery import _get_cache_dir\n"
        "shutil.rmtree(_get_cache_dir(Path(sys.argv[1])))\n"
    )
    subprocess.run([sys.executable, "-c", script, str(chain.project)], env=chain.env, check=True)
    for proxy in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY"):
        chain.env[proxy] = "http://127.0.0.1:9"
    chain.env.pop("NO_PROXY", None)
    chain.env.pop("no_proxy", None)

    result = chain.run("audit", "--ci", "--no-drift")
    output = " ".join((result.stdout + result.stderr).split())
    assert "now deployed" not in output, output
    assert "exec-status-current" not in output or "not verified" in output, output
