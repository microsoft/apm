"""Tests for ``apm audit --ci --policy`` CLI integration."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.audit import audit
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy

# -- Fixtures -------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _setup_clean_project(project: Path) -> None:
    """Create a project that passes all baseline + default policy checks."""
    apm_yml = textwrap.dedent("""\
        name: test-project
        version: '1.0.0'
        dependencies:
          apm:
            - owner/repo#v1.0.0
    """)
    lockfile = textwrap.dedent("""\
        lockfile_version: '1'
        generated_at: '2025-01-01T00:00:00Z'
        dependencies:
          - repo_url: owner/repo
            resolved_ref: v1.0.0
            deployed_files:
              - .github/prompts/test.md
    """)
    (project / "apm.yml").write_text(apm_yml, encoding="utf-8")
    (project / "apm.lock.yaml").write_text(lockfile, encoding="utf-8")
    package = project / "apm_modules" / "owner" / "repo"
    package.mkdir(parents=True)
    (package / "apm.yml").write_text(
        "name: repo\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    prompts_dir = project / ".github" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "test.md").write_text("Clean content\n", encoding="utf-8")


def _write_policy_file(project: Path, **overrides) -> Path:
    """Write a minimal policy file and return its path."""
    import yaml

    data = {
        "name": "test-policy",
        "version": "1.0.0",
        "enforcement": "block",
    }
    data.update(overrides)
    policy_path = project / "apm-policy.yml"
    policy_path.write_text(yaml.dump(data), encoding="utf-8")
    return policy_path


# -- Tests ----------------------------------------------------------


class TestCiWithPolicyFlag:
    def test_ci_with_policy_file(self, runner, tmp_path, monkeypatch):
        """--ci --policy <file> runs both baseline and policy checks."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        policy_path = _write_policy_file(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "--no-drift", "--policy", str(policy_path)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0

    def test_ci_with_policy_json_output(self, runner, tmp_path, monkeypatch):
        """JSON output includes both baseline + policy checks."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        policy_path = _write_policy_file(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "--no-drift", "--policy", str(policy_path), "-f", "json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        # Baseline: up to 7 checks, Policy: 17 checks -> total > 7 when
        # policy evaluation actually ran.  Asserting > 7 (not > 6) catches
        # the regression where only baseline checks are returned.
        assert data["summary"]["total"] > 7

    def test_ci_with_policy_deny_fails(self, runner, tmp_path, monkeypatch):
        """Policy deny list causing failure -> exit 1."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        import yaml

        policy_data = {
            "name": "strict-policy",
            "version": "1.0.0",
            "enforcement": "block",
            "dependencies": {"deny": ["owner/*"]},
        }
        policy_path = tmp_path / "strict-policy.yml"
        policy_path.write_text(yaml.dump(policy_data), encoding="utf-8")

        result = runner.invoke(
            audit,
            ["--ci", "--no-drift", "--policy", str(policy_path)],
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    def test_require_hashes_stays_fail_closed_in_warn_mode(
        self,
        runner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Integrity hash failures are not downgraded by warn enforcement."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        policy_path = _write_policy_file(
            tmp_path,
            enforcement="warn",
            security={"integrity": {"require_hashes": True}},
        )

        result = runner.invoke(
            audit,
            [
                "--ci",
                "--no-drift",
                "--no-fail-fast",
                "--policy",
                str(policy_path),
                "--format",
                "json",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        hash_check = next(
            check for check in payload["checks"] if check["name"] == "dependency-content-hashes"
        )
        assert hash_check["passed"] is False


class TestCiWithPolicyOrg:
    def test_ci_policy_org_discovery(self, runner, tmp_path, monkeypatch):
        """--ci --policy org triggers discover_policy with 'org' override."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        mock_result = PolicyFetchResult(policy=ApmPolicy(), source="org:test/.github")

        with patch(
            "apm_cli.policy.discovery.discover_policy", return_value=mock_result
        ) as mock_disc:
            result = runner.invoke(
                audit,
                ["--ci", "--no-drift", "--policy", "org"],
                catch_exceptions=False,
            )
            mock_disc.assert_called_once()
            call_kwargs = mock_disc.call_args
            assert call_kwargs.kwargs.get("policy_override") == "org"
            assert result.exit_code == 0


class TestCiPolicyNotFound:
    def test_policy_not_found_still_runs_baseline(self, runner, tmp_path, monkeypatch):
        """If policy fetch returns not-found (no error), baseline runs alone."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        mock_result = PolicyFetchResult(error=None, policy=None)

        with patch("apm_cli.policy.discovery.discover_policy", return_value=mock_result):
            result = runner.invoke(
                audit,
                ["--ci", "--no-drift", "--policy", "org"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0


class TestCiPolicyFetchError:
    def test_fetch_error_exits_1(self, runner, tmp_path, monkeypatch):
        """If policy fetch has an error AND project opts in to fail-closed, exit 1."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        # #829: post-warn-default behaviour requires opting in via
        # policy.fetch_failure_default=block to fail closed on fetch error.
        (tmp_path / "apm.yml").write_text(
            "name: test-project\nversion: '1.0.0'\n"
            "dependencies:\n  apm:\n    - owner/repo#v1.0.0\n"
            "policy:\n  fetch_failure_default: block\n",
            encoding="utf-8",
        )

        mock_result = PolicyFetchResult(error="Network timeout")

        with patch("apm_cli.policy.discovery.discover_policy", return_value=mock_result):
            result = runner.invoke(
                audit,
                ["--ci", "--no-drift", "--policy", "org"],
                catch_exceptions=False,
            )
            assert result.exit_code == 1
            assert "Policy fetch failed" in result.output


class TestNoCacheFlag:
    def test_no_cache_flag_accepted(self, runner, tmp_path, monkeypatch):
        """--no-cache flag is accepted and passed to discover_policy."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        mock_result = PolicyFetchResult(policy=ApmPolicy(), source="org:test/.github")

        with patch(
            "apm_cli.policy.discovery.discover_policy", return_value=mock_result
        ) as mock_disc:
            result = runner.invoke(
                audit,
                ["--ci", "--no-drift", "--policy", "org", "--no-cache"],
                catch_exceptions=False,
            )
            mock_disc.assert_called_once()
            assert mock_disc.call_args.kwargs.get("no_cache") is True
            assert result.exit_code == 0

    def test_no_cache_without_policy(self, runner, tmp_path, monkeypatch):
        """--no-cache without --policy doesn't error (just ignored)."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "--no-drift", "--no-cache"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0


class TestCiWithoutPolicy:
    def test_baseline_only(self, runner, tmp_path, monkeypatch):
        """--ci without --policy runs baseline only."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        result = runner.invoke(
            audit,
            ["--ci", "--no-drift", "-f", "json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        # Auto-discovery emits a [!] warning to stderr when no git remote is
        # found; JSON is on stdout. Read stdout explicitly so the warning does
        # not corrupt JSON parsing.
        data = json.loads(result.stdout)
        # Only the nine baseline checks, including deployment ownership.
        assert data["summary"]["total"] == 9


# -- #3195: explicit --policy fails closed when it cannot be loaded -------


def _invoke_ci(runner, *args: str):
    return runner.invoke(audit, ["--ci", "--no-drift", *args], catch_exceptions=False)


def _assert_explicit_load_failure(result, source: str) -> None:
    import re
    from urllib.parse import urlparse

    assert result.exit_code == 1, result.output
    match = re.search(r"^\[x\] Policy '([^']+)' could not be loaded:", result.stderr, re.M)
    assert match is not None, result.stderr
    printed = match.group(1)
    if "://" in source:
        expected, actual = urlparse(source), urlparse(printed)
        assert (actual.scheme, actual.hostname, actual.path) == (
            expected.scheme,
            expected.hostname,
            expected.path,
        )
    else:
        assert printed == source
    assert "--policy was passed explicitly" in result.stderr
    assert "enforcement skipped" not in result.stderr
    # Fail-closed exits before any report; stdout must not claim a pass.
    assert "passed" not in result.stdout


class TestExplicitPolicyLoadFailureFailsClosed:
    """An explicit ``--policy <source>`` that cannot be loaded exits 1."""

    def test_unreachable_url_exits_1(self, runner, tmp_path, monkeypatch):
        import requests

        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        source = "https://policy.invalid/apm-policy.yml"

        with patch(
            "apm_cli.policy.discovery.requests.get",
            side_effect=requests.exceptions.ConnectionError(),
        ):
            result = _invoke_ci(runner, "--policy", source, "--no-cache")

        _assert_explicit_load_failure(result, source)
        assert "Connection error fetching" in result.stderr

    def test_url_404_exits_1(self, runner, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        source = "https://policy.invalid/apm-policy.yml"
        resp = MagicMock(status_code=404)

        with patch("apm_cli.policy.discovery.requests.get", return_value=resp):
            result = _invoke_ci(runner, "--policy", source, "--no-cache")

        _assert_explicit_load_failure(result, source)
        assert "404: Policy file not found" in result.stderr

    def test_garbage_url_response_exits_1(self, runner, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        source = "https://policy.invalid/apm-policy.yml"
        resp = MagicMock(status_code=200, text="<html>captive portal</html>")

        with patch("apm_cli.policy.discovery.requests.get", return_value=resp):
            result = _invoke_ci(runner, "--policy", source, "--no-cache")

        _assert_explicit_load_failure(result, source)
        assert "is not a YAML mapping" in result.stderr

    @pytest.mark.parametrize("relative", [True, False])
    def test_missing_local_file_exits_1_without_remote_lookup(
        self, runner, tmp_path, monkeypatch, relative
    ):
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        source = "./missing-policy.yml" if relative else str(tmp_path / "missing-policy.yml")

        with patch("apm_cli.policy.discovery._fetch_github_contents") as remote:
            result = _invoke_ci(runner, "--policy", source)

        remote.assert_not_called()
        _assert_explicit_load_failure(result, source)
        assert f"Policy file not found: {source}" in result.stderr

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("not: [valid\n", "YAML parse error"),
            ("- a\n- b\n", "Policy must be a YAML mapping"),
            ("enforcement: bogus\n", "enforcement must be one of"),
        ],
        ids=["garbage-yaml", "not-a-mapping", "schema-invalid"],
    )
    def test_malformed_local_policy_exits_1(self, runner, tmp_path, monkeypatch, content, expected):
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        (tmp_path / "bad-policy.yml").write_text(content, encoding="utf-8")

        result = _invoke_ci(runner, "--policy", "bad-policy.yml")

        _assert_explicit_load_failure(result, "bad-policy.yml")
        assert expected in result.stderr

    def test_repo_without_policy_exits_1(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        with patch(
            "apm_cli.policy.discovery._fetch_github_contents",
            return_value=(None, "404: Not Found"),
        ):
            result = _invoke_ci(runner, "--policy", "contoso/no-policy", "--no-cache")

        _assert_explicit_load_failure(result, "contoso/no-policy")
        assert "no apm-policy.yml found in repository 'contoso/no-policy'" in result.stderr

    def test_project_warn_default_does_not_reopen_explicit_failure(
        self, runner, tmp_path, monkeypatch
    ):
        """policy.fetch_failure_default=warn governs discovery, not --policy."""
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            apm_yml.read_text(encoding="utf-8") + "policy:\n  fetch_failure_default: warn\n",
            encoding="utf-8",
        )

        result = _invoke_ci(runner, "--policy", "./missing-policy.yml")

        _assert_explicit_load_failure(result, "./missing-policy.yml")

    def test_stale_but_valid_cached_policy_is_still_enforced(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        stale = PolicyFetchResult(
            policy=ApmPolicy(enforcement="block"),
            source="url:https://policy.invalid/apm-policy.yml",
            cached=True,
            cache_stale=True,
            fetch_error="Connection error",
            outcome="cached_stale",
        )

        with patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=stale):
            result = _invoke_ci(
                runner, "--policy", "https://policy.invalid/apm-policy.yml", "-f", "json"
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["policy"]["enforced"] is True
        assert payload["policy"]["enforcement"] == "block"
        assert payload["summary"]["total"] > 9

    def test_failure_message_redacts_url_credentials(self, runner, tmp_path, monkeypatch):
        import requests

        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        with patch(
            "apm_cli.policy.discovery.requests.get",
            side_effect=requests.exceptions.ConnectionError(),
        ):
            result = _invoke_ci(
                runner,
                "--policy",
                "https://user:s3cr3t-token@policy.invalid/apm-policy.yml?sig=leak",
                "--no-cache",
            )

        assert result.exit_code == 1, result.output
        assert "could not be loaded" in result.stderr
        assert "s3cr3t-token" not in result.stderr
        assert "sig=leak" not in result.stderr

    def test_policy_disable_env_warns_and_skips(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_POLICY_DISABLE", "1")
        _setup_clean_project(tmp_path)
        policy_path = _write_policy_file(tmp_path)

        result = _invoke_ci(runner, "--policy", str(policy_path), "-f", "json")

        assert result.exit_code == 0, result.output
        assert "APM_POLICY_DISABLE=1 disables policy enforcement" in result.stderr
        assert json.loads(result.stdout)["policy"]["enforced"] is False


class TestDiscoveryFetchFailureUnchanged:
    """Auto-discovery and ``--policy org`` keep the #829 warn default."""

    @pytest.mark.parametrize("args", [(), ("--policy", "org")], ids=["auto", "org"])
    def test_discovery_fetch_failure_still_warns_and_exits_0(
        self, runner, tmp_path, monkeypatch, args
    ):
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        failed = PolicyFetchResult(
            source="org:contoso/.github",
            outcome="cache_miss_fetch_fail",
            error="dns failure",
        )

        with patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=failed):
            result = _invoke_ci(runner, *args, "-f", "json")

        assert result.exit_code == 0, result.output
        assert "[!] Policy fetch failed: dns failure; enforcement skipped" in result.stderr
        payload = json.loads(result.stdout)
        assert payload["passed"] is True
        assert payload["policy"] == {"enforced": False, "source": None, "enforcement": None}


class TestPolicyEnforcementReport:
    """JSON and SARIF report whether a policy was actually enforced."""

    def test_json_reports_enforced_policy(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        policy_path = _write_policy_file(tmp_path)

        result = _invoke_ci(runner, "--policy", str(policy_path), "-f", "json")

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["policy"] == {
            "enforced": True,
            "source": f"file:{policy_path}",
            "enforcement": "block",
        }

    def test_json_reports_enforcement_off_as_not_enforced(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        policy_path = _write_policy_file(tmp_path, enforcement="off")

        result = _invoke_ci(runner, "--policy", str(policy_path), "-f", "json")

        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)["policy"]
        assert report["enforced"] is False
        assert report["enforcement"] == "off"

    def test_sarif_reports_policy_in_invocation_properties(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)
        policy_path = _write_policy_file(tmp_path)

        result = _invoke_ci(runner, "--policy", str(policy_path), "-f", "sarif")

        assert result.exit_code == 0, result.output
        sarif = json.loads(result.stdout)
        report = sarif["runs"][0]["invocations"][0]["properties"]["policy"]
        assert report["enforced"] is True
        assert report["enforcement"] == "block"

    def test_no_policy_reports_not_enforced(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _setup_clean_project(tmp_path)

        result = _invoke_ci(runner, "--no-policy", "-f", "json")

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["policy"]["enforced"] is False
