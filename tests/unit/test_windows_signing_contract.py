"""Static contract: Windows Authenticode signing step exists in build-release.yml.

Regression guard for microsoft/apm#2435: the Windows binary release was flagged
as Trojan:Script/Wacatac.H!ml by Windows Defender because PyInstaller-packed
binaries trigger ML-based AV heuristics without Authenticode signatures. The fix
adds a signing step gated on the WINDOWS_CERT_PFX secret. This test pins that
contract so it cannot be silently removed by a future workflow edit.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import (
    WorkflowNode,
    load_workflow,
    workflow_job,
    workflow_step,
    workflow_step_index,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-release.yml"

SIGNING_STEP = "Sign Windows binary (Authenticode)"
BUILD_STEP = "Build binary (Windows)"
UPLOAD_STEP = "Upload binary as workflow artifact"


def _workflow() -> WorkflowNode:
    return load_workflow(WORKFLOW)


def _assert_signing_step(workflow: WorkflowNode) -> None:
    """Assert the Authenticode signing step is correctly configured."""
    job = workflow_job(workflow, "build-and-test")
    step = workflow_step(job, SIGNING_STEP)

    # Must only run on the Windows matrix entry.
    step_if: str = step.get("if", "")
    assert "windows" in step_if, (
        f"signing step must be gated on Windows platform, got if={step_if!r}"
    )

    # Must reference the WINDOWS_CERT_PFX secret so unsigned builds skip it.
    assert "WINDOWS_CERT_PFX" in step_if, (
        "signing step must be gated on WINDOWS_CERT_PFX secret being non-empty"
    )

    # Must invoke sign-binary.ps1.
    run_text: str = step.get("run", "")
    assert "sign-binary.ps1" in run_text, "signing step must invoke scripts/windows/sign-binary.ps1"

    # Must pass the certificate secret via env.
    env = step.get("env", {})
    assert isinstance(env, dict), f"signing step env must be a mapping, got {type(env)!r}"
    assert "WINDOWS_CERT_PFX" in env, "signing step env must include WINDOWS_CERT_PFX"
    assert "WINDOWS_CERT_PASSWORD" in env, "signing step env must include WINDOWS_CERT_PASSWORD"

    # Ordering: signing must occur after build and before upload.
    build_idx = workflow_step_index(job, BUILD_STEP)
    sign_idx = workflow_step_index(job, SIGNING_STEP)
    upload_idx = workflow_step_index(job, UPLOAD_STEP)
    assert build_idx < sign_idx < upload_idx, (
        f"signing step order wrong: build={build_idx} sign={sign_idx} upload={upload_idx}"
    )


def test_signing_step_exists() -> None:
    """Signing step is present in the build-and-test job."""
    _assert_signing_step(_workflow())


def test_signing_step_gated_on_windows() -> None:
    """Removing the Windows platform gate must fail the contract."""
    workflow = deepcopy(_workflow())
    step = workflow_step(workflow_job(workflow, "build-and-test"), SIGNING_STEP)
    step["if"] = "env.WINDOWS_CERT_PFX != ''"  # no platform gate

    with pytest.raises(AssertionError, match="Windows platform"):
        _assert_signing_step(workflow)


def test_signing_step_gated_on_secret() -> None:
    """Removing the secret gate must fail -- signing must skip gracefully."""
    workflow = deepcopy(_workflow())
    step = workflow_step(workflow_job(workflow, "build-and-test"), SIGNING_STEP)
    step["if"] = "matrix.platform == 'windows'"  # no secret gate

    with pytest.raises(AssertionError, match="WINDOWS_CERT_PFX"):
        _assert_signing_step(workflow)


def test_signing_step_references_script() -> None:
    """Replacing the script reference must fail the contract."""
    workflow = deepcopy(_workflow())
    step = workflow_step(workflow_job(workflow, "build-and-test"), SIGNING_STEP)
    step["run"] = "echo skipping signing\n"

    with pytest.raises(AssertionError, match=r"sign-binary\.ps1"):
        _assert_signing_step(workflow)


def test_signing_step_order() -> None:
    """Moving the signing step after upload must fail the contract."""
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, "build-and-test")
    steps: list = job["steps"]

    # Find and remove the signing step, then insert it after the upload step.
    sign_step = next(
        (s for s in steps if isinstance(s, dict) and s.get("name") == SIGNING_STEP),
        None,
    )
    assert sign_step is not None, "signing step not found for order mutation"
    steps.remove(sign_step)
    upload_idx = next(
        i for i, s in enumerate(steps) if isinstance(s, dict) and s.get("name") == UPLOAD_STEP
    )
    steps.insert(upload_idx + 1, sign_step)

    with pytest.raises(AssertionError, match="order"):
        _assert_signing_step(workflow)


def test_signing_step_requires_password_env() -> None:
    """Removing WINDOWS_CERT_PASSWORD from env must fail the contract."""
    workflow = deepcopy(_workflow())
    step = workflow_step(workflow_job(workflow, "build-and-test"), SIGNING_STEP)
    env = step.get("env", {})
    assert isinstance(env, dict), "env must be a mapping for this mutation test"
    env.pop("WINDOWS_CERT_PASSWORD", None)

    with pytest.raises(AssertionError, match="WINDOWS_CERT_PASSWORD"):
        _assert_signing_step(workflow)


# ---------------------------------------------------------------------------
# Static code contracts for scripts/windows/sign-binary.ps1
# ---------------------------------------------------------------------------

SIGN_SCRIPT = ROOT / "scripts" / "windows" / "sign-binary.ps1"


def _sign_script_text() -> str:
    return SIGN_SCRIPT.read_text(encoding="utf-8")


def test_sign_script_restricts_acl() -> None:
    """sign-binary.ps1 must call SetAccessRuleProtection to disable ACL inheritance.

    Regression guard for the FU-3a supply-chain finding: the temp PFX file
    must not be world-readable on shared runners. SetAccessRuleProtection with
    ($true, $false) removes inherited rules and ensures only an explicit
    current-user grant remains.
    """
    text = _sign_script_text()
    assert "SetAccessRuleProtection" in text, (
        "sign-binary.ps1 must call SetAccessRuleProtection to restrict the "
        "temporary PFX file to the current user only -- world-readable temp "
        "files expose the private key on shared runners"
    )


def test_sign_script_uses_exclusive_file_lock() -> None:
    """sign-binary.ps1 must use FileShare.None when writing the temp PFX.

    Ensures no concurrent reader can observe the private key bytes during
    the file-write window, even on a shared runner.
    """
    text = _sign_script_text()
    assert "FileShare" in text, (
        "sign-binary.ps1 must open the temp PFX with FileShare.None (exclusive "
        "lock) to prevent a concurrent process from reading the private key "
        "bytes during the write window"
    )
