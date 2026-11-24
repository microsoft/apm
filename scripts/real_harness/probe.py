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
import base64
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
# Claude-only diagnostics at project scope: a second session in the same HOME, the
# marketplace path made absolute, no folder trust (headless and TUI), and the
# native oracle (Claude's own `plugin marketplace add` + `plugin install --scope
# project`, then the user plugin state wiped to simulate a fresh clone).
# Worktree diagnostics: apm installs only on the worktree's branch (main checkout
# has no .agents/plugins), or a session runs in the main checkout before the
# first session in a worktree.
DIAG_ENVS = (
    "tui-untrusted",
    "headless-rerun",
    "headless-abspath",
    "headless-untrusted",
    "headless-native",
    "headless-wt-branch",
    "headless-wt-after-main",
)
_AUTH_BLOCKERS = ("could not be validated", "rate limit exceeded", "No authentication")
TOOL_PROMPT = (
    "Call the MCP tool rh_probe_tool exactly once, then reply with the exact text it returned."
)
SCOPES = ("user", "project")
HARNESSES = ("claude", "copilot")
# #1120: two packages whose native plugins share a skill (and command) name.
# "distinct": different plugin names; "same": one plugin name from two packages.
COLLISION_ENVS = ("collision-distinct", "collision-same")
COLLIDING = "analyze"
COLLISION_MARKERS = {"rh-a": "RH_A_BODY", "rh-b": "RH_B_BODY"}
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
        self.install_root = self.project

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
                            "args": [str(self.deployed_probe()), str(self.mcp_marker)],
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

    def deployed_probe(self) -> Path:
        """The MCP server script inside the plugin copy apm deploys at user scope."""
        return self.home / ".agents" / "plugins" / PLUGIN / "mcp" / "probe.js"

    def seed_harness(self) -> None:
        trusted = [] if self.env.endswith("untrusted") else [str(self.project), str(self.worktree)]
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
                self.install_root / "apm.yml",
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
        proc = self.run("apm-install", argv, self.install_root)
        output = proc.stdout + proc.stderr
        if "Parked" in output:
            match = re.search(r"Approve: (apm approve [^\n]+)", output)
            if not match:
                self.evidence["apm_error"] = "parked without an approve hint"
                return False
            self.run("apm-approve", [apm, *match.group(1).split()[1:]], self.install_root)
            again = [apm, "install", *flags, "--no-policy", "--target", self.harness]
            proc = self.run("apm-install-approved", again, self.install_root)
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
        if self.env == "worktree" or self.env.startswith("headless-wt"):
            return self.worktree
        return self.project

    def marketplace(self) -> str:
        lock = _read(self.install_root / "apm.lock.yaml") + _read(
            self.home / ".apm" / "apm.lock.yaml"
        )
        match = re.search(r"name: (apm-[pu]-[0-9a-f]+)", lock)
        return match.group(1) if match else ""

    def native_oracle(self, cwd: Path) -> None:
        """Re-declare the deployed plugin with Claude's own commands, then forget user state."""
        settings = self.project / ".claude" / "settings.json"
        settings.unlink()
        name = self.marketplace()
        for label, argv in (
            ("native-mkt-help", ["claude", "plugin", "marketplace", "add", "--help"]),
            ("native-install-help", ["claude", "plugin", "install", "--help"]),
            (
                "native-mkt-add",
                [
                    "claude",
                    "plugin",
                    "marketplace",
                    "add",
                    "./.agents/plugins",
                    "--scope",
                    "project",
                ],
            ),
            (
                "native-install",
                ["claude", "plugin", "install", f"{PLUGIN}@{name}", "--scope", "project"],
            ),
        ):
            self.run(label, argv, cwd, timeout=120)
        self.evidence["native_settings"] = _read(settings)
        shutil.rmtree(self.home / ".claude" / "plugins", ignore_errors=True)

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

    def wait_for(self, done, timeout: float) -> bool:
        """Poll a marker predicate instead of sleeping a fixed time."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if done():
                return True
            time.sleep(1)
        return done()

    def mcp_methods(self) -> list[str]:
        return _read(self.mcp_marker).split()

    def interactive(self, cwd: Path) -> None:
        """Start the TUI in a detached tmux pane (a real terminal emulator).

        Waits on markers, not fixed sleeps: startup is done once the plugin's MCP
        server has listed its tools (and, for a Claude plugin, the SessionStart
        hook fired), or after SESSION_SECONDS. Claude then gets a typed (never
        submitted) slash command so the TUI lists the plugin skill. Copilot
        starts its session on the first prompt, so with a token it gets one
        prompt asking for the plugin tool.
        """
        token = os.environ.get("COPILOT_GITHUB_TOKEN")
        if self.harness == "copilot" and not token:
            self.evidence["blocked"] = "no COPILOT_GITHUB_TOKEN for a Copilot session"
        tmux = ["tmux", "-L", self.id]
        start = [*tmux, "new-session", "-d", "-x", "200", "-y", "50", self.harness]
        launched = self.run("tmux-start", start, cwd, extra_env=self.session_env(), timeout=30)
        self.evidence["tui_launched"] = launched.returncode == 0

        def pane() -> str:
            return self.run("tmux-capture", [*tmux, "capture-pane", "-p"], cwd, timeout=30).stdout

        def started() -> bool:
            # Copilot fires sessionStart hooks on the first prompt, not at startup.
            hook = self.harness == "copilot" or "SessionStart" in _read(self.hook_marker)
            return hook and "tools/list" in self.mcp_methods()

        self.evidence["startup_ready"] = self.wait_for(started, SESSION_SECONDS)
        if self.harness == "claude" and self.env.endswith("untrusted"):
            # Keystrokes could answer the folder-trust dialog; observe it untouched,
            # and record that it is actually on screen.
            self.evidence["trust_prompt_shown"] = "trust this folder" in pane().lower()
        elif self.harness == "claude":
            self.run("tmux-slash", [*tmux, "send-keys", "-l", f"/{SKILL}"], cwd, timeout=30)
            self.wait_for(lambda: pane().count(SKILL) > 1, 10)
            self.evidence["slash_menu"] = _tail(pane(), 2000)
            self.run("tmux-clear", [*tmux, "send-keys", "Escape", "C-u"], cwd, timeout=30)
        elif token:
            self.run("tmux-prompt", [*tmux, "send-keys", "-l", TOOL_PROMPT], cwd, timeout=30)
            time.sleep(1)
            self.run("tmux-enter", [*tmux, "send-keys", "Enter"], cwd, timeout=30)
            self.wait_for(
                lambda: "tools/call" in self.mcp_methods() and "RH_PROBE_TOOL_OK" in pane(),
                SESSION_SECONDS * 3,
            )
        screen = self.run("tmux-final", [*tmux, "capture-pane", "-p", "-S", "-300"], cwd)
        self.run("tmux-kill", [*tmux, "kill-server"], cwd, timeout=30)
        self.evidence["screen_tail"] = _tail(screen.stdout, 3000)

    def claude_session(self, label: str, cwd: Path) -> tuple[str, dict | None]:
        """Run ``claude -p`` and stop it once the plugin MCP server lists tools after init.

        The model call can never succeed (closed ANTHROPIC_BASE_URL), so waiting
        for the client's retries only burns time; hooks and MCP start by then.
        """
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
        log = self.logs / f"{len(self.steps):02d}-{label}.log"
        started = time.monotonic()
        with log.open("w", encoding="utf-8") as sink:
            proc = subprocess.Popen(  # noqa: S603 -- fixed argv, pinned harness
                argv,
                cwd=str(cwd),
                env=self.base_env(),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=sink,
            )
            seen = None
            while proc.poll() is None and time.monotonic() - started < SESSION_SECONDS + 30:
                if seen is None and '"subtype":"init"' in _read(log):
                    seen = time.monotonic()
                if seen is not None and (
                    "tools/list" in self.mcp_methods() or time.monotonic() - seen > 10
                ):
                    break
                time.sleep(1)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        output = _read(log)
        self.steps.append(
            {
                "label": label,
                "argv": argv,
                "cwd": str(cwd),
                "exit": proc.returncode,
                "seconds": round(time.monotonic() - started, 1),
            }
        )
        init = None
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                init = event
                break
        return output, init

    @staticmethod
    def plugin_paths(init: dict | None) -> list[str]:
        plugins = (init or {}).get("plugins") or []
        return [str(entry.get("path")) for entry in plugins if PLUGIN in str(entry)]

    def reset_markers(self) -> None:
        self.hook_marker.unlink(missing_ok=True)
        self.mcp_marker.unlink(missing_ok=True)

    def headless(self, cwd: Path) -> None:
        if self.harness == "claude":
            if self.env == "headless-abspath" and self.scope == "project":
                self.absolutize_project_marketplace()
            if self.env == "headless-native" and self.scope == "project":
                self.native_oracle(cwd)
            if self.env == "headless-wt-after-main":
                _output, init = self.claude_session("headless-main", self.project)
                self.evidence["main_session_plugin_paths"] = self.plugin_paths(init)
                self.reset_markers()
            output, init = self.claude_session("headless", cwd)
            if self.env == "headless-rerun":
                self.evidence["first_session_hook"] = _read(self.hook_marker)
                self.evidence["first_session_init_has_skill"] = SKILL in json.dumps(
                    (init or {}).get("skills")
                )
                self.reset_markers()
                output, init = self.claude_session("headless-2", cwd)
            self.evidence["headless_tail"] = _tail(output, 2000)
            if init is not None:
                _json(self.logs / "init-event.json", init)
                self.evidence["init_plugin_paths"] = self.plugin_paths(init)
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
            "list_mcp_spawned": "spawned" in self.evidence.get("list_mcp_marker", "").split(),
        }
        if self.harness == "claude":
            sig["details_listed"] = SKILL in lists.get("plugin_details", "")
            if "slash_menu" in self.evidence:
                # One occurrence is the typed text; a loaded skill adds a menu entry.
                sig["tui_skill"] = self.evidence["slash_menu"].count(SKILL) > 1
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
            paths = self.evidence.get("init_plugin_paths") or []
            sig["init_path_in_cwd"] = bool(paths) and all(
                str(path).startswith(f"{self.run_dir()}/") for path in paths
            )
        return sig

    def verdict(self, sig: dict) -> tuple[str, str]:
        if self.evidence.get("apm_error"):
            return "ERROR", "apm install: " + self.evidence["apm_error"].splitlines()[0][:160]
        if self.env == "tui-untrusted" and not (
            self.evidence.get("tui_launched") and self.evidence.get("trust_prompt_shown")
        ):
            # A crashed or absent TUI must not pass as proof of the trust gate.
            return "ERROR", "the TUI did not launch or did not show the folder-trust prompt"
        if self.env == "headless-untrusted" and "init" not in self.evidence:
            return "ERROR", "claude -p emitted no init event, so the session never started"
        runtime = [sig["mcp_initialized"], sig["mcp_tools_listed"]]
        if not self.agent_plugin:
            runtime.append(sig["hook_fired"])
        if self.env.startswith("headless") and self.harness == "claude":
            runtime.append(bool(sig.get("init_skill")))
        if self.env in ("normal", "worktree") and self.harness == "claude":
            runtime.append(bool(sig.get("tui_skill")))
        if self.env.startswith("headless-wt") and self.harness == "claude":
            runtime.append(bool(sig.get("init_path_in_cwd")))
        if self.harness == "copilot":
            runtime.append(sig["skill_listed"])
        if self.env.endswith("untrusted") and sig["list_mcp_spawned"] and not all(runtime):
            # No harness command may run plugin code in a folder it does not trust.
            return "PARTIAL", "plugin MCP server spawned by a list command without folder trust"
        if all(runtime):
            return "LOADS", ", ".join(k for k, v in sig.items() if v)
        output = self.evidence.get("headless_tail", "") + self.evidence.get("screen_tail", "")
        auth = next((m for m in _AUTH_BLOCKERS if m in output), None)
        if auth and not all(runtime):
            return "BLOCKED", f"harness auth failed ({auth}); partial: " + ", ".join(
                k for k, v in sig.items() if v
            )
        if self.evidence.get("blocked"):
            partial = ", ".join(k for k, v in sig.items() if v) or "none"
            return "BLOCKED", f"{self.evidence['blocked']}; partial: {partial}"
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
        if self.env == "headless-wt-branch":
            self.git("worktree", "add", "-q", "-b", "wt", str(self.worktree))
            self.install_root = self.worktree
        if self.apm_install(plugin):
            shutil.rmtree(plugin.parent)
            self.run("git-add", ["git", "add", "-A"], self.install_root)
            self.run("git-commit", ["git", "commit", "-q", "-m", "apm install"], self.install_root)
            if self.env in ("worktree", "headless-wt-after-main"):
                self.git("worktree", "add", "-q", "-b", "wt", str(self.worktree))
            cwd = self.run_dir()
            self.list_probes(cwd)
            # `claude mcp list` health-checks (spawns) MCP servers; session evidence
            # must come from the session alone.
            self.evidence["list_mcp_marker"] = _read(self.mcp_marker)
            self.reset_markers()
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


class CollisionCell(Cell):
    """#1120: two native plugins with the same inner skill (and command) name.

    Installs packages A and B, checks through the real harness that both skills
    load under distinguishable plugin namespaces, that both deployed plugin roots
    keep their source bytes, then uninstalls A and checks that B still loads.
    """

    def plugin_names(self) -> tuple[str, str]:
        return ("rh-a", "rh-b") if self.env == "collision-distinct" else ("rh-dup", "rh-dup")

    def write_package(self, label: str, name: str) -> Path:
        source = self.root / "src" / f"pkg-{label}"
        marker = f"RH_{label.upper()}_BODY"
        _write(
            source / "skills" / COLLIDING / "SKILL.md",
            f"---\nname: {COLLIDING}\ndescription: {marker} skill\n---\n\n{marker}\n",
        )
        if self.agent_plugin:
            _json(
                source / "plugin.json",
                {
                    "$schema": _AGENT_PLUGIN_SCHEMA,
                    "name": name,
                    "version": "1.0.0",
                    "description": f"collision fixture {label}",
                    "license": "MIT",
                },
            )
        else:
            _json(
                source / ".claude-plugin" / "plugin.json",
                {"name": name, "version": "1.0.0", "description": f"collision fixture {label}"},
            )
            _write(
                source / "commands" / f"{COLLIDING}.md",
                f"---\ndescription: {marker} command\n---\n\n{marker}\n",
            )
        return source

    def manifest(self, packages: list[Path]) -> Path:
        deps = "".join(f"    - {pkg}\n" for pkg in packages)
        if self.scope == "project":
            path = self.project / "apm.yml"
            _write(
                path,
                f"name: consumer\nversion: 1.0.0\ntargets: [{self.harness}]\n"
                f"dependencies:\n  apm:\n{deps}",
            )
        else:
            path = self.home / ".apm" / "apm.yml"
            _write(path, f"name: user\nversion: 1.0.0\ndependencies:\n  apm:\n{deps}")
        return path

    def apm(self, label: str, *argv: str) -> subprocess.CompletedProcess:
        apm = os.environ.get("APM_BIN", "apm")
        flags = ["-g"] if self.scope == "user" else []
        return self.run(label, [apm, *argv[:1], *flags, *argv[1:]], self.project)

    def plugins_root(self) -> Path:
        base = self.home if self.scope == "user" else self.project
        return base / ".agents" / "plugins"

    def deployed_roots(self) -> list[Path]:
        base = self.home / ".apm" if self.scope == "user" else self.project
        lock = _read(base / "apm.lock.yaml")
        roots = re.findall(r"native_plugin:\n\s+name: \S+\n\s+root: (\S+)", lock)
        anchor = self.home if self.scope == "user" else self.project
        return [anchor / root for root in roots]

    def bytes_intact(self, source: Path) -> bool:
        """Some deployed native plugin root holds exactly the source's files and bytes."""
        want = {
            str(f.relative_to(source)): f.read_bytes() for f in source.rglob("*") if f.is_file()
        }
        for root in self.deployed_roots():
            have = {
                str(f.relative_to(root)): f.read_bytes() for f in root.rglob("*") if f.is_file()
            }
            if have == want:
                return True
        return False

    def observe(self, label: str) -> dict:
        """What the harness loads now: qualified skill names per plugin."""
        cwd = self.project
        if self.harness == "claude":
            _output, init = self.claude_session(f"session-{label}", cwd)
            skills = (init or {}).get("skills") or []
            commands = (init or {}).get("slash_commands") or []
            plugins = [str(p.get("source")) for p in (init or {}).get("plugins") or []]
            return {
                "skills": sorted(s for s in skills if COLLIDING in s),
                "commands": sorted(c for c in commands if COLLIDING in c),
                "plugins": sorted(p for p in plugins if p.startswith("rh-")),
            }
        out = {}
        for key, argv in (
            ("plugin_list", ["copilot", "plugin", "list"]),
            ("skill_list", ["copilot", "skill", "list"]),
        ):
            proc = self.run(f"{key}-{label}", argv, cwd, timeout=90)
            out[key] = _tail(_ANSI.sub("", proc.stdout + proc.stderr), 3000)
        lines = out["skill_list"].splitlines()
        out["skills"] = [line.strip() for line in lines if COLLIDING in line]
        out["plugins"] = sorted(set(re.findall(r"rh-[a-z]+", out["plugin_list"])))
        return out

    def loads(self, seen: dict, names: set[str]) -> bool:
        """Each named plugin contributes a distinguishable `analyze` skill (and command)."""
        if self.harness == "claude":
            return all(
                f"{name}:{COLLIDING}" in seen["skills"]
                and f"{name}:{COLLIDING}" in seen["commands"]
                for name in names
            )
        # Copilot qualifies a skill name only while it is ambiguous, so each
        # package's skill is identified by the body marker in its description.
        joined = "\n".join(seen["skills"])
        return all(name in seen["plugins"] and COLLISION_MARKERS[name] in joined for name in names)

    def gone(self, seen: dict, name: str) -> bool:
        """No trace of the plugin: not registered, no skill, no command."""
        if self.harness == "claude":
            qualified = f"{name}:{COLLIDING}"
            registered = any(p.startswith(f"{name}@") for p in seen["plugins"])
            return not registered and qualified not in seen["skills"] + seen["commands"]
        joined = "\n".join(seen["skills"])
        return name not in seen["plugins"] and COLLISION_MARKERS[name] not in joined

    def settings_untouched(self) -> bool:
        """No harness settings file declares the refused plugin name."""
        files = [
            self.project / ".claude" / "settings.json",
            self.project / ".github" / "copilot" / "settings.json",
            self.home / ".claude" / "settings.json",
            self.home / ".copilot" / "settings.json",
        ]
        return not any("rh-dup" in _read(path) for path in files)

    def execute(self) -> dict:
        name_a, name_b = self.plugin_names()
        pkg_a = self.write_package("a", name_a)
        pkg_b = self.write_package("b", name_b)
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", ORIGIN)
        _write(self.project / "README.md", "consumer\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.seed_harness()
        self.manifest([pkg_a, pkg_b])
        install = self.apm("apm-install", "install", "--no-policy", "--target", self.harness)
        output = install.stdout + install.stderr
        self.evidence["install_exit"] = install.returncode
        self.evidence["install_tail"] = _tail(output, 2500)
        self.evidence["plugins_dir"] = sorted(
            p.name
            for p in self.plugins_root().glob("*")
            if p.is_dir() and p.name != ".claude-plugin"
        )
        if self.env == "collision-same" and install.returncode != 0:
            refusal = [line for line in output.splitlines() if "[x]" in line]
            detail = f"exit {install.returncode}: {' | '.join(refusal)[:300]}"
            clean = (
                install.returncode == 1
                and "Native plugin name collision" in output
                and not self.evidence["plugins_dir"]
                and self.settings_untouched()
            )
            # Only the collision diagnostic with nothing deployed is the expected refusal.
            return self.finish("REFUSED" if clean else "ERROR", detail)
        if install.returncode != 0:
            return self.finish("ERROR", f"apm install exit {install.returncode}")
        before = self.observe("both")
        self.evidence["both"] = before
        intact = {"a": self.bytes_intact(pkg_a), "b": self.bytes_intact(pkg_b)}
        self.evidence["bytes_intact"] = intact
        uninstall = self.apm("apm-uninstall-a", "uninstall", str(pkg_a))
        self.evidence["uninstall_exit"] = uninstall.returncode
        self.evidence["uninstall_tail"] = _tail(uninstall.stdout + uninstall.stderr, 2000)
        after = self.observe("after-uninstall-a")
        self.evidence["after"] = after
        self.evidence["bytes_intact_b_after"] = self.bytes_intact(pkg_b)
        if self.env == "collision-same":
            reason = (
                f"install exit 0; plugins_dir={self.evidence['plugins_dir']}; "
                f"both={before.get('skills')}; bytes={intact}; after={after.get('skills')}"
            )
            ok = intact["a"] and intact["b"] and len(before.get("skills", [])) >= 2
            return self.finish("LOADS" if ok else "PARTIAL", reason)
        both_ok = self.loads(before, {name_a, name_b}) and all(intact.values())
        b_ok = (
            uninstall.returncode == 0
            and self.loads(after, {name_b})
            and self.gone(after, name_a)
            and self.evidence["bytes_intact_b_after"]
        )
        reason = (
            f"both={before.get('skills')}; bytes={intact}; uninstall_a exit "
            f"{uninstall.returncode}; after={after.get('skills')}"
        )
        if both_ok and b_ok:
            return self.finish("LOADS", reason)
        return self.finish("PARTIAL" if (both_ok or b_ok) else "DOES NOT LOAD", reason)

    def finish(self, verdict: str, reason: str) -> dict:
        result = {
            "cell": self.id,
            "harness": self.harness,
            "env": self.env,
            "scope": self.scope,
            "fixture": "agent-plugin" if self.agent_plugin else "claude-plugin",
            "harness_version": self.harness_version(),
            "verdict": verdict,
            "reason": reason,
            "signals": {},
            "evidence": self.evidence,
            "steps": self.steps,
        }
        _json(self.root / "cell.json", result)
        return result


def secret_needles(token: str) -> tuple[list[bytes], list[bytes]]:
    """The token as written, and its base64 forms at every byte alignment.

    A token inside a longer base64 stream ("Bearer <t>", "<user>:<t>") encodes
    differently depending on its offset modulo 3. For each offset, encode
    ``NUL * offset + text`` and keep only the characters the text alone
    determines: drop the leading ones mixed with the prefix and the trailing
    ones mixed with whatever follows. Returns (raw needles, base64 needles).
    """
    if not token:
        return [], []
    encoded = []
    for text in (token, f"x-access-token:{token}"):
        for offset, lead in ((0, 0), (1, 2), (2, 3)):
            data = b"\0" * offset + text.encode()
            full = base64.b64encode(data)[: (len(data) // 3) * 4]
            encoded.append(full[lead:])
    return [token.encode()], encoded


def scan(paths: list[Path], token: str) -> list[Path]:
    """Files under ``paths`` that contain the token in any form (never printed).

    Base64 needles are also matched with line breaks removed, so a wrapped
    (for example 76-column) base64 block cannot hide the token.
    """
    raw, encoded = secret_needles(token)
    hits = []
    for root in paths:
        files = [root] if root.is_file() else sorted(f for f in root.rglob("*") if f.is_file())
        for path in files:
            data = path.read_bytes()
            joined = data.replace(b"\r", b"").replace(b"\n", b"")
            if any(n in data for n in raw) or any(n in data or n in joined for n in encoded):
                hits.append(path)
    return hits


def check(out: Path, expected_path: Path) -> list[str]:
    """Compare every cell verdict with the committed expectation (red-first oracle).

    BLOCKED (no credential, rate limit) is reported but never fails. Any other
    divergence fails, including a known bug cell that now LOADS: flip its
    expectation and close the linked issue in the fixing PR.
    """
    expected = json.loads(expected_path.read_text(encoding="utf-8"))["cells"]
    problems = []
    for path in sorted(out.rglob("cell.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        cell, verdict = row["cell"], row["verdict"]
        want = expected.get(cell)
        if verdict == "BLOCKED":
            print(f"[!] {cell}: BLOCKED ({row['reason']})")
            continue
        if want is None:
            problems.append(
                f"{cell}: {verdict}, but the cell has no expectation in {expected_path}"
            )
        elif verdict != want["verdict"]:
            issue = f" (tracked in #{want['issue']})" if want.get("issue") else ""
            problems.append(
                f"{cell}: expected {want['verdict']}{issue}, got {verdict}: {row['reason']}"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-harness native plugin load probe")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--harness", choices=HARNESSES, required=True)
    run.add_argument("--env", choices=ENVS + DIAG_ENVS + COLLISION_ENVS, action="append")
    run.add_argument("--scope", choices=SCOPES, action="append")
    run.add_argument("--out", type=Path, required=True)
    summ = sub.add_parser("summarize")
    summ.add_argument("out", type=Path)
    chk = sub.add_parser("check")
    chk.add_argument("out", type=Path)
    chk.add_argument("--expected", type=Path, default=Path(__file__).with_name("expected.json"))
    scn = sub.add_parser("scan", help="fail if $COPILOT_TOKEN appears in the files")
    scn.add_argument("paths", type=Path, nargs="+")
    args = parser.parse_args()
    if args.command == "scan":
        hits = scan(args.paths, os.environ.get("COPILOT_TOKEN", ""))
        for hit in hits:
            print(f"[x] the Copilot token appears in {hit}")
        return 1 if hits else 0
    if args.command == "check":
        problems = check(args.out, args.expected)
        for problem in problems:
            print(f"[x] {problem}")
        return 1 if problems else 0
    if args.command == "summarize":
        sys.stdout.write(summarize(args.out))
        return 0
    for env in args.env or ENVS:
        for scope in args.scope or SCOPES:
            kind = CollisionCell if env in COLLISION_ENVS else Cell
            result = kind(args.out, args.harness, env, scope).execute()
            print(f"{result['cell']}: {result['verdict']} ({result['reason']})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
