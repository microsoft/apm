from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

RATCHET_TEST_SCOPE = "fixture"

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_test_assertions.py"
RULE_AQ001 = "AQ001_constant_assertion"
RULE_AQ002 = "AQ002_broad_pytest_raises"
RULE_AQ003 = "AQ003_unobserved_outcome"
RULE_AQ004 = "AQ004_patches_owner_it_imports"
RULE_AQ006 = "AQ006_e2e_not_driving_installed_cli"
EMPTY_RULES = {RULE_AQ001: {}, RULE_AQ002: {}, RULE_AQ003: {}, RULE_AQ004: {}, RULE_AQ006: {}}


def _run(root: Path, baseline: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            "--baseline",
            str(baseline),
            *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_baseline(path: Path, rules: dict[str, dict[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": 1, "rules": {**EMPTY_RULES, **rules}},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_test(root: Path, source: str, name: str = "test_sample.py") -> Path:
    path = root / "tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    _track_tests(root)
    return path


def _track_tests(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "tests"], check=True)


def test_aq001_new_constant_assertion_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(root, "def test_sample():\n    assert True\n")
    baseline = root / "baseline.json"
    _write_baseline(baseline, EMPTY_RULES)

    result = _run(root, baseline)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "tests/test_sample.py:2" in result.stderr
    assert RULE_AQ001 in result.stderr
    assert "replace it with an assertion over observed output or state" in result.stderr


def test_aq002_new_broad_raises_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(
        root,
        "import pytest\n"
        "def test_sample():\n"
        "    with pytest.raises(Exception, match='boom'):\n"
        "        raise Exception('boom')\n",
    )
    baseline = root / "baseline.json"
    _write_baseline(baseline, EMPTY_RULES)

    result = _run(root, baseline)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "tests/test_sample.py:3" in result.stderr
    assert RULE_AQ002 in result.stderr
    assert "assert the narrow exception type" in result.stderr


def test_specific_exception_raises_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(
        root,
        "import pytest\n"
        "def test_sample():\n"
        "    with pytest.raises(ValueError):\n"
        "        raise ValueError\n",
    )
    baseline = root / "baseline.json"
    _write_baseline(baseline, EMPTY_RULES)

    result = _run(root, baseline)

    assert result.returncode == 0


def test_numeric_constants_are_not_aq001(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(
        root,
        "def test_zero():\n    assert 0\ndef test_one():\n    assert 1\n",
    )
    baseline = root / "baseline.json"
    _write_baseline(baseline, EMPTY_RULES)

    result = _run(root, baseline)

    assert result.returncode == 0


def test_new_file_debt_fails_against_zero_default(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(root, "def test_sample():\n    assert None\n", "test_new_debt.py")
    baseline = root / "baseline.json"
    _write_baseline(baseline, EMPTY_RULES)

    result = _run(root, baseline)

    assert result.returncode == 1
    assert "tests/test_new_debt.py:2" in result.stderr


def test_reduction_is_reported_as_stale_info_not_failure(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(root, "def test_sample():\n    assert 1 == 1\n")
    baseline = root / "baseline.json"
    _write_baseline(baseline, {RULE_AQ001: {"tests/test_sample.py": 1}})

    result = _run(root, baseline)

    assert result.returncode == 0
    assert result.stderr == ""
    assert (
        f"[i] stale assertion-quality baseline entry: tests/test_sample.py: {RULE_AQ001} "
        "reduced to 0 from 1"
    ) in result.stdout
    assert "uv run --frozen python scripts/check_test_assertions.py --update-baseline" in (
        result.stdout
    )


_COMPONENT = "import pytest\n\npytestmark = pytest.mark.component\n\n"
_E2E = "import pytest\n\npytestmark = pytest.mark.e2e\n\n"


def test_aq003_component_test_without_observable_assertion_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(
        root,
        _COMPONENT + "def test_mock_only(mocker):\n"
        "    client = mocker.Mock()\n"
        "    client.run()\n"
        "    client.run.assert_called_once_with()\n"
        "\n"
        "class TestUnittestStyle:\n"
        "    def test_return_value_only(self):\n"
        "        self.assertEqual(run(), 1)\n"
        "\n"
        "def test_observes_output(capsys, tmp_path):\n"
        "    out = capsys.readouterr().out\n"
        "    assert 'done' in out\n"
        "    assert (tmp_path / 'apm.lock.yaml').exists()\n",
    )
    baseline = root / "baseline.json"
    _write_baseline(baseline, {})

    result = _run(root, baseline)

    assert result.returncode == 1
    assert (
        f"{RULE_AQ003}: component/e2e test asserts no "
        "user-visible output, exit code or durable state (observed 2, allowed 0). Fix:"
    ) in result.stderr
    assert f"tests/test_sample.py:5, tests/test_sample.py:11: {RULE_AQ003}" in result.stderr
    assert "tests/test_sample.py:14" not in result.stderr


def test_aq004_patching_a_symbol_defined_in_the_imported_owner_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    owner = root / "src" / "apm_cli" / "owner.py"
    owner.parent.mkdir(parents=True)
    owner.write_text(
        "import requests\n\ndef fetch():\n    return 1\n\ndef run():\n    return fetch()\n",
        encoding="utf-8",
    )
    _write_test(
        root,
        "from unittest.mock import patch\n\n"
        "from apm_cli.owner import run\n\n"
        "def test_self_mock():\n"
        "    with patch('apm_cli.owner.fetch', return_value=2):\n"
        "        assert run() == 2\n\n"
        "def test_collaborator():\n"
        "    with patch('apm_cli.owner.requests'):\n"
        "        assert run() == 1\n",
    )
    baseline = root / "baseline.json"
    _write_baseline(baseline, {})

    result = _run(root, baseline)

    assert result.returncode == 1
    assert (
        f"[x] tests/test_sample.py:6: {RULE_AQ004}: test patches a symbol defined in an "
        "owner module it imports (observed 1, allowed 0). Fix: exercise the real owner"
    ) in result.stderr


def test_aq006_e2e_module_using_cli_runner_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(
        root,
        _E2E + "from click.testing import CliRunner\n\n"
        "def test_in_process():\n"
        "    result = CliRunner().invoke(None)\n"
        "    assert result.exit_code == 0\n",
    )
    _write_test(
        root,
        _E2E + "def test_installed(apm_binary):\n"
        "    import subprocess\n"
        "    result = subprocess.run([apm_binary, '--version'], check=False)\n"
        "    assert result.returncode == 0\n",
        "test_driving.py",
    )
    baseline = root / "baseline.json"
    _write_baseline(baseline, {})

    result = _run(root, baseline)

    assert result.returncode == 1
    assert (
        f"[x] tests/test_sample.py:1, tests/test_sample.py:1: {RULE_AQ006}: e2e module does "
        "not drive the installed CLI only (observed 2, allowed 0). Fix: run the installed apm"
    ) in result.stderr
    assert "test_driving.py" not in result.stderr


def test_update_refuses_growth_without_writing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(root, "def test_sample():\n    assert False\n")
    baseline = root / "baseline.json"
    _write_baseline(baseline, EMPTY_RULES)
    before = baseline.read_bytes()

    result = _run(root, baseline, "--update-baseline")

    assert result.returncode == 1
    assert "refusing to update assertion baseline with new debt" in result.stderr
    assert baseline.read_bytes() == before


def test_update_refuses_existing_count_growth_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _write_test(
        root,
        "def test_first():\n    assert True\ndef test_second():\n    assert False\n",
    )
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        {
            RULE_AQ001: {"tests/test_sample.py": 1},
            RULE_AQ002: {},
        },
    )
    before = baseline.read_bytes()

    result = _run(root, baseline, "--update-baseline")

    assert result.returncode == 1
    assert "observed 2, allowed 1" in result.stderr
    assert "refusing to update assertion baseline with new debt" in result.stderr
    assert baseline.read_bytes() == before


def test_tracked_symlink_inside_root_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    target = _write_test(root, "def test_sample():\n    assert True\n", "fixture.py")
    link = root / "tests" / "test_link.py"
    try:
        link.symlink_to(target.name)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, EMPTY_RULES)

    result = _run(root, baseline)

    assert result.returncode == 2
    assert "tracked Python path is a symlink: tests/test_link.py" in result.stderr


def test_symlink_outside_root_fails_containment_check(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    tests = root / "tests"
    tests.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    link = tests / "test_escape.py"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    _track_tests(root)
    baseline = root / "baseline.json"
    _write_baseline(baseline, EMPTY_RULES)

    result = _run(root, baseline)

    assert result.returncode == 2
    assert "tracked Python path resolves outside repository" in result.stderr


def test_update_writes_reduced_canonical_baseline(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(root, "def test_sample():\n    assert 1 == 1\n")
    baseline = root / "baseline.json"
    _write_baseline(
        baseline,
        {
            RULE_AQ001: {"tests/test_sample.py": 1},
            RULE_AQ002: {},
        },
    )
    expected = (
        json.dumps(
            {"schema_version": 1, "rules": EMPTY_RULES},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()

    result = _run(root, baseline, "--update-baseline")

    assert result.returncode == 0
    assert (
        "[+] updated assertion-quality baseline: removed 1 resolved occurrence(s)" in result.stdout
    )
    assert baseline.read_bytes() == expected


def test_malformed_baseline_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(root, "def test_sample():\n    assert 1 == 1\n")
    baseline = root / "baseline.json"
    baseline.write_text("{not-json", encoding="utf-8")

    result = _run(root, baseline)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "[x]" in result.stderr


def test_structurally_invalid_baseline_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(root, "def test_sample():\n    assert 1 == 1\n")
    baseline = root / "baseline.json"
    invalid_payloads = [
        {"schema_version": 2, "rules": EMPTY_RULES},
        {"schema_version": 1, "rules": {RULE_AQ001: {}}},
        {
            "schema_version": 1,
            "rules": {RULE_AQ001: {"tests/test_sample.py": -1}, RULE_AQ002: {}},
        },
        {
            "schema_version": 1,
            "rules": {
                RULE_AQ001: {"tests/test_sample.py": "1"},
                RULE_AQ002: {},
            },
        },
    ]

    for payload in invalid_payloads:
        baseline.write_text(json.dumps(payload), encoding="utf-8")
        result = _run(root, baseline)
        assert result.returncode == 2
        assert result.stdout == ""
        assert "[x]" in result.stderr


def test_provisional_baseline_requires_explicit_allow(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_test(root, "def test_sample():\n    assert 1 == 1\n")
    baseline = root / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "provisional": {
                    "basis_commit": "abc123",
                    "required_follow_up": "remeasure",
                },
                "rules": EMPTY_RULES,
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )

    final = _run(root, baseline)
    provisional = _run(root, baseline, "--allow-provisional")
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    del payload["provisional"]
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    finalized = _run(root, baseline)

    assert final.returncode == 2
    assert "provisional baseline is not allowed in final mode" in final.stderr
    assert provisional.returncode == 0
    assert provisional.stderr == ""
    assert finalized.returncode == 0
    assert finalized.stderr == ""
