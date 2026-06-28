"""Round-32 red-team probes of the lifecycle-script TRUST surface.

Trust has been CLEAN r9-r31 (23 consecutive rounds -- the most robust
domain). Prior rounds already nailed: case-insensitive path keys (r22),
alias / merge-key out-of-subtree drift (r21/r22), NFC/NFD command + key
collisions (r23/r31), alias-bomb fail-closed (r26), trust-store format /
duplicate-key / prefix / int-value (r31), and the trust-write race (r31).

This round attacks the seams those rounds UNDER-probed, all driven through
the REAL firing gate ``build_runner_from_context`` (the same path
``apm install`` / ``update`` / ``uninstall`` use) and the REAL
``fingerprint_lifecycle_subtree`` -- never a re-implementation:

  1. FIELD-AGNOSTIC HASH COVERAGE (pivot 4: "a field added since the
     canonicalizer was written"). The canonicalizer is NOT a field
     whitelist -- it json-dumps the WHOLE raw lifecycle subtree -- so EVERY
     executor-read field (url, run, bash, command, type, timeoutSec, cwd,
     env, headers, allowedEnvVars) AND any unknown future field must be
     baked into the fp. We mutate each one and assert the fp re-gates AND
     the end-to-end gate refuses to fire the mutated (untrusted) variant.

  2. run / bash / command PRECEDENCE (pivot 4). ``_build_entry`` prefers
     ``bash`` over ``command`` on Unix, but the fp covers ALL three keys.
     Trusting ``{run: BENIGN}`` must NOT authorize ``{bash: BENIGN,
     command: EVIL}`` even though their Unix effective_command is identical
     -- the differing ``command`` key is in the hash domain.

  3. YAML INT-NORMALIZATION collisions (pivot 4, KNOWN-CLEAN). ``0x1e`` /
     ``3_0`` / ``30`` all decode to the SAME Python int 30, so they share a
     fp -- but they are EXECUTION-EQUIVALENT (identical timeout), so the
     collision carries no differential. Asserted, documented non-genuine.

  4. STORE READ/WRITE RACE under real multiprocessing (pivot 3). N writers
     trust a BENIGN repo while a reader repeatedly gates an EVIL-variant
     repo; the reader must observe pre- or post-write state, never a torn
     file that over-trusts EVIL, and never an abort.

  5. TIER handling (pivot 5). The PROJECT tier (the attacker's committed
     apm.yml) is ALWAYS gated; the user tier (~/.apm/apm.yml) is ungated by
     design but is NOT repo-controlled. We assert the project tier never
     rides a user-tier-style un-gating.

A genuine BREAK would show the fire spy firing on an UNtrusted / edited
project, or a trust grant authorizing DIFFERENT executable bytes than were
approved. A fail-closed DoS (install merely skips) is NOT a break.
"""

from __future__ import annotations

import multiprocessing as mp
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

import apm_cli.core.script_executors as _se
from apm_cli.core import script_trust
from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    PackageInfo,
    build_runner_from_context,
)
from apm_cli.core.script_trust import fingerprint_lifecycle_subtree

_ROOT = Path(tempfile.mkdtemp(prefix="rt32-trust-"))


# --------------------------------------------------------------------------
# Harness (mirrors r31): real gate + executor spy, hang-guarded.
# --------------------------------------------------------------------------


@pytest.fixture
def tmp_apm_home(monkeypatch):
    home = tempfile.mkdtemp(dir=str(_ROOT))
    monkeypatch.setenv("APM_HOME", home)
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    yield Path(home)


def _proj(text: str, name: str | None = None) -> Path:
    d = Path(tempfile.mkdtemp(dir=str(_ROOT), prefix=(name or "p") + "-"))
    (d / "apm.yml").write_text(text, encoding="utf-8")
    return d


def _event(project_root: str) -> LifecycleEvent:
    return LifecycleEvent.create(
        event="pre-install",
        packages=[PackageInfo(name="x/y", reference="v0")],
        scope="project",
        working_directory=project_root,
    )


