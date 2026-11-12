"""Bounded behavioral mutations run only against disposable candidate copies.

The static M9 ownership mutation lives in test_architecture_gitlab_sparse_transport.
These component tests reuse the contract assertions rather than replacing Git
materialization with mutation-specific fake behavior.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path("src/apm_cli/deps/download_strategies.py")
UNIT_CONTRACT = "tests/unit/deps/test_gitlab_sparse_transport_contract.py"
GIT_CONTRACT = "tests/integration/test_gitlab_sparse_transport_contract.py"


@dataclass(frozen=True)
class Mutation:
    """One source mutation and the exact behavioral assertion that must kill it."""

    name: str
    method: str
    old: str
    new: str
    node: str
    assertion: str
    source: Path = SOURCE


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "M1-force-https",
        "_prepared_repo_url",
        "return requested_url",
        'return self.build_repo_url(repo_ref, dep_ref=dep_ref, use_ssh=False, token="")',
        f"{GIT_CONTRACT}::test_real_git_manifest_remote_bytes_and_single_fetch[main]",
        "manifest transport must reach the actual Git remote",
    ),
    Mutation(
        "M2a-drop-port",
        "_prepared_repo_url",
        "return requested_url",
        'return requested_url.replace(f":{dep_ref.port}", "")',
        f"{GIT_CONTRACT}::test_real_git_manifest_remote_bytes_and_single_fetch[main]",
        "manifest transport must reach the actual Git remote",
    ),
    Mutation(
        "M2b-drop-ssh-user",
        "_prepared_repo_url",
        "return requested_url",
        'return requested_url.replace(f"{dep_ref.ssh_user}@", "")',
        f"{GIT_CONTRACT}::test_real_git_manifest_remote_bytes_and_single_fetch[main]",
        "manifest transport must reach the actual Git remote",
    ),
    Mutation(
        "M3-unconditional-rest",
        "download_gitlab_file",
        "if rest_eligible:",
        "if True:",
        f"{UNIT_CONTRACT}::test_strict_ssh_failure_never_rest[with-pat]",
        "P3 strict SSH",
    ),
    Mutation(
        "M4-rewritten-https-authorizes-rest",
        "download_gitlab_file",
        "effective_url, host_info.api_base\n",
        "requested_url, host_info.api_base\n",
        f"{GIT_CONTRACT}::test_effective_local_rewrite_does_not_authorize_rest",
        "P8 effective rewrite must not authorize REST",
    ),
    Mutation(
        "M5-managed-token-in-ssh-child",
        "_run",
        "env=git_subprocess_env(self._git_env),",
        "env={**git_subprocess_env(self._git_env), "
        '"GITLAB_APM_PAT": os.environ.get("GITLAB_APM_PAT", "")},',
        f"{GIT_CONTRACT}::test_real_git_manifest_remote_bytes_and_single_fetch[main]",
        "P7 credentials",
        source=Path("src/apm_cli/deps/git_file_transport.py"),
    ),
    Mutation(
        "M6a-old-reuse-key",
        "_git_file_transport_key",
        """return (
            provider.kind,
            ref,
            normalize_repo_url(requested_url),
            normalize_repo_url(effective_url),
            auth_mode,
        )""",
        "return (dep_ref.host or default_host(), dep_ref.repo_url, ref, dep_ref.port)",
        f"{UNIT_CONTRACT}::test_prepared_identity_separates_checkouts[user]",
        "P10 distinct prepared identities must not reuse a checkout",
    ),
    Mutation(
        "M6b-omit-effective-url",
        "_git_file_transport_key",
        "            normalize_repo_url(effective_url),\n",
        "",
        f"{UNIT_CONTRACT}::test_prepared_identity_separates_checkouts[effective-mirror]",
        "P10 distinct prepared identities must not reuse a checkout",
    ),
    Mutation(
        "M6c-omit-auth-mode",
        "_git_file_transport_key",
        "            auth_mode,\n",
        "",
        f"{UNIT_CONTRACT}::test_prepared_identity_separates_checkouts[auth-mode]",
        "P10 distinct prepared identities must not reuse a checkout",
    ),
    Mutation(
        "M7a-security-fallback",
        "download_gitlab_file",
        "except GitFileTransportError as exc:",
        "except (GitFileTransportError, ValueError) as exc:",
        f"{UNIT_CONTRACT}::test_non_transport_failures_are_terminal[failure3]",
        "P6 non-transport failures must remain terminal",
    ),
    Mutation(
        "M7b-local-io-fallback",
        "download_gitlab_file",
        "except GitFileTransportError as exc:",
        "except (GitFileTransportError, OSError) as exc:",
        f"{UNIT_CONTRACT}::test_non_transport_failures_are_terminal[failure1]",
        "P6 non-transport failures must remain terminal",
    ),
    Mutation(
        "M8a-bypass-materialization",
        "_download_gitlab_file_via_git",
        "return transport.fetch_file(file_path)",
        'return b""',
        f"{GIT_CONTRACT}::test_real_git_manifest_remote_bytes_and_single_fetch[v1]",
        "P9 requested ref must materialize exact local Git bytes",
    ),
    Mutation(
        "M8b-ignore-requested-ref",
        "_download_gitlab_file_via_git",
        "                    ref,\n",
        '                    "main",\n',
        f"{GIT_CONTRACT}::test_real_git_manifest_remote_bytes_and_single_fetch[v1]",
        "P9 requested ref must materialize exact local Git bytes",
    ),
    Mutation(
        "M10-silent-protocol-switch",
        "download_gitlab_file",
        "                and previous_attempt is not None\n",
        "                and False\n",
        f"{UNIT_CONTRACT}::test_protocol_switch_warning_matches_executed_attempts[True-True-ssh]",
        "P11 warn exactly when the executed protocol changes",
    ),
)


def _mutate(candidate: Path, mutation: Mutation) -> None:
    """Require a unique executable seam and modify only the candidate copy."""
    path = candidate / mutation.source
    assert not path.samefile(ROOT / mutation.source), "Never mutate the active source checkout"
    source = path.read_text(encoding="utf-8")
    methods = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == mutation.method
    ]
    assert len(methods) == 1, f"{mutation.name}: missing/ambiguous mutation method"
    method = methods[0]
    lines = source.splitlines(keepends=True)
    body = "".join(lines[method.lineno - 1 : method.end_lineno])
    assert body.count(mutation.old) == 1, f"{mutation.name}: source seam drifted"
    mutated = (
        "".join(lines[: method.lineno - 1])
        + body.replace(mutation.old, mutation.new, 1)
        + "".join(lines[method.end_lineno :])
    )
    assert mutated != source, f"{mutation.name}: mutation made no change"
    compile(mutated, str(path), "exec")
    path.write_text(mutated, encoding="utf-8")


def _copy_candidate(destination: Path) -> None:
    """Copy bounded production/test inputs without caches or editable imports."""
    destination.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for directory in ("src", "tests/utils"):
        shutil.copytree(ROOT / directory, destination / directory, ignore=ignore)
    for relative in (
        "pyproject.toml",
        "tests/__init__.py",
        "tests/unit/__init__.py",
        "tests/unit/deps/__init__.py",
        "tests/integration/__init__.py",
        "tests/conftest.py",
        "tests/integration/conftest.py",
        UNIT_CONTRACT,
        GIT_CONTRACT,
    ):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)


def _run_contracts(
    candidate: Path, run_root: Path, nodes: tuple[str, ...]
) -> tuple[subprocess.CompletedProcess[str], list[ET.Element]]:
    """Run real pytest under isolated Git/config/network policy and read JUnit."""
    isolated = IsolatedApmEnvironment.create(run_root, base_env=os.environ)
    environment = isolated.subprocess_env(
        overrides={
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_ADDOPTS": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    environment["PYTHONPATH"] = os.pathsep.join(
        (environment["PYTHONPATH"], str(candidate / "src"), str(candidate))
    )
    report = run_root / "results.xml"
    bootstrap = (
        "from pathlib import Path; import apm_cli; "
        "assert Path(apm_cli.__file__).resolve().is_relative_to(Path.cwd() / 'src'), "
        "'mutation imported source outside candidate'; "
        "import pytest, sys; raise SystemExit(pytest.main(sys.argv[1:]))"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            bootstrap,
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-q",
            "--tb=short",
            f"--junitxml={report}",
            f"--basetemp={run_root / 'pytest-work'}",
            *nodes,
        ],
        cwd=candidate,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert report.is_file(), f"No JUnit evidence:\n{result.stdout}\n{result.stderr}"
    # The report is generated by the bounded local pytest child, not external XML.
    cases = list(ET.parse(report).iter("testcase"))  # noqa: S314
    assert cases, f"No collected behavioral assertions:\n{result.stdout}\n{result.stderr}"
    assert not any(case.find("error") is not None for case in cases), (
        f"Setup/import/collection errors do not kill mutants:\n{result.stdout}\n{result.stderr}"
    )
    assert not any(case.find("skipped") is not None for case in cases), (
        f"Skipped assertions do not prove mutations:\n{result.stdout}"
    )
    return result, cases


@pytest.fixture(scope="module")
def passing_candidate(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Freeze and prove the integrated candidate before applying any mutation."""
    root = tmp_path_factory.mktemp("gitlab-mutation-baseline")
    candidate = root / "candidate"
    _copy_candidate(candidate)
    nodes = tuple(dict.fromkeys(mutation.node for mutation in MUTATIONS))
    assert len(MUTATIONS) == 14, "Expected 14 behavioral mutants plus the separate static M9"
    result, cases = _run_contracts(candidate, root / "baseline-run", nodes)
    assert result.returncode == 0, f"Unmutated candidate failed:\n{result.stdout}\n{result.stderr}"
    assert len(cases) == len(nodes), "Baseline did not execute every named assertion"
    assert all(case.find("failure") is None for case in cases), "Baseline assertions failed"
    yield candidate
    shutil.rmtree(root)


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda mutation: mutation.name)
def test_gitlab_sparse_behavioral_mutation(
    passing_candidate: Path, tmp_path: Path, mutation: Mutation
) -> None:
    """Count a kill only when the named behavioral assertion fails in JUnit."""
    candidate = tmp_path / "candidate"
    shutil.copytree(passing_candidate, candidate)
    original = (passing_candidate / mutation.source).read_bytes()
    _mutate(candidate, mutation)
    result, cases = _run_contracts(candidate, tmp_path / "mutant-run", (mutation.node,))
    assert (passing_candidate / mutation.source).read_bytes() == original, (
        "Baseline source was modified"
    )
    assert result.returncode == 1, (
        f"{mutation.name}: survived or infrastructure failed ({result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert len(cases) == 1, f"{mutation.name}: expected exactly one named behavioral assertion"
    case = cases[0]
    module, name = mutation.node.split("::", 1)
    assert case.attrib["classname"] == module.removesuffix(".py").replace("/", ".")
    assert case.attrib["name"] == name
    failure = case.find("failure")
    assert failure is not None, f"{mutation.name}: named assertion did not fail"
    evidence = failure.text or ""
    message = failure.attrib.get("message", "")
    assert message.startswith("AssertionError:") and mutation.assertion in message, (
        f"{mutation.name}: incidental failure is not a behavioral kill:\n{evidence}"
    )
