"""Round-29 red-team probes of the lifecycle-script TRUST surface.

Twenty-first-round hunt for a GENUINE execution-differential trust bypass the
prior 20 clean rounds (r9-r28) missed. A genuine break REQUIRES an execution
differential: two DIFFERENT lifecycle payloads (a command / url / fired
env-VALUE that actually differs) sharing ONE trusted fingerprint, OR a trusted
fingerprint reused to run a different file's content. A bare fingerprint
collision with NO behavioral/payload difference is NOT genuine.

Round-29 DOMAIN: trust / fingerprint / TOCTOU. Novel seam probed this round:
the TWO sinks where the executor reads ARBITRARY (non-fixed) dict keys --
``script.env`` (merged into the command subprocess environment) and
``script.headers`` (sent on the HTTP request). Everywhere else the executor
reads only FIXED ASCII string keys, but env/header keys are author-chosen, so
json.dumps' non-string-key coercion (``True`` -> ``"true"``, ``1`` -> ``"1"``)
CAN collide two raw subtrees onto one fingerprint there. The question this
round answers: does that collision carry an EXECUTION DIFFERENTIAL?

Findings (all CLEAN -- the collision is non-genuine):

  * The colliding variant differs ONLY in a non-string KEY. The fired command
    bytes (``effective_command`` / ``url``) are byte-identical, so no
    command/url payload differential rides the shared fingerprint.
  * A non-string env key fails CLOSED at ``subprocess.Popen`` (TypeError) and a
    non-string header key fails CLOSED at ``requests`` header prep
    (InvalidHeader). The coerced-key variant therefore never FIRES, so the two
    colliding subtrees cannot both run a different payload.
  * Every executor-read VALUE (command, url, fired env value, header value,
    timeoutSec) is injective under json.dumps -- changing any of them rebakes
    the fingerprint and revokes trust (proven end-to-end through
    ``build_runner_from_context``).

Hangs are caught with a daemon thread + join(timeout) (the runtime bans the
``timeout`` shell command, kill, pkill).
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import (
    build_runner_from_context,
    parse_apm_yml_lifecycle_with_fingerprint,
)
from apm_cli.core.script_trust import (
    fingerprint_lifecycle_subtree,
    is_fingerprint_trusted,
    trust_project_scripts,
)

_ROOT = Path(tempfile.mkdtemp(prefix="rt29-trust-"))


@pytest.fixture
def tmp_apm_home(monkeypatch):
    home = tempfile.mkdtemp(dir=str(_ROOT))
    monkeypatch.setenv("APM_HOME", home)
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    yield Path(home)


def _proj(text: str) -> Path:
    """Write apm.yml into a fresh dir, return its path."""
    d = Path(tempfile.mkdtemp(dir=str(_ROOT)))
    yml = d / "apm.yml"
    yml.write_text(text, encoding="utf-8")
    return yml


def _run_with_timeout(fn, seconds: float = 20.0):
    box: dict[str, object] = {}

    def _t():
        try:
            box["r"] = fn()
        except BaseException as e:
            box["e"] = e

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(seconds)
    assert not th.is_alive(), "operation hung (possible parse/serialize DoS)"
    if "e" in box:
        raise box["e"]  # type: ignore[misc]
    return box["r"]


def _kept_cmds(runner) -> list[str | None]:
    return [s.effective_command for s in runner.scripts_for_event("post-install")]


# -- A. env-key type collision is NON-GENUINE (no command differential) ------


def test_envkey_type_collision_shares_fp_but_no_command_differential():
    """``env: {true: ...}`` (bool key) and ``env: {"true": ...}`` (str key)
    canonicalise to the SAME fingerprint via json.dumps key coercion, but the
    FIRED command bytes are byte-identical, so the shared fingerprint carries
    no command/url execution differential.

    SECURE INVARIANT: if two raw subtrees share a fingerprint, every value the
    executor would RUN (effective_command, url) must be identical. A genuine
    break would show two DIFFERENT fired commands under one fingerprint.
    """
    body = "lifecycle:\n  post-install:\n    - run: echo SAFE_CMD\n      env:\n"
    boolkey = _proj(body + "        true: /attacker\n")
    strkey = _proj(body + '        "true": /attacker\n')

    ent_b, fp_b = parse_apm_yml_lifecycle_with_fingerprint(boolkey, "project")
    ent_s, fp_s = parse_apm_yml_lifecycle_with_fingerprint(strkey, "project")

    # The collision exists (this is the non-genuine part we document) ...
    assert fp_b == fp_s and fp_b is not None
    # ... but the env KEY differs only in TYPE (bool vs str) -- the value the
    # executor would RUN is identical, so there is no payload differential.
    assert list((ent_b[0].env or {}).keys()) == [True]
    assert list((ent_s[0].env or {}).keys()) == ["true"]
    assert ent_b[0].effective_command == ent_s[0].effective_command == "echo SAFE_CMD"
    assert ent_b[0].url is None and ent_s[0].url is None


def test_nonstring_env_key_fails_closed_at_subprocess():
    """The coerced (non-string) env-key variant cannot FIRE: a non-string env
    key raises TypeError inside subprocess.Popen, so the colliding subtree
    never runs a differential payload (fail-closed), it merely no-ops.

    This is what makes the env-key collision non-genuine end-to-end: both
    colliding subtrees CANNOT simultaneously run two different payloads.
    """
    import subprocess

    env = {k: v for k, v in os.environ.items()}
    env.update({True: "/attacker"})  # the bool-key coercion case
    with pytest.raises(TypeError):
        subprocess.Popen(
            ["true"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


# -- B. header-key type collision is NON-GENUINE (no url/value differential) --


def test_headerkey_type_collision_shares_fp_but_no_payload_differential():
    """HTTP header keys are the other arbitrary-key sink. ``headers: {1: v}``
    (int key) and ``headers: {"1": v}`` (str key) share a fingerprint, but the
    fired url and header VALUES are identical, so no payload differential
    rides the collision.
    """
    body = (
        "lifecycle:\n  post-install:\n"
        "    - type: http\n"
        "      url: https://example.test/hook\n"
        "      headers:\n"
    )
    intkey = _proj(body + "        1: tokenval\n")
    strkey = _proj(body + '        "1": tokenval\n')

    ent_i, fp_i = parse_apm_yml_lifecycle_with_fingerprint(intkey, "project")
    ent_s, fp_s = parse_apm_yml_lifecycle_with_fingerprint(strkey, "project")

    assert fp_i == fp_s and fp_i is not None
    assert list((ent_i[0].headers or {}).keys()) == [1]
    assert list((ent_s[0].headers or {}).keys()) == ["1"]
    # url + header VALUE (the security-relevant payload) are identical.
    assert ent_i[0].url == ent_s[0].url == "https://example.test/hook"
    assert list((ent_i[0].headers or {}).values()) == list((ent_s[0].headers or {}).values())


def test_nonstring_header_key_fails_closed_at_requests():
    """The coerced (non-string) header-key variant cannot FIRE: requests'
    header prep raises InvalidHeader on a non-string key, so the colliding
    HTTP subtree never dispatches a differential request (fail-closed).
    """
    from requests.exceptions import InvalidHeader
    from requests.models import PreparedRequest

    pr = PreparedRequest()
    with pytest.raises(InvalidHeader):
        pr.prepare(
            method="POST",
            url="http://127.0.0.1:9/",
            headers={1: "tokenval", "Content-Type": "application/json"},
            data="{}",
        )


# -- C. executor-read VALUE domain is injective (rebake on any fired change) --


@pytest.mark.parametrize(
    ("base", "mutated"),
    [
        # fired command string
        (
            "lifecycle:\n  post-install:\n    - run: echo GOOD\n",
            "lifecycle:\n  post-install:\n    - run: echo EVIL\n",
        ),
        # fired env VALUE (string key, only the value changes)
        (
            "lifecycle:\n  post-install:\n    - run: echo HI\n      env:\n        PATHX: /good\n",
            "lifecycle:\n  post-install:\n    - run: echo HI\n      env:\n        PATHX: /evil\n",
        ),
        # fired http url
        (
            "lifecycle:\n  post-install:\n    - type: http\n      url: https://good.test/h\n",
            "lifecycle:\n  post-install:\n    - type: http\n      url: https://evil.test/h\n",
        ),
        # fired header value
        (
            "lifecycle:\n  post-install:\n    - type: http\n      url: https://x.test/h\n"
            "      headers:\n        X-Tok: good\n",
            "lifecycle:\n  post-install:\n    - type: http\n      url: https://x.test/h\n"
            "      headers:\n        X-Tok: evil\n",
        ),
        # timeoutSec the executor reads
        (
            "lifecycle:\n  post-install:\n    - run: echo HI\n      timeoutSec: 5\n",
            "lifecycle:\n  post-install:\n    - run: echo HI\n      timeoutSec: 9\n",
        ),
    ],
)
def test_fired_value_change_rebakes_fingerprint(base, mutated):
    """SECURE INVARIANT: any change to a VALUE the executor actually runs MUST
    produce a different fingerprint (hash-domain covers the whole exec-domain).
    A break would be two distinct fired payloads sharing one fingerprint.
    """
    _ent_a, fp_a = parse_apm_yml_lifecycle_with_fingerprint(_proj(base), "project")
    _ent_b, fp_b = parse_apm_yml_lifecycle_with_fingerprint(_proj(mutated), "project")
    assert fp_a is not None and fp_b is not None
    assert fp_a != fp_b, "fired-value change did NOT rebake the fingerprint (exec-domain leak)"


# -- D. end-to-end: a trusted fingerprint never runs a CHANGED fired value ----


def test_trusted_fp_does_not_fire_changed_env_value(tmp_apm_home):
    """Trust a config whose script exports ``SECRET_SINK=/good``; then change
    ONLY the fired env VALUE to ``/evil`` (string key, no collision trick).
    The runner MUST drop the project script (fingerprint mismatch -> fail
    closed): a trusted record cannot fire a different env value.
    """
    yml = _proj(
        "lifecycle:\n  post-install:\n    - run: echo HI\n      env:\n        SECRET_SINK: /good\n"
    )
    proj_root = str(yml.parent)
    assert trust_project_scripts(yml) is not None

    r0 = _run_with_timeout(lambda: build_runner_from_context(project_root=proj_root))
    assert _kept_cmds(r0) == ["echo HI"], "trusted config should fire"

    yml.write_text(
        "lifecycle:\n  post-install:\n    - run: echo HI\n      env:\n        SECRET_SINK: /evil\n",
        encoding="utf-8",
    )
    r1 = _run_with_timeout(lambda: build_runner_from_context(project_root=proj_root))
    assert _kept_cmds(r1) == [], "changed fired env VALUE must revoke trust (fail closed)"


def test_envkey_collision_is_trust_neutral_no_changed_fire(tmp_apm_home):
    """End-to-end neutrality of the env-key collision: trust the str-key
    variant, then swap apm.yml to the bool-key variant (same fingerprint).
    Even though the fingerprint still 'matches', the kept script's fired
    command is byte-identical AND its only difference (a bool env key) cannot
    fire (it would crash in Popen). So the collision yields NO changed,
    successfully-firing payload -- it is trust-neutral, not a bypass.
    """
    body = "lifecycle:\n  post-install:\n    - run: echo SAFE_CMD\n      env:\n"
    yml = _proj(body + '        "true": /attacker\n')
    proj_root = str(yml.parent)
    fp_trusted = trust_project_scripts(yml)
    assert fp_trusted is not None

    # Swap to the bool-key variant (raw bytes differ, fingerprint collides).
    yml.write_text(body + "        true: /attacker\n", encoding="utf-8")
    _ent, fp_now = parse_apm_yml_lifecycle_with_fingerprint(yml, "project")
    assert fp_now == fp_trusted  # documented non-genuine collision
    assert is_fingerprint_trusted(yml, fp_now) is True

    runner = _run_with_timeout(lambda: build_runner_from_context(project_root=proj_root))
    kept = runner.scripts_for_event("post-install")
    # The fired command is identical to what was trusted; the only delta is a
    # non-string env key that fails closed at execution -> no payload change.
    assert [s.effective_command for s in kept] == ["echo SAFE_CMD"]
    assert list((kept[0].env or {}).keys()) == [True]


# -- E. cross-file: a trusted fingerprint is bound to its resolved path -------


def test_fingerprint_not_reusable_across_distinct_files(tmp_apm_home):
    """Identical lifecycle content in TWO distinct project files yields the
    same content fingerprint, but trust is keyed by RESOLVED path: trusting
    file A must NOT pre-trust file B (no trusted-fp-runs-other-file reuse).
    """
    text = "lifecycle:\n  post-install:\n    - run: echo SAME\n"
    yml_a = _proj(text)
    yml_b = _proj(text)

    fp_a = trust_project_scripts(yml_a)
    assert fp_a is not None
    # Same content -> same fingerprint, by design.
    assert fingerprint_lifecycle_subtree({"post-install": [{"run": "echo SAME"}]}) is not None
    # But B (distinct resolved path) is NOT trusted by A's record.
    _ent_b, fp_b = parse_apm_yml_lifecycle_with_fingerprint(yml_b, "project")
    assert fp_b == fp_a
    assert is_fingerprint_trusted(yml_b, fp_b) is False, "trust must not leak across files"
    r_b = _run_with_timeout(lambda: build_runner_from_context(project_root=str(yml_b.parent)))
    assert r_b.scripts_for_event("post-install") == []