def _run_with_timeout(fn, seconds: float = 25.0):
    box: dict[str, object] = {}

    def _t():
        try:
            box["r"] = fn()
        except BaseException as e:  # surface in caller thread
            box["e"] = e

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(seconds)
    assert not th.is_alive(), "gate hung (possible parse/serialize/resolve DoS)"
    if "e" in box:
        raise box["e"]  # type: ignore[misc]
    return box.get("r")


def _build_and_fire(project_root: str, fired: list[str]) -> None:
    def _spy_exec(script, event, **kw):
        fired.append(script.effective_command or script.url or "")

    def _spy_http(scripts, event, **kw):
        for s in scripts:
            fired.append(s.url or s.effective_command or "")
        return []

    with (
        mock.patch.object(_se, "execute_script", _spy_exec),
        mock.patch.object(_se, "dispatch_http_batch", _spy_http),
    ):
        runner = build_runner_from_context(project_root=project_root)
        runner.fire("pre-install", _event(project_root))


def _fire_real_gate(project_root: str) -> list[str]:
    fired: list[str] = []
    _run_with_timeout(lambda: _build_and_fire(project_root, fired))
    return fired


def _trust(project: Path) -> str | None:
    return script_trust.trust_project_scripts(project / "apm.yml")


# --------------------------------------------------------------------------
# Sanity: the secure contract (a regression would invert these).
# --------------------------------------------------------------------------


def test_untrusted_project_never_fires(tmp_apm_home):
    proj = _proj("lifecycle:\n  pre-install:\n    - run: echo PWNED\n", "untrusted")
    assert _fire_real_gate(str(proj)) == []


def test_trusted_project_fires(tmp_apm_home):
    proj = _proj("lifecycle:\n  pre-install:\n    - run: echo BENIGN\n", "trusted")
    assert _trust(proj) is not None
    assert _fire_real_gate(str(proj)) == ["echo BENIGN"]


# --------------------------------------------------------------------------
# 1. FIELD-AGNOSTIC HASH COVERAGE -- every executor-read field is in the fp.
#    For each field: trust a BENIGN entry, then mutate ONLY that field to an
#    EVIL value and assert (a) the fp changes (re-gate) and (b) the real gate
#    refuses to fire the mutated variant under the stale grant.
# --------------------------------------------------------------------------

# (yaml_text_benign, yaml_text_evil_variant, human label)
_FIELD_MUTATIONS = [
    # url (http type)
    (
        "lifecycle:\n  pre-install:\n    - type: http\n      url: https://ok.example/h\n",
        "lifecycle:\n  pre-install:\n    - type: http\n      url: https://evil.example/h\n",
        "url",
    ),
    # run scalar
    (
        "lifecycle:\n  pre-install:\n    - run: echo OK\n",
        "lifecycle:\n  pre-install:\n    - run: echo EVIL\n",
        "run",
    ),
    # bash field
    (
        "lifecycle:\n  pre-install:\n    - type: command\n      bash: echo OK\n",
        "lifecycle:\n  pre-install:\n    - type: command\n      bash: echo EVIL\n",
        "bash",
    ),
    # command field (the non-effective one on Unix -- still must be hashed)
    (
        "lifecycle:\n  pre-install:\n    - type: command\n      command: echo OK\n",
        "lifecycle:\n  pre-install:\n    - type: command\n      command: echo EVIL\n",
        "command",
    ),
    # type switch command->http (changes which executor runs)
    (
        "lifecycle:\n  pre-install:\n    - type: command\n      run: echo OK\n      url: https://evil.example/h\n",
        "lifecycle:\n  pre-install:\n    - type: http\n      run: echo OK\n      url: https://evil.example/h\n",
        "type",
    ),
    # timeoutSec
    (
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      timeoutSec: 30\n",
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      timeoutSec: 99\n",
        "timeoutSec",
    ),
    # cwd
    (
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      cwd: sub\n",
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      cwd: other\n",
        "cwd",
    ),
    # env value
    (
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      env:\n        K: ok\n",
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      env:\n        K: evil\n",
        "env-value",
    ),
    # env key
    (
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      env:\n        K1: v\n",
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      env:\n        K2: v\n",
        "env-key",
    ),
    # headers value (http)
    (
        "lifecycle:\n  pre-install:\n    - type: http\n      url: https://ok.example/h\n      headers:\n        H: ok\n",
        "lifecycle:\n  pre-install:\n    - type: http\n      url: https://ok.example/h\n      headers:\n        H: $SECRET\n",
        "headers-value",
    ),
    # allowedEnvVars (opt-in denylist bypass -- security-relevant)
    (
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      allowedEnvVars:\n        - SAFE\n",
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      allowedEnvVars:\n        - AWS_SECRET_ACCESS_KEY\n",
        "allowedEnvVars",
    ),
    # unknown / future field (proves the dump is not a whitelist)
    (
        "lifecycle:\n  pre-install:\n    - run: echo OK\n",
        "lifecycle:\n  pre-install:\n    - run: echo OK\n      futureField: shellInjection\n",
        "unknown-future-field",
    ),
]


