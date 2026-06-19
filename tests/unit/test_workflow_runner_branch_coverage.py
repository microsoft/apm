"""Unit tests for apm_cli.workflow.runner.

Covers the near-zero coverage gap in runner.py (16.8% -> ~70%+).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workflow(
    name="my-workflow",
    content="Hello ${input:name}!",
    input_parameters=None,
    llm_model=None,
    validate_errors=None,
):
    wf = MagicMock()
    wf.name = name
    wf.content = content
    wf.input_parameters = input_parameters
    wf.llm_model = llm_model
    wf.validate.return_value = validate_errors or []
    return wf


# ---------------------------------------------------------------------------
# substitute_parameters
# ---------------------------------------------------------------------------


class TestSubstituteParameters:
    def test_single_substitution(self):
        from apm_cli.workflow.runner import substitute_parameters

        result = substitute_parameters("Hello ${input:name}!", {"name": "World"})
        assert result == "Hello World!"

    def test_multiple_substitutions(self):
        from apm_cli.workflow.runner import substitute_parameters

        result = substitute_parameters(
            "${input:a} and ${input:b}",
            {"a": "foo", "b": "bar"},
        )
        assert result == "foo and bar"

    def test_missing_param_leaves_placeholder(self):
        from apm_cli.workflow.runner import substitute_parameters

        result = substitute_parameters("Hello ${input:name}!", {})
        assert "${input:name}" in result

    def test_empty_content(self):
        from apm_cli.workflow.runner import substitute_parameters

        assert substitute_parameters("", {"a": "1"}) == ""

    def test_no_placeholders(self):
        from apm_cli.workflow.runner import substitute_parameters

        result = substitute_parameters("plain text", {"a": "1"})
        assert result == "plain text"

    def test_value_is_integer(self):
        from apm_cli.workflow.runner import substitute_parameters

        result = substitute_parameters("count=${input:n}", {"n": 42})
        assert result == "count=42"


# ---------------------------------------------------------------------------
# collect_parameters
# ---------------------------------------------------------------------------


class TestCollectParameters:
    def test_no_input_parameters_returns_provided(self):
        from apm_cli.workflow.runner import collect_parameters

        wf = _make_workflow(input_parameters=None)
        result = collect_parameters(wf, {"x": "1"})
        assert result == {"x": "1"}

    def test_empty_input_parameters_list_returns_provided(self):
        from apm_cli.workflow.runner import collect_parameters

        wf = _make_workflow(input_parameters=[])
        result = collect_parameters(wf, {"x": "1"})
        assert result == {"x": "1"}

    def test_all_params_provided_no_prompt(self):
        from apm_cli.workflow.runner import collect_parameters

        wf = _make_workflow(input_parameters=["name", "age"])
        result = collect_parameters(wf, {"name": "Alice", "age": "30"})
        assert result == {"name": "Alice", "age": "30"}

    def test_dict_input_parameters_all_provided(self):
        from apm_cli.workflow.runner import collect_parameters

        wf = _make_workflow(input_parameters={"name": "Your name", "city": "Your city"})
        result = collect_parameters(wf, {"name": "Bob", "city": "NYC"})
        assert result == {"name": "Bob", "city": "NYC"}

    def test_missing_param_prompts_user(self):
        from apm_cli.workflow.runner import collect_parameters

        wf = _make_workflow(input_parameters=["name"])
        with patch("builtins.input", return_value="Alice") as mock_input:
            with patch("builtins.print"):
                result = collect_parameters(wf, {})
        mock_input.assert_called_once()
        assert result["name"] == "Alice"

    def test_partial_params_only_prompts_missing(self):
        from apm_cli.workflow.runner import collect_parameters

        wf = _make_workflow(input_parameters=["a", "b"])
        with patch("builtins.input", return_value="val") as mock_input:
            with patch("builtins.print"):
                result = collect_parameters(wf, {"a": "existing"})
        mock_input.assert_called_once()
        assert result["a"] == "existing"
        assert result["b"] == "val"

    def test_provided_params_defaults_to_empty_dict(self):
        from apm_cli.workflow.runner import collect_parameters

        wf = _make_workflow(input_parameters=None)
        result = collect_parameters(wf)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# find_workflow_by_name
# ---------------------------------------------------------------------------


class TestFindWorkflowByName:
    def test_returns_none_when_not_found(self, tmp_path):
        from apm_cli.workflow.runner import find_workflow_by_name

        result = find_workflow_by_name("nonexistent", base_dir=str(tmp_path))
        assert result is None

    def test_uses_cwd_when_base_dir_none(self):
        from apm_cli.workflow.runner import find_workflow_by_name

        with patch("apm_cli.workflow.runner.discover_workflows", return_value=[]) as mock_discover:
            result = find_workflow_by_name("test")
        assert result is None
        mock_discover.assert_called_once()

    def test_finds_by_name_from_discovered(self, tmp_path):
        from apm_cli.workflow.runner import find_workflow_by_name

        wf = _make_workflow(name="target-workflow")
        with patch("apm_cli.workflow.runner.discover_workflows", return_value=[wf]):
            result = find_workflow_by_name("target-workflow", base_dir=str(tmp_path))
        assert result is wf

    def test_file_path_prompt_md_not_found_returns_none(self, tmp_path):
        from apm_cli.workflow.runner import find_workflow_by_name

        result = find_workflow_by_name("missing.prompt.md", base_dir=str(tmp_path))
        assert result is None

    def test_file_path_workflow_md_not_found_returns_none(self, tmp_path):
        from apm_cli.workflow.runner import find_workflow_by_name

        result = find_workflow_by_name("missing.workflow.md", base_dir=str(tmp_path))
        assert result is None

    def test_file_path_prompt_md_found_parses(self, tmp_path):
        from apm_cli.workflow.runner import find_workflow_by_name

        wf_file = tmp_path / "hello.prompt.md"
        wf_file.write_text("# Hello\n", encoding="utf-8")
        parsed_wf = _make_workflow(name="hello")

        with patch("apm_cli.workflow.parser.parse_workflow_file", return_value=parsed_wf):
            result = find_workflow_by_name(str(wf_file), base_dir=str(tmp_path))
        assert result is parsed_wf

    def test_file_path_parse_error_returns_none(self, tmp_path):
        from apm_cli.workflow.runner import find_workflow_by_name

        wf_file = tmp_path / "bad.prompt.md"
        wf_file.write_text("broken", encoding="utf-8")

        with patch(
            "apm_cli.workflow.parser.parse_workflow_file",
            side_effect=ValueError("parse error"),
        ):
            with patch("builtins.print"):
                result = find_workflow_by_name(str(wf_file), base_dir=str(tmp_path))
        assert result is None

    def test_relative_path_gets_joined(self, tmp_path):
        from apm_cli.workflow.runner import find_workflow_by_name

        wf_file = tmp_path / "rel.workflow.md"
        wf_file.write_text("# Workflow\n", encoding="utf-8")
        parsed_wf = _make_workflow(name="rel")

        with patch("apm_cli.workflow.parser.parse_workflow_file", return_value=parsed_wf):
            result = find_workflow_by_name("rel.workflow.md", base_dir=str(tmp_path))
        assert result is parsed_wf


# ---------------------------------------------------------------------------
# run_workflow
# ---------------------------------------------------------------------------


class TestRunWorkflow:
    def test_workflow_not_found(self, tmp_path):
        from apm_cli.workflow.runner import run_workflow

        ok, msg = run_workflow("nonexistent", base_dir=str(tmp_path))
        assert ok is False
        assert "not found" in msg

    def test_validation_errors(self, tmp_path):
        from apm_cli.workflow.runner import run_workflow

        wf = _make_workflow(validate_errors=["missing field"])
        with patch("apm_cli.workflow.runner.find_workflow_by_name", return_value=wf):
            ok, msg = run_workflow("some-wf", base_dir=str(tmp_path))
        assert ok is False
        assert "Invalid workflow" in msg

    def test_successful_execution(self, tmp_path):
        from apm_cli.workflow.runner import run_workflow

        wf = _make_workflow(content="Say hello", input_parameters=None)
        mock_runtime = MagicMock()
        mock_runtime.execute_prompt.return_value = "Hello!"

        with patch("apm_cli.workflow.runner.find_workflow_by_name", return_value=wf):
            with patch(
                "apm_cli.workflow.runner.RuntimeFactory.create_runtime",
                return_value=mock_runtime,
            ):
                ok, response = run_workflow("some-wf", params={}, base_dir=str(tmp_path))

        assert ok is True
        assert response == "Hello!"

    def test_runtime_execution_exception(self, tmp_path):
        from apm_cli.workflow.runner import run_workflow

        wf = _make_workflow(content="text")
        mock_runtime = MagicMock()
        mock_runtime.execute_prompt.side_effect = RuntimeError("LLM down")

        with patch("apm_cli.workflow.runner.find_workflow_by_name", return_value=wf):
            with patch(
                "apm_cli.workflow.runner.RuntimeFactory.create_runtime",
                return_value=mock_runtime,
            ):
                ok, msg = run_workflow("some-wf", params={}, base_dir=str(tmp_path))

        assert ok is False
        assert "Runtime execution failed" in msg

    def test_named_runtime_valid(self, tmp_path):
        from apm_cli.workflow.runner import run_workflow

        wf = _make_workflow(content="text")
        mock_runtime = MagicMock()
        mock_runtime.execute_prompt.return_value = "response"

        with patch("apm_cli.workflow.runner.find_workflow_by_name", return_value=wf):
            with patch(
                "apm_cli.workflow.runner.RuntimeFactory.runtime_exists",
                return_value=True,
            ):
                with patch(
                    "apm_cli.workflow.runner.RuntimeFactory.create_runtime",
                    return_value=mock_runtime,
                ):
                    ok, _resp = run_workflow(
                        "some-wf", params={"_runtime": "copilot"}, base_dir=str(tmp_path)
                    )
        assert ok is True

    def test_invalid_runtime_name(self, tmp_path):
        from apm_cli.workflow.runner import run_workflow

        wf = _make_workflow(content="text")
        mock_adapter = MagicMock()
        mock_adapter.is_available.return_value = True
        mock_adapter.get_runtime_name.return_value = "copilot"

        with patch("apm_cli.workflow.runner.find_workflow_by_name", return_value=wf):
            with patch(
                "apm_cli.workflow.runner.RuntimeFactory.runtime_exists",
                return_value=False,
            ):
                with patch(
                    "apm_cli.workflow.runner.RuntimeFactory._RUNTIME_ADAPTERS",
                    [mock_adapter],
                ):
                    ok, msg = run_workflow(
                        "some-wf", params={"_runtime": "badruntime"}, base_dir=str(tmp_path)
                    )
        assert ok is False
        assert "Invalid runtime" in msg

    def test_both_frontmatter_and_llm_flag_warns(self, tmp_path):
        from apm_cli.workflow.runner import run_workflow

        wf = _make_workflow(content="text", llm_model="gpt-4")
        mock_runtime = MagicMock()
        mock_runtime.execute_prompt.return_value = "ok"

        with patch("apm_cli.workflow.runner.find_workflow_by_name", return_value=wf):
            with patch(
                "apm_cli.workflow.runner.RuntimeFactory.create_runtime",
                return_value=mock_runtime,
            ):
                with patch("builtins.print") as mock_print:
                    ok, _ = run_workflow(
                        "some-wf",
                        params={"_llm": "claude-3"},
                        base_dir=str(tmp_path),
                    )
        assert ok is True
        # WARNING about conflict should have been printed
        warning_printed = any("WARNING" in str(call) for call in mock_print.call_args_list)
        assert warning_printed

    def test_params_defaults_to_empty(self, tmp_path):
        from apm_cli.workflow.runner import run_workflow

        mock_runtime = MagicMock()
        mock_runtime.execute_prompt.return_value = "resp"
        wf = _make_workflow(content="no params")

        with patch("apm_cli.workflow.runner.find_workflow_by_name", return_value=wf):
            with patch(
                "apm_cli.workflow.runner.RuntimeFactory.create_runtime",
                return_value=mock_runtime,
            ):
                ok, _resp = run_workflow("some-wf", params=None, base_dir=str(tmp_path))
        assert ok is True


# ---------------------------------------------------------------------------
# preview_workflow
# ---------------------------------------------------------------------------


class TestPreviewWorkflow:
    def test_workflow_not_found(self, tmp_path):
        from apm_cli.workflow.runner import preview_workflow

        ok, msg = preview_workflow("nonexistent", base_dir=str(tmp_path))
        assert ok is False
        assert "not found" in msg

    def test_validation_errors(self, tmp_path):
        from apm_cli.workflow.runner import preview_workflow

        wf = _make_workflow(validate_errors=["field missing"])
        with patch("apm_cli.workflow.runner.find_workflow_by_name", return_value=wf):
            ok, msg = preview_workflow("wf", base_dir=str(tmp_path))
        assert ok is False
        assert "Invalid workflow" in msg

    def test_successful_preview(self, tmp_path):
        from apm_cli.workflow.runner import preview_workflow

        wf = _make_workflow(content="Hello ${input:x}!")
        with patch("apm_cli.workflow.runner.find_workflow_by_name", return_value=wf):
            ok, content = preview_workflow("wf", params={"x": "World"}, base_dir=str(tmp_path))
        assert ok is True
        assert "World" in content

    def test_params_defaults_to_empty(self, tmp_path):
        from apm_cli.workflow.runner import preview_workflow

        wf = _make_workflow(content="No params here")
        with patch("apm_cli.workflow.runner.find_workflow_by_name", return_value=wf):
            ok, content = preview_workflow("wf", params=None, base_dir=str(tmp_path))
        assert ok is True
        assert content == "No params here"
