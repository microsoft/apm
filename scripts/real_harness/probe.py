#!/usr/bin/env python3
"""Real-harness probe: does a real Claude Code / Copilot CLI LOAD a plugin apm installs natively?

The lifecycle suite proves that ``apm install`` writes the native plugin files
and settings. This probe goes one step further and asks the real, pinned
harness binary whether it loads them, using non-model evidence wherever
possible:

* list commands (``claude plugin list``, ``copilot plugin|skill|mcp list``);
* the ``system/init`` event of ``claude -p --output-format stream-json``;
* side effects: the plugin's SessionStart hook appends to a marker file and
  the plugin's stdio MCP server records every JSON-RPC method it receives.

Claude Code never reaches a model: ``ANTHROPIC_BASE_URL`` points at a closed
local port, so a session starts (hooks fire, MCP servers connect, the init
event is emitted) and the first API call fails. Copilot CLI needs a real
token (``COPILOT_GITHUB_TOKEN``) to start a session; without one, the
session cells report BLOCKED.

Each cell runs under its own throwaway ``HOME`` so cells cannot see each
other's state. Usage::

    probe.py run --harness claude --out OUT [--env normal --scope project]
    probe.py summarize OUT
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLUGIN = "rh-probe"
SKILL = "rh-probe-skill"
MCP = "rh-probe-mcp"
ORIGIN = "https://github.com/rh-probe/consumer.git"
DEAD_ANTHROPIC = "http://127.0.0.1:9"
DUMMY_KEY = "sk-ant-api03-" + "x" * 80 + "RHPROBE0000000000000"
ENVS = ("normal", "worktree", "headless")
# Diagnostic variants: a second headless session in the same project, and the
# project marketplace path rewritten to an absolute path.
DIAG_ENVS = ("headless-rerun", "headless-abspath")
TOOL_PROMPT = (
    "Call the MCP tool rh_probe_tool exactly once, then reply with the exact text it returned."
)
SCOPES = ("user", "project")
HARNESSES = ("claude", "copilot")
SESSION_SECONDS = 45

_AGENT_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
_AGENT_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

# A real stdio MCP server: it appends every JSON-RPC method it receives to the
# marker file given as argv[2] and answers initialize + tools/list.
_PROBE_JS = r"""
const fs = require('fs');
const marker = process.argv[2];
fs.appendFileSync(marker, 'spawned\n');
let buffer = '';
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let index;
  while ((index = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, index);
    buffer = buffer.slice(index + 1);
    if (!line.trim()) continue;
    let message;
    try { message = JSON.parse(line); } catch (e) { continue; }
    if (message.method) fs.appendFileSync(marker, message.method + '\n');
    if (message.method === 'initialize') {
      process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: message.id, result: {
        protocolVersion: (message.params && message.params.protocolVersion) || '2024-11-05',
        capabilities: {tools: {}}, serverInfo: {name: 'rh-probe', version: '1.0.0'}}}) + '\n');
    } else if (message.method === 'tools/list') {
      process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: message.id,
        result: {tools: [{name: 'rh_probe_tool', description: 'probe',
        inputSchema: {type: 'object', properties: {}}}]}}) + '\n');
    } else if (message.method === 'tools/call') {
      process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: message.id,
        result: {content: [{type: 'text', text: 'RH_PROBE_TOOL_OK'}]}}) + '\n');
    } else if (message.id !== undefined && message.method) {
      process.stdout.write(JSON.stringify({jsonrpc: '2.0', id: message.id, result: {}}) + '\n');
    }
  }
});
"""

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\r")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _json(path: Path, payload: object) -> None:
    _write(path, json.dumps(payload, indent=2) + "\n")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _tail(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "...<truncated>...\n" + text[-limit:]


def _text(value: object) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value or "")


class Cell:
    """One (harness, env, scope) probe under an isolated HOME."""

    def __init__(self, out: Path, harness: str, env: str, scope: str) -> None:
        self.harness, self.env, self.scope = harness, env, scope
        self.id = f"{harness}-{env}-{scope}"
        self.root = (out / self.id).resolve()
        if self.root.exists():
            shutil.rmtree(self.root)
        self.home = self.root / "home"
        self.markers = self.root / "markers"
        self.logs = self.root / "logs"
        self.project = self.root / "work" / "consumer"
        self.worktree = self.root / "work" / "consumer-wt"
        for path in (self.home, self.markers, self.logs, self.project):
            path.mkdir(parents=True)
        self.hook_marker = self.markers / "hook.txt"
        self.mcp_marker = self.markers / "mcp.txt"
        self.steps: list[dict] = []
        self.evidence: dict = {}
        # A Claude-format plugin loads in Copilot only at project scope; an
        # Agent Plugin is the Copilot user-scope format (native_plugins/scopes.py).
        self.agent_plugin = harness == "copilot" and scope == "user"

    # -- process plumbing ---------------------------------------------------

    def base_env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "COLUMNS": "200",
            "LINES": "50",
            "GIT_AUTHOR_NAME": "rh",
            "GIT_AUTHOR_EMAIL": "rh@example.com",
            "GIT_COMMITTER_NAME": "rh",
            "GIT_COMMITTER_EMAIL": "rh@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
            "NO_UPDATE_NOTIFIER": "1",
            "CI": "true",
        }
        if self.harness == "claude":
            env.update(
                ANTHROPIC_API_KEY=DUMMY_KEY,
                ANTHROPIC_BASE_URL=DEAD_ANTHROPIC,
                DISABLE_AUTOUPDATER="1",
                DISABLE_TELEMETRY="1",
                DISABLE_ERROR_REPORTING="1",
            )
        else:
            # Pin the npm-installed version: never run a newer downloaded CLI.
            env["COPILOT_AUTO_UPDATE"] = "false"
        return env

    def run(
        self,
        label: str,
        argv: list[str],
        cwd: Path,
        *,
        extra_env: dict[str, str] | None = None,
        timeout: int = 180,
    ) -> subprocess.CompletedProcess:
        env = self.base_env()
        env.update(extra_env or {})
        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 -- fixed argv, pinned harness
                argv,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            proc = subprocess.CompletedProcess(argv, 124, _text(exc.stdout), _text(exc.stderr))
        except FileNotFoundError as exc:
            proc = subprocess.CompletedProcess(argv, 127, "", str(exc))
        record = {
            "label": label,
            "argv": argv,
            "cwd": str(cwd),
            "exit": proc.returncode,
            "seconds": round(time.monotonic() - started, 1),
        }
        log = self.logs / f"{len(self.steps):02d}-{label}.log"
        _write(log, f"{record}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}\n")
        self.steps.append(record)
        return proc

    def git(self, *argv: str) -> subprocess.CompletedProcess:
        return self.run("git-" + argv[0], ["git", *argv], self.project)

    # -- fixture ------------------------------------------------------------

    def write_fixture(self) -> Path:
        source = self.root / "src" / PLUGIN
        probe_js = source / "mcp" / "probe.js"
        _write(probe_js, _PROBE_JS)
        hook_cmd = f"echo SessionStart >> '{self.hook_marker}'"
        _json(
            source / "hooks" / "hooks.json",
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": hook_cmd}]}]}},
        )
        _write(
            source / "skills" / SKILL / "SKILL.md",
            f"---\nname: {SKILL}\ndescription: RH_PROBE_SKILL marker skill\n---\n\nReply RH_PROBE.\n",
        )
        if self.agent_plugin:
            _json(
                source / "plugin.json",
                {
                    "$schema": _AGENT_PLUGIN_SCHEMA,
                    "name": PLUGIN,
                    "version": "1.0.0",
                    "description": "real-harness probe",
                    "license": "MIT",
                },
            )
            _json(
                source / "mcp.json",
                {
                    "$schema": _AGENT_MCP_SCHEMA,
                    "mcpServers": {
                        MCP: {
                            "type": "stdio",
                            "command": "node",
                            "args": [str(probe_js), str(self.mcp_marker)],
                        }
                    },
                },
            )
        else:
            _json(
                source / ".claude-plugin" / "plugin.json",
                {"name": PLUGIN, "version": "1.0.0", "description": "real-harness probe"},
            )
            _json(
                source / ".mcp.json",
                {
                    "mcpServers": {
                        MCP: {
                            "command": "node",
                            "args": ["${CLAUDE_PLUGIN_ROOT}/mcp/probe.js", str(self.mcp_marker)],
                        }
                    }
                },
            )
        return source

    def seed_harness(self) -> None:
        trusted = [str(self.project), str(self.worktree)]
        if self.harness == "claude":
            _json(
                self.home / ".claude.json",
                {
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "bypassPermissionsModeAccepted": True,
                    "customApiKeyResponses": {"approved": [DUMMY_KEY[-20:]], "rejected": []},
                    "projects": {
                        path: {
                            "hasTrustDialogAccepted": True,
                            "hasCompletedProjectOnboarding": True,
                            "allowedTools": [],
                        }
                        for path in trusted
                    },
                },
            )
        else:
            _json(
                self.home / ".copilot" / "config.json",
                {"trustedFolders": trusted, "banner": "never"},
            )

    # -- apm ----------------------------------------------------------------

    def apm_install(self, plugin: Path) -> bool:
        apm = os.environ.get("APM_BIN", "apm")
        flags = ["-g"] if self.scope == "user" else []
        if self.scope == "project":
            _write(
                self.project / "apm.yml",
                f"name: consumer\nversion: 1.0.0\ntargets: [{self.harness}]\n"
                f"dependencies:\n  apm:\n    - {plugin}\n",
            )
            argv = [apm, "install", "--no-policy"]
        elif self.agent_plugin:
            # Agent Plugins refuse the imperative local-bundle route; declare them.
            _write(
                self.home / ".apm" / "apm.yml",
                f"name: user\nversion: 1.0.0\ndependencies:\n  apm:\n    - {plugin}\n",
            )
            argv = [apm, "install", "-g", "--no-policy", "--target", self.harness]
        else:
            argv = [apm, "install", "-g", "--no-policy", "--target", self.harness, str(plugin)]
        proc = self.run("apm-install", argv, self.project)
        output = proc.stdout + proc.stderr
        if "Parked" in output:
            match = re.search(r"Approve: (apm approve [^\n]+)", output)
            if not match:
                self.evidence["apm_error"] = "parked without an approve hint"
                return False
            self.run("apm-approve", [apm, *match.group(1).split()[1:]], self.project)
            again = [apm, "install", *flags, "--no-policy", "--target", self.harness]
            proc = self.run("apm-install-approved", again, self.project)
            output = proc.stdout + proc.stderr
            if "Parked" in output:
                self.evidence["apm_error"] = "still parked after approve"
                return False
        self.evidence["apm_install_tail"] = _tail(output, 2500)
        if proc.returncode != 0:
            self.evidence["apm_error"] = f"exit {proc.returncode}: " + _tail(output, 1500)
            return False
        return True

    # -- probes -------------------------------------------------------------

    def run_dir(self) -> Path:
        return self.worktree if self.env == "worktree" else self.project

    def marketplace(self) -> str:
        lock = _read(self.project / "apm.lock.yaml") + _read(self.home / ".apm" / "apm.lock.yaml")
        match = re.search(r"name: (apm-[pu]-[0-9a-f]+)", lock)
        return match.group(1) if match else ""

    def absolutize_project_marketplace(self) -> None:
        settings = self.project / ".claude" / "settings.json"
        data = json.loads(_read(settings))
        for entry in data.get("extraKnownMarketplaces", {}).values():
            entry["source"]["path"] = str(self.project / ".agents" / "plugins")
        _json(settings, data)

    def session_env(self) -> dict[str, str]:
        if self.harness == "copilot":
            env = {"COPILOT_ALLOW_ALL": "true"}
            token = os.environ.get("COPILOT_GITHUB_TOKEN")
            if token:
                env["COPILOT_GITHUB_TOKEN"] = token
            return env
        return {}

    def list_probes(self, cwd: Path) -> None:
        out: dict[str, str] = {}
        if self.harness == "claude":
            cmds = {
                "plugin_help": ["claude", "plugin", "--help"],
                "plugin_list": ["claude", "plugin", "list"],
                "marketplace_list": ["claude", "plugin", "marketplace", "list"],
                "mcp_list": ["claude", "mcp", "list"],
                "plugin_details": ["claude", "plugin", "details", f"{PLUGIN}@{self.marketplace()}"],
            }
        else:
            cmds = {
                "plugin_list": ["copilot", "plugin", "list"],
                "skill_list": ["copilot", "skill", "list"],
                "mcp_list": ["copilot", "mcp", "list"],
            }
        for key, argv in cmds.items():
            proc = self.run(f"list-{key}", argv, cwd, timeout=90)
            out[key] = _tail(_ANSI.sub("", proc.stdout + proc.stderr), 2500)
        self.evidence["lists"] = out

    def interactive(self, cwd: Path) -> None:
        """Start the TUI under a pty; Claude gets no input, Copilot gets one prompt."""
        typescript = self.logs / "interactive.typescript"
        if self.harness == "copilot" and not os.environ.get("COPILOT_GITHUB_TOKEN"):
            self.evidence["blocked"] = "no COPILOT_GITHUB_TOKEN for a Copilot session"
        limit = SESSION_SECONDS
        feed = f"sleep {limit + 15}"
        if self.harness == "copilot":
            limit = SESSION_SECONDS * 2
            # Copilot starts the session (and its hooks) on the first prompt.
            feed = f"{{ sleep 15; printf '%s\\r' '{TOOL_PROMPT}'; sleep {limit}; }}"
        wrapped = [
            "bash",
            "-c",
            f"{feed} | timeout -s INT {limit} script -qfec '{self.harness}' '{typescript}'",
        ]
        self.run("interactive", wrapped, cwd, extra_env=self.session_env(), timeout=limit + 60)
        self.evidence["screen_tail"] = _tail(_ANSI.sub("", _read(typescript)), 3000)

    def headless(self, cwd: Path) -> None:
        if self.harness == "claude":
            argv = [
                "claude",
                "-p",
                "Reply with the single word OK.",
                "--output-format",
                "stream-json",
                "--verbose",
                "--max-turns",
                "1",
            ]
            if self.env == "headless-abspath" and self.scope == "project":
                self.absolutize_project_marketplace()
            proc = self.run("headless", argv, cwd, timeout=SESSION_SECONDS + 30)
            if self.env == "headless-rerun":
                self.evidence["first_session_hook"] = _read(self.hook_marker)
                self.evidence["first_session_init_has_skill"] = SKILL in proc.stdout
                self.hook_marker.unlink(missing_ok=True)
                self.mcp_marker.unlink(missing_ok=True)
                proc = self.run("headless-2", argv, cwd, timeout=SESSION_SECONDS + 30)
            init = None
            for line in proc.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init":
                    init = event
                    break
            self.evidence["headless_tail"] = _tail(proc.stdout + proc.stderr, 2000)
            if init is not None:
                _json(self.logs / "init-event.json", init)
                keys = ("plugins", "mcp_servers", "skills", "slash_commands", "claude_code_version")
                self.evidence["init"] = {key: init.get(key) for key in keys}
            return
        if not os.environ.get("COPILOT_GITHUB_TOKEN"):
            self.evidence["blocked"] = "no COPILOT_GITHUB_TOKEN for copilot -p"
            return
        argv = ["copilot", "-p", TOOL_PROMPT, "--allow-all-tools", "--no-color"]
        proc = self.run("headless", argv, cwd, extra_env=self.session_env(), timeout=180)
        self.evidence["headless_tail"] = _tail(proc.stdout + proc.stderr, 2000)

    # -- verdict ------------------------------------------------------------

    def signals(self) -> dict:
        lists = self.evidence.get("lists", {})
        mcp = _read(self.mcp_marker).split()
        sig = {
            "hook_fired": "SessionStart" in _read(self.hook_marker),
            "mcp_initialized": "initialize" in mcp,
            "mcp_tools_listed": "tools/list" in mcp,
            "mcp_tool_called": "tools/call" in mcp,
            "plugin_listed": PLUGIN in lists.get("plugin_list", ""),
        }
        if self.harness == "claude":
            sig["details_listed"] = SKILL in lists.get("plugin_details", "")
        if self.harness == "copilot":
            sig["skill_listed"] = SKILL in lists.get("skill_list", "")
            reply = self.evidence.get("headless_tail", "") + self.evidence.get("screen_tail", "")
            sig["model_reply_tool_ok"] = "RH_PROBE_TOOL_OK" in reply
            sig["mcp_listed"] = MCP in lists.get("mcp_list", "")
        init = self.evidence.get("init")
        if init:
            sig["init_plugin"] = PLUGIN in json.dumps(init.get("plugins"))
            skills = json.dumps(init.get("skills")) + json.dumps(init.get("slash_commands"))
            sig["init_skill"] = SKILL in skills
            sig["init_mcp"] = MCP in json.dumps(init.get("mcp_servers"))
        return sig

    def verdict(self, sig: dict) -> tuple[str, str]:
        if self.evidence.get("apm_error"):
            return "BLOCKED", "apm install: " + self.evidence["apm_error"].splitlines()[0][:160]
        runtime = [sig["mcp_initialized"]]
        if not self.agent_plugin:
            runtime.append(sig["hook_fired"])
        if self.env.startswith("headless") and self.harness == "claude":
            runtime.append(bool(sig.get("init_skill")))
        if self.harness == "copilot":
            runtime.append(sig["skill_listed"])
        if all(runtime):
            return "LOADS", ", ".join(k for k, v in sig.items() if v)
        if self.evidence.get("blocked") and not any(runtime):
            return "BLOCKED", self.evidence["blocked"]
        missing = "missing: " + ", ".join(k for k, v in sig.items() if not v)
        return ("PARTIAL" if any(runtime) else "DOES NOT LOAD"), missing

    def harness_version(self) -> str:
        """The version the cell's own environment runs, read after the session."""
        proc = self.run("version", [self.harness, "--version"], self.project, timeout=60)
        text = (proc.stdout or proc.stderr).strip()
        return text.splitlines()[0] if text else "?"

    # -- driver -------------------------------------------------------------

    def execute(self) -> dict:
        plugin = self.write_fixture()
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", ORIGIN)
        _write(self.project / "README.md", "consumer\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.seed_harness()
        if self.apm_install(plugin):
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "apm install")
            if self.env == "worktree":
                self.git("worktree", "add", "-q", "-b", "wt", str(self.worktree))
            cwd = self.run_dir()
            self.list_probes(cwd)
            if self.env.startswith("headless"):
                self.headless(cwd)
            else:
                self.interactive(cwd)
        sig = self.signals()
        verdict, reason = self.verdict(sig)
        result = {
            "cell": self.id,
            "harness": self.harness,
            "env": self.env,
            "scope": self.scope,
            "fixture": "agent-plugin" if self.agent_plugin else "claude-plugin",
            "harness_version": self.harness_version(),
            "verdict": verdict,
            "reason": reason,
            "signals": sig,
            "evidence": self.evidence,
            "steps": self.steps,
            "hook_marker": _read(self.hook_marker),
            "mcp_marker": _read(self.mcp_marker),
        }
        _json(self.root / "cell.json", result)
        return result


def summarize(out: Path) -> str:
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.rglob("cell.json"))]
    lines = ["| cell | harness version | fixture | verdict | reason |", "|---|---|---|---|---|"]
    for row in rows:
        reason = row["reason"].replace("|", "/")
        lines.append(
            f"| `{row['cell']}` | {row['harness_version']} | {row['fixture']} "
            f"| **{row['verdict']}** | {reason} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-harness native plugin load probe")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--harness", choices=HARNESSES, required=True)
    run.add_argument("--env", choices=ENVS + DIAG_ENVS, action="append")
    run.add_argument("--scope", choices=SCOPES, action="append")
    run.add_argument("--out", type=Path, required=True)
    summ = sub.add_parser("summarize")
    summ.add_argument("out", type=Path)
    args = parser.parse_args()
    if args.command == "summarize":
        sys.stdout.write(summarize(args.out))
        return 0
    for env in args.env or ENVS:
        for scope in args.scope or SCOPES:
            result = Cell(args.out, args.harness, env, scope).execute()
            print(f"{result['cell']}: {result['verdict']} ({result['reason']})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