@pytest.mark.parametrize(
    "benign,evil,label", _FIELD_MUTATIONS, ids=[m[2] for m in _FIELD_MUTATIONS]
)
def test_every_field_is_in_the_hash_domain(tmp_apm_home, benign, evil, label):
    """A mutation of ANY executor-read field must re-gate (fp change) and the
    real gate must refuse the mutated variant under the stale grant."""
    import yaml

    sub_b = yaml.safe_load(benign)["lifecycle"]
    sub_e = yaml.safe_load(evil)["lifecycle"]
    fp_b = fingerprint_lifecycle_subtree(sub_b)
    fp_e = fingerprint_lifecycle_subtree(sub_e)
    assert fp_b is not None, f"benign {label} did not fingerprint"
    assert fp_b != fp_e, f"OVER-TRUST: mutating {label} did NOT change the fingerprint"

    # End-to-end: trust BENIGN, swap in the EVIL variant, gate must skip it.
    proj = _proj(benign, f"field-{label}")
    assert _trust(proj) is not None
    (proj / "apm.yml").write_text(evil, encoding="utf-8")
    fired = _fire_real_gate(str(proj))
    assert fired == [], f"OVER-TRUST: mutated {label} FIRED under a stale grant: {fired}"


# --------------------------------------------------------------------------
# 2. run / bash / command PRECEDENCE -- the non-effective key is still hashed.
# --------------------------------------------------------------------------


def test_command_key_is_hashed_even_when_bash_wins_on_unix(tmp_apm_home):
    """On Unix effective_command = bash, so {bash:X} and {bash:X, command:EVIL}
    run the SAME command -- but the differing ``command`` key changes the fp,
    so a grant on the former must NOT authorize the latter."""
    benign = "lifecycle:\n  pre-install:\n    - type: command\n      bash: echo OK\n"
    evil = (
        "lifecycle:\n  pre-install:\n    - type: command\n"
        "      bash: echo OK\n      command: echo EVIL\n"
    )
    import yaml

    fp_b = fingerprint_lifecycle_subtree(yaml.safe_load(benign)["lifecycle"])
    fp_e = fingerprint_lifecycle_subtree(yaml.safe_load(evil)["lifecycle"])
    assert fp_b != fp_e, "OVER-TRUST: shadow ``command`` key escaped the fingerprint"

    proj = _proj(benign, "precedence")
    assert _trust(proj) is not None
    (proj / "apm.yml").write_text(evil, encoding="utf-8")
    assert _fire_real_gate(str(proj)) == [], "OVER-TRUST: shadow-command variant fired"


def test_run_alias_distinct_from_bash_key(tmp_apm_home):
    """``{run: X}`` and ``{bash: X}`` resolve to the same effective command on
    Unix but are distinct hash domains -> no cross-trust."""
    fp_run = fingerprint_lifecycle_subtree({"pre-install": [{"run": "echo X"}]})
    fp_bash = fingerprint_lifecycle_subtree({"pre-install": [{"bash": "echo X"}]})
    assert fp_run != fp_bash


# --------------------------------------------------------------------------
# 3. YAML INT-NORMALIZATION collisions -- KNOWN-CLEAN (execution-equivalent).
# --------------------------------------------------------------------------


