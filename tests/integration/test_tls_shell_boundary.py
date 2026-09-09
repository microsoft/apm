"""Real shell and nested CLI proof of the documented trust-policy boundary."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import certifi
import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

from .test_tls_custom_ca import _TRUST_ENV_VARS

pytestmark = [pytest.mark.integration, pytest.mark.lifecycle_smoke]


def _command(args):
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


@pytest.mark.parametrize(
    "control", ["APM_DISABLE_TRUSTSTORE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"]
)
@pytest.mark.parametrize("native_node", [False, True])
def test_apm_run_shell_controls_apply_at_apm_boundary(
    tmp_path, apm_binary_path, control, native_node
):
    base_env = {key: value for key, value in os.environ.items() if key not in _TRUST_ENV_VARS}
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=base_env)
    project = isolated.work_root
    probe = project / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "import requests\n"
        f"names = {_TRUST_ENV_VARS!r}\n"
        "settings = {key: os.environ.get(key) for key in names}\n"
        "settings['verify'] = requests.Session().merge_environment_settings(\n"
        "    'https://localhost/', {}, False, None, None)['verify']\n"
        "Path(sys.argv[1]).write_text(json.dumps(settings), encoding='utf-8')\n",
        encoding="utf-8",
    )
    replacement = project / "replacement.pem"
    replacement.write_bytes(Path(certifi.where()).read_bytes())
    value = "1" if control == "APM_DISABLE_TRUSTSTORE" else str(replacement)
    assignment = (
        f'set "{control}={value}" && ' if os.name == "nt" else f"{control}={shlex.quote(value)} "
    )
    direct = _command([sys.executable, str(probe), "direct.json"])
    nested = _command([str(apm_binary_path), "run", "probe"])
    dump_yaml(
        {
            "name": "shell-tls-boundary",
            "version": "1.0.0",
            "description": "Trust policy is resolved before shell execution",
            "scripts": {
                "direct": assignment + direct,
                "nested": assignment + nested,
                "probe": _command([sys.executable, str(probe), "nested.json"]),
            },
        },
        project / "apm.yml",
    )
    overrides = {"APM_E2E_TESTS": "1", "APM_EXTRA_CA_BUNDLE": certifi.where()}
    if native_node:
        overrides["NODE_EXTRA_CA_CERTS"] = str(replacement)
    env = isolated.subprocess_env(overrides=overrides)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=30)
    runner.run_sequence(
        (("run", "direct"), ("run", "nested")),
        expected_returncodes=(0, 0),
        scenario_id=f"shell-{control}-{native_node}",
        cwd=project,
        env=env,
    )
    direct_env = json.loads((project / "direct.json").read_text(encoding="utf-8"))
    nested_env = json.loads((project / "nested.json").read_text(encoding="utf-8"))

    # A direct shell child sees the assignment after APM prepared its CA
    # mappings. Requests honors its native override; curl cannot outrank the
    # already-derived REQUESTS_CA_BUNDLE. Node keeps its independent setting.
    assert direct_env[control] == value
    assert direct_env["APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE"]
    if control == "REQUESTS_CA_BUNDLE":
        assert direct_env["verify"] == str(replacement)
        assert (
            direct_env["REQUESTS_CA_BUNDLE"]
            != direct_env["APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE"]
        )
    else:
        assert direct_env["verify"] == direct_env["REQUESTS_CA_BUNDLE"]
        assert (
            direct_env["REQUESTS_CA_BUNDLE"]
            == direct_env["APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE"]
        )
    if native_node:
        assert direct_env["NODE_EXTRA_CA_CERTS"] == str(replacement)
        assert direct_env["APM_NODE_EXTRA_CA_CERTS_IS_DERIVED_ADDITIVE"] is None
    else:
        assert direct_env["NODE_EXTRA_CA_CERTS"]
        assert (
            direct_env["NODE_EXTRA_CA_CERTS"]
            == direct_env["APM_NODE_EXTRA_CA_CERTS_IS_DERIVED_ADDITIVE"]
        )

    # The nested APM sees the controls before deriving its own child env,
    # removes only its inherited mappings, and preserves user-owned values.
    assert nested_env[control] == value
    assert nested_env["APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE"] is None
    assert nested_env["APM_NODE_EXTRA_CA_CERTS_IS_DERIVED_ADDITIVE"] is None
    assert nested_env["NODE_EXTRA_CA_CERTS"] == (str(replacement) if native_node else None)
    assert nested_env["verify"] == (
        True if control == "APM_DISABLE_TRUSTSTORE" else str(replacement)
    )
    if control != "REQUESTS_CA_BUNDLE":
        assert nested_env["REQUESTS_CA_BUNDLE"] is None
