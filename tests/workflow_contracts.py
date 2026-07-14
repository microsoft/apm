"""Shared semantic helpers for GitHub Actions workflow contracts."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import yaml

WorkflowNode = dict[str, Any]


def load_workflow(path: Path) -> WorkflowNode:
    """Parse a workflow while normalizing YAML 1.1's boolean ``on`` key."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"workflow root must be a mapping: {path}")
    if True in loaded and "on" not in loaded:
        loaded["on"] = loaded.pop(True)
    return loaded


def workflow_job(workflow: WorkflowNode, name: str) -> WorkflowNode:
    """Return one named job mapping."""
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or not isinstance(jobs.get(name), dict):
        raise AssertionError(f"workflow job not found: {name}")
    return jobs[name]


def workflow_step(job: WorkflowNode, name: str) -> WorkflowNode:
    """Return one named step mapping."""
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise AssertionError("workflow job steps must be a list")
    matches = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one workflow step named {name!r}")
    return matches[0]


def workflow_step_index(job: WorkflowNode, name: str) -> int:
    """Return the list index of one named step."""
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise AssertionError("workflow job steps must be a list")
    for index, step in enumerate(steps):
        if isinstance(step, dict) and step.get("name") == name:
            return index
    raise AssertionError(f"workflow step not found: {name}")


def effective_env(
    workflow: WorkflowNode,
    job: WorkflowNode,
    step: WorkflowNode,
) -> dict[str, Any]:
    """Merge workflow, job, and step environments using Actions precedence."""
    merged: dict[str, Any] = {}
    for node in (workflow, job, step):
        env = node.get("env", {})
        if not isinstance(env, dict):
            raise AssertionError("workflow env must be a mapping")
        merged.update(env)
    return merged


def shell_tokens(step: WorkflowNode) -> list[str]:
    """Tokenize a run step while treating commented commands as absent."""
    return [token for command in shell_commands(step) for token in command]


def shell_commands(step: WorkflowNode) -> list[list[str]]:
    """Tokenize each shell command after joining line continuations."""
    command = step.get("run")
    if not isinstance(command, str):
        raise AssertionError("workflow step must contain a run command")
    logical = command.replace("\\\n", " ")
    return [
        tokens
        for line in logical.splitlines()
        if (tokens := shlex.split(line, comments=True, posix=True))
    ]


def assert_exact_command(
    commands: list[list[str]],
    expected: list[str],
    *,
    label: str,
) -> None:
    """Require one complete shell command to match exactly."""
    if expected not in commands:
        raise AssertionError(f"{label} must contain exact command: {expected!r}")


def assert_unconditional(node: WorkflowNode, *, label: str) -> None:
    """Require a job or step to have no conditional gate."""
    if "if" in node:
        raise AssertionError(f"{label} must be unconditional, got if={node['if']!r}")