def test_int_normalization_collision_is_execution_equivalent(tmp_apm_home):
    """``0x1e`` / ``3_0`` / ``30`` decode to the SAME int 30 -> same fp. This is
    a canonicalization collision but it is EXECUTION-EQUIVALENT (identical
    timeout), so it carries no differential and is NOT a break."""
    import yaml

    fps = set()
    timeouts = set()
    for lit in ("30", "0x1e", "3_0"):
        sub = yaml.safe_load(f"pre-install:\n  - run: echo X\n    timeoutSec: {lit}\n")
        fps.add(fingerprint_lifecycle_subtree(sub))
        timeouts.add(sub["pre-install"][0]["timeoutSec"])
    assert fps == {next(iter(fps))}, "expected the int forms to share one fp"
    assert timeouts == {30}, "collision is only clean because the int value is identical"


# --------------------------------------------------------------------------
# 4. STORE READ/WRITE RACE under real multiprocessing.
#    Writers trust BENIGN; the reader gates an EVIL variant and must NEVER
#    over-trust EVIL through a torn read.
# --------------------------------------------------------------------------


def _writer_proc(apm_home: str, benign_yml: str, n: int) -> None:
    import os

    os.environ["APM_HOME"] = apm_home
    from apm_cli.core import script_trust as st

    p = Path(benign_yml)
    for _ in range(n):
        st.trust_project_scripts(p)
        # churn the store back to empty to widen the torn-read window
        st.untrust_project_scripts(p)


def test_store_race_never_over_trusts_evil(tmp_apm_home):
    """Concurrent trust/untrust churn on a BENIGN repo must never let a reader
    over-trust a DIFFERENT (evil) repo via a torn store read."""
    benign = _proj("lifecycle:\n  pre-install:\n    - run: echo BENIGN\n", "race-benign")
    evil = _proj("lifecycle:\n  pre-install:\n    - run: echo PWNED\n", "race-evil")

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_writer_proc, args=(str(tmp_apm_home), str(benign / "apm.yml"), 60))
        for _ in range(4)
    ]
    for p in procs:
        p.start()

    deadline = time.time() + 12.0
    reads = 0
    try:
        while time.time() < deadline and any(p.is_alive() for p in procs):
            # The evil repo was NEVER trusted: every gate read must skip it,
            # regardless of how torn the concurrently-written store is.
            assert script_trust.is_project_scripts_trusted(evil / "apm.yml") is False, (
                "OVER-TRUST: torn store read trusted an un-granted evil repo"
            )
            reads += 1
    finally:
        for p in procs:
            p.join(10.0)
            assert not p.is_alive(), "writer process hung"
            assert p.exitcode == 0, f"writer crashed (exitcode={p.exitcode})"

    assert reads > 0, "reader never sampled the store during the race"
    # Final consistency: after all churn ends in an untrust, neither is trusted.
    assert script_trust.is_project_scripts_trusted(evil / "apm.yml") is False


# --------------------------------------------------------------------------
# 5. TIER handling -- the project tier (attacker's repo) is ALWAYS gated.
# --------------------------------------------------------------------------


def test_project_tier_always_gated_user_tier_independent(tmp_apm_home):
    """A project apm.yml grant is keyed by the project path; it cannot be made
    to ride the (ungated) user tier. With a sane APM_HOME (the trust-store
    home, NOT the repo), the project tier stays gated and an untrusted repo
    fires nothing even when an unrelated user-tier file exists."""
    # Plant a user-tier lifecycle (ungated by design) -- benign, unrelated.
    (tmp_apm_home / "apm.yml").write_text(
        "lifecycle:\n  pre-install:\n    - run: echo USERTIER\n", encoding="utf-8"
    )
    proj = _proj("lifecycle:\n  pre-install:\n    - run: echo PROJECT_EVIL\n", "tier")
    fired = _fire_real_gate(str(proj))
    # User tier fires (developer-controlled location); project tier is gated out.
    assert "echo PROJECT_EVIL" not in fired, "OVER-TRUST: untrusted project rode the user tier"
