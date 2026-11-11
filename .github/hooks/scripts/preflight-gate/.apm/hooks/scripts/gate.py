#!/usr/bin/env python3
"""gate.py -- preToolUse gate: deny unverified push / PR actions.

Reads ONE hook payload on stdin. Prints nothing (default flow) or exactly
one deny JSON object. Never prints "allow" (that would auto-approve), and
always exits 0: a crashing command hook is fail-closed and would brick every
tool call, so the launcher and this script fail open on their own bugs and
fail closed only on a well-formed deny.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
from collections.abc import Callable  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import preflight_lib as lib  # noqa: E402

# Exact tool names hosts emit for shell execution. The hook JSON `matcher`
# (^(?:PATTERN)$ on toolName) MUST cover every name here plus
# PR_TOOL_SUFFIX; tests/test_launcher.py enforces it.
SHELL_TOOL_NAMES = (
    "bash",
    "Bash",
    "write_bash",
    "shell",
    "powershell",
    "pwsh",
    "run_in_terminal",
    "runInTerminal",
)
SHELL_TOOLS = {name.lower() for name in SHELL_TOOL_NAMES}
PR_TOOL_SUFFIX = "create_pull_request"
COMMAND_KEYS = ("command", "input", "cmd", "script")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
KEYWORDS = {"!", "{", "}", "if", "then", "else", "elif", "do", "while", "until", "fi", "done"}
WRAPPERS_PLAIN = {"nohup", "builtin", "caffeinate"}

GIT_VALUE_OPTS = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--super-prefix",
    "--attr-source",
}
PUSH_VALUE_OPTS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
PUSH_UNBINDABLE = {
    "--all": "--all",
    "--branches": "--branches",
    "--mirror": "--mirror",
    "--tags": "--tags",
    "--follow-tags": "--follow-tags",
}
GH_REPO_OPTS = {"-R", "--repo"}
GH_API_VALUE_OPTS = {
    "-X",
    "--method",
    "-H",
    "--header",
    "-f",
    "--raw-field",
    "-F",
    "--field",
    "--input",
    "-q",
    "--jq",
    "-t",
    "--template",
    "--cache",
    "-p",
    "--preview",
    "--hostname",
}
GH_API_FIELD_OPTS = {"-f", "--raw-field", "-F", "--field", "--input"}
PULLS_PATH = re.compile(r"(^|/)pulls(/\d+)?/?$")
GRAPHQL_PR_MUTATION = re.compile(r"\b(createPullRequest|markPullRequestReadyForReview)\b")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PUNCT = set(";&|()<>\n")


@dataclass
class Action:
    """One gated operation found in a tool call."""

    kind: str
    cwd: str | None
    text: str
    sources: list[str] = field(default_factory=list)
    deny: str | None = None
    base: str | None = None
    head: str | None = None


# ---------------------------------------------------------------------------
# classification (pure: no subprocess; git aliases are NOT expanded)
# ---------------------------------------------------------------------------


def _lex(script: str) -> list[str]:
    lex = shlex.shlex(script, posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _is_punct(token: str) -> bool:
    return bool(token) and all(ch in _PUNCT for ch in token)


def _resolve_dir(cwd: str | None, target: str) -> str | None:
    expanded = os.path.expandvars(os.path.expanduser(target))
    if "$" in expanded or "`" in expanded:
        return None
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    if cwd is None:
        return None
    return os.path.normpath(os.path.join(cwd, expanded))


def _substitutions(word: str) -> list[str]:
    """Inner scripts of $(...) inside one word (backticks: see caller)."""
    inner: list[str] = []
    start = word.find("$(")
    while start != -1:
        depth = 0
        end = len(word)
        for idx in range(start + 1, len(word)):
            if word[idx] == "(":
                depth += 1
            elif word[idx] == ")":
                depth -= 1
                if depth == 0:
                    end = idx
                    break
        inner.append(word[start + 2 : end])
        start = word.find("$(", end)
    return inner


def _split_simple(tokens: list[str]) -> list[tuple[list[str], int]]:
    """Split tokens into simple commands: [(words, paren_delta_before)]."""
    commands: list[tuple[list[str], int]] = []
    words: list[str] = []
    delta = 0
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if _is_punct(token):
            is_redirect = ("<" in token or ">" in token) and "(" not in token and ")" not in token
            if is_redirect:
                if words and words[-1].isdigit():
                    words.pop()
                skip_next = True
                continue
            if words:
                commands.append((words, delta))
                words = []
                delta = 0
            delta += token.count("(") - token.count(")")
            continue
        words.append(token)
    if words:
        commands.append((words, delta))
    return commands


def _strip_prefix(words: list[str]) -> tuple[list[str], str | None]:
    """Drop assignments, keywords and wrappers. Returns (argv, env -C dir)."""
    env_dir = None
    idx = 0
    while idx < len(words):
        word = words[idx]
        base = os.path.basename(word)
        if _ASSIGNMENT.match(word) or word in KEYWORDS or base in WRAPPERS_PLAIN:
            idx += 1
        elif base == "time":
            idx += 1
            while idx < len(words) and words[idx] == "-p":
                idx += 1
        elif base == "env":
            idx += 1
            while idx < len(words):
                cur = words[idx]
                if cur in ("-u", "--unset", "-P", "-S", "--split-string"):
                    idx += 2
                elif cur in ("-C", "--chdir"):
                    env_dir = words[idx + 1] if idx + 1 < len(words) else None
                    idx += 2
                elif cur.startswith("-") or _ASSIGNMENT.match(cur):
                    idx += 1
                else:
                    break
        elif base == "command":
            idx += 1
            while idx < len(words) and words[idx] == "-p":
                idx += 1
            if idx < len(words) and words[idx] in ("-v", "-V"):
                return [], env_dir
        elif base == "exec":
            idx += 1
            while idx < len(words) and words[idx].startswith("-"):
                idx += 2 if words[idx] == "-a" else 1
        elif base in ("sudo", "xargs", "timeout", "nice", "stdbuf", "ionice"):
            idx = _skip_wrapper_opts(base, words, idx + 1)
        else:
            break
    return words[idx:], env_dir


_WRAPPER_VALUE_OPTS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T"},
    "xargs": {"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "stdbuf": {"-i", "-o", "-e"},
    "ionice": {"-c", "-n", "-p"},
}


def _skip_wrapper_opts(base: str, words: list[str], idx: int) -> int:
    value_opts = _WRAPPER_VALUE_OPTS[base]
    while idx < len(words):
        cur = words[idx]
        if cur == "--":
            return idx + 1
        if cur in value_opts:
            idx += 2
        elif cur.startswith("-") and len(cur) > 1:
            idx += 1
        else:
            break
    if base == "timeout" and idx < len(words):
        idx += 1  # duration
    return idx


def _shell_script(args: list[str]) -> str | None:
    """Script text of `bash -c <script>` style invocations."""
    idx = 0
    want_script = False
    while idx < len(args):
        cur = args[idx]
        if cur == "--":
            idx += 1
            continue
        if cur in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            idx += 2
            continue
        if cur.startswith("-") and not cur.startswith("--") and "c" in cur[1:]:
            want_script = True
            idx += 1
            continue
        if cur.startswith("-") and len(cur) > 1:
            idx += 1
            continue
        return cur if want_script else None
    return None


def _push_action(args: list[str], cwd: str | None, text: str) -> Action | None:
    """Parse `git push` arguments. None when not gated."""
    positionals: list[str] = []
    delete = False
    unbindable = None
    idx = 0
    while idx < len(args):
        cur = args[idx]
        idx += 1
        if cur == "--":
            positionals.extend(args[idx:])
            break
        if cur in ("--dry-run",):
            return None
        if cur == "--delete":
            delete = True
        elif cur in PUSH_UNBINDABLE:
            unbindable = cur
        elif cur in PUSH_VALUE_OPTS or cur == "--recurse-submodules":
            idx += 1
        elif cur.startswith("--"):
            continue
        elif cur.startswith("-") and len(cur) > 1:
            letters = cur[1:]
            for pos, letter in enumerate(letters):
                if letter == "n":
                    return None
                if letter == "d":
                    delete = True
                if letter == "o":
                    if pos == len(letters) - 1:
                        idx += 1
                    break
        else:
            positionals.append(cur)
    refspecs = positionals[1:]
    if unbindable:
        return Action(
            "git-push",
            cwd,
            text,
            deny=f"`git push {unbindable}` cannot be bound to one verified commit; push a branch",
        )
    if delete:
        return None
    sources: list[str] = []
    for spec in refspecs:
        spec = spec.lstrip("+")
        if spec.startswith(":"):
            continue
        src, _, dst = spec.partition(":")
        if "*" in spec:
            return Action(
                "git-push", cwd, text, deny=f"wildcard refspec {spec!r} is not verifiable"
            )
        if src.startswith("refs/tags/") or dst.startswith("refs/tags/") or src == "tag":
            return Action("git-push", cwd, text, deny=f"tag push {spec!r} is not verifiable")
        sources.append(src or "HEAD")
    if refspecs and not sources:
        return None
    return Action("git-push", cwd, text, sources=sources or ["HEAD"])


def _git_actions(args: list[str], cwd: str | None, text: str) -> list[Action]:
    """Only a literal `push` subcommand is gated; aliases are not expanded."""
    target = cwd
    idx = 0
    while idx < len(args):
        cur = args[idx]
        if cur == "-C" and idx + 1 < len(args):
            target = _resolve_dir(target, args[idx + 1])
            idx += 2
        elif cur in GIT_VALUE_OPTS:
            if cur in ("--git-dir", "--work-tree"):
                target = None
            idx += 2
        elif cur.startswith(("--git-dir=", "--work-tree=")):
            target = None
            idx += 1
        elif cur.startswith("-"):
            idx += 1
        else:
            break
    if idx >= len(args):
        return []
    sub, rest = args[idx], args[idx + 1 :]
    if sub == "push":
        action = _push_action(rest, target, text)
        return [action] if action else []
    return []


def _flag_value(args: list[str], short: str, long: str) -> str | None:
    for idx, cur in enumerate(args):
        if cur in (short, long) and idx + 1 < len(args):
            return args[idx + 1]
        if cur.startswith(long + "="):
            return cur[len(long) + 1 :]
        if cur.startswith(short) and len(cur) > len(short) and not cur.startswith("--"):
            return cur[len(short) :]
    return None


def _gh_positionals(args: list[str], value_opts: set[str]) -> list[str]:
    found = []
    idx = 0
    while idx < len(args):
        cur = args[idx]
        if cur in value_opts:
            idx += 2
            continue
        if not cur.startswith("-"):
            found.append(cur)
        idx += 1
    return found


def _gh_actions(args: list[str], cwd: str | None, text: str) -> list[Action]:
    positionals = _gh_positionals(args, GH_REPO_OPTS)
    if not positionals:
        return []
    group = positionals[0]
    if group == "pr" and len(positionals) > 1:
        sub = positionals[1]
        if sub in ("create", "new"):
            return [
                Action(
                    "gh-pr-create",
                    cwd,
                    text,
                    base=_flag_value(args, "-B", "--base"),
                    head=_flag_value(args, "-H", "--head"),
                )
            ]
        if sub == "ready" and "--undo" not in args:
            return [Action("gh-pr-ready", cwd, text)]
        return []
    if group == "api":
        api_args = args[args.index("api") + 1 :] if "api" in args else []
        api_pos = _gh_positionals(api_args, GH_API_VALUE_OPTS | GH_REPO_OPTS)
        path = api_pos[0] if api_pos else ""
        method = _flag_value(api_args, "-X", "--method")
        has_fields = any(
            a in GH_API_FIELD_OPTS or a.startswith(("--field=", "--raw-field=", "--input="))
            for a in api_args
        )
        effective = (method or ("POST" if has_fields else "GET")).upper()
        if path == "graphql" and any(GRAPHQL_PR_MUTATION.search(a) for a in api_args):
            return [Action("gh-api-pulls", cwd, text)]
        if effective in ("POST", "PATCH") and PULLS_PATH.search(path.split("?")[0]):
            return [Action("gh-api-pulls", cwd, text)]
    return []


def _command_actions(argv: list[str], cwd: str | None, depth: int) -> list[Action]:
    text = " ".join(argv)[:300]
    name = os.path.basename(argv[0])
    if name.endswith(".exe"):
        name = name[:-4]
    args = argv[1:]
    if name in SHELLS:
        script = _shell_script(args)
        return _classify_script(script, cwd, depth + 1) if script else []
    if name == "eval":
        return _classify_script(" ".join(args), cwd, depth + 1)
    if name == "git":
        return _git_actions(args, cwd, text)
    if name == "gh":
        return _gh_actions(args, cwd, text)
    return []


def looks_gated(raw: str) -> bool:
    """Conservative raw-text heuristic for unparseable commands."""
    git_push = re.search(r"\bgit\b", raw) and "push" in raw
    gh_pr = re.search(r"\bgh\b", raw) and re.search(r"\bpr\b|pulls", raw)
    return bool(git_push or gh_pr)


def _classify_script(script: str, cwd: str | None, depth: int = 0) -> list[Action]:
    if depth > 6:
        if looks_gated(script):
            return [Action("unparseable", cwd, script[:200], deny="command nesting too deep")]
        return []
    try:
        tokens = _lex(script)
    except ValueError:
        if looks_gated(script):
            return [
                Action(
                    "unparseable",
                    cwd,
                    script[:200],
                    deny="could not parse the command; rewrite it as a plain command",
                )
            ]
        return []
    actions: list[Action] = []
    for inner in re.findall(r"`([^`]*)`", script):
        actions += _classify_script(inner, cwd, depth + 1)
    stack: list[str | None] = []
    current = cwd
    for words, delta in _split_simple(tokens):
        for _ in range(max(delta, 0)):
            stack.append(current)
        for _ in range(max(-delta, 0)):
            current = stack.pop() if stack else current
        for word in words:
            for inner in _substitutions(word):
                actions += _classify_script(inner, current, depth + 1)
        argv, env_dir = _strip_prefix(words)
        if not argv:
            continue
        run_dir = _resolve_dir(current, env_dir) if env_dir else current
        head = os.path.basename(argv[0])
        if head in ("cd", "pushd"):
            targets = [a for a in argv[1:] if not a.startswith("-") or a == "-"]
            if not targets:
                current = os.path.expanduser("~")
            elif targets[0] == "-":
                current = None
            else:
                current = _resolve_dir(current, targets[0])
            continue
        if head == "popd":
            current = None
            continue
        actions += _command_actions(argv, run_dir, depth)
    return actions


def classify(tool_name: str, tool_args: object, cwd: str | None) -> list[Action]:
    """Return gated actions for one tool call (empty list = not gated)."""
    name = (tool_name or "").strip()
    lowered = name.lower()
    args = tool_args if isinstance(tool_args, dict) else {}
    if lowered in SHELL_TOOLS:
        command = next((args[k] for k in COMMAND_KEYS if isinstance(args.get(k), str)), None)
        if not command:
            return []
        return _classify_script(command, cwd)
    if lowered.endswith(PR_TOOL_SUFFIX):
        base = args.get("base") or args.get("base_branch")
        head = args.get("head") or args.get("head_branch")
        return [
            Action(
                "tool-create-pr",
                cwd,
                name,
                base=base if isinstance(base, str) else None,
                head=head if isinstance(head, str) else None,
            )
        ]
    return []


# ---------------------------------------------------------------------------
# payload + verification
# ---------------------------------------------------------------------------


def parse_payload(raw: str) -> tuple[str, object, str | None] | None:
    """Return (tool_name, tool_args, cwd) for camelCase or PascalCase payloads."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("toolName") or data.get("tool_name") or ""
    args: object = None
    for key in ("toolArgs", "tool_input", "toolInput", "tool_args"):
        if key in data:
            args = data[key]
            break
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"command": args}
    raw_cwd = data.get("cwd")
    cwd = os.path.realpath(raw_cwd) if isinstance(raw_cwd, str) and raw_cwd else None
    return str(name), args, cwd


def deny_json(reason: str) -> str:
    reason = reason.encode("ascii", "replace").decode("ascii")
    return json.dumps(
        {
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }
    )


def _samefile(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _failed_checks(receipt: dict) -> list[tuple[str, str]]:
    """(check id, log path) for every non-zero check in the receipt."""
    out = []
    for check in receipt.get("checks") or []:
        if not isinstance(check, dict):
            out.append(("?", ""))
        elif check.get("exit") != 0:
            out.append((str(check.get("id", "?")), str(check.get("log", ""))))
    return out


def verify_receipt(
    receipt: dict | None,
    error: str | None,
    target: dict[str, str],
    expected_bundle: str | None,
    now: datetime,
) -> str | None:
    """Repo-level receipt validity. Returns a deny reason or None."""
    if error:
        return error
    if receipt is None:
        return "no fresh green preflight receipt for this worktree"
    if receipt.get("schema") != lib.SCHEMA:
        return f"receipt schema {receipt.get('schema')!r} is not {lib.SCHEMA}"
    failed = _failed_checks(receipt)
    required = receipt.get("required_tiers") or {}
    ran = receipt.get("ran_tiers") or []
    missing = [t for t in required if t not in ran]
    if receipt.get("status") != "pass" or failed:
        if failed:
            return f"the last preflight run failed (status {receipt.get('status')!r})"
        if missing:
            return (
                f"the last preflight run was {receipt.get('status')!r}; required tier(s) "
                f"not run: {', '.join(missing)}"
            )
        return f"the last preflight run has status {receipt.get('status')!r}, not 'pass'"
    if missing or not required:
        return f"receipt is missing required tier(s): {', '.join(missing) or 'none recorded'}"
    if receipt.get("gate_eligible") is not True:
        reasons = "; ".join(receipt.get("ineligible_reasons") or []) or "unknown"
        return f"receipt is not gate-eligible ({reasons})"
    return _verify_freshness(receipt, target, expected_bundle, now)


def _verify_freshness(
    receipt: dict, target: dict[str, str], expected_bundle: str | None, now: datetime
) -> str | None:
    """Bundle identity, base binding, age and tree/commit consistency."""
    if expected_bundle is None or receipt.get("bundle_hash") != expected_bundle:
        return "preflight bundle changed since the receipt was written (re-run after apm install)"
    root = target["toplevel"]
    base_ref = receipt.get("base_ref") or ""
    current_base = lib.rev_commit(base_ref, root) if base_ref else None
    if current_base != receipt.get("base_sha"):
        return f"base {base_ref!r} moved since preflight (fetch done; re-run preflight)"
    try:
        finished = lib.parse_iso(str(receipt.get("finished_at")))
    except ValueError:
        return "receipt timestamp unreadable"
    age = (now - finished).total_seconds()
    if age < -lib.MAX_FUTURE_SKEW_S:
        return "receipt timestamp is in the future"
    if age > lib.MAX_AGE_S:
        return f"receipt is older than {lib.MAX_AGE_S // 3600}h"
    head = receipt.get("head")
    tree = lib.git_out(["rev-parse", "--verify", "--quiet", f"{head}^{{tree}}"], root)
    if not head or tree != receipt.get("tree"):
        return "receipt tree does not match its commit"
    return None


def _resolve_head_arg(value: str, root: str) -> str | None:
    branch = value.split(":", 1)[1] if ":" in value else value
    return lib.rev_commit(branch, root) or lib.rev_commit(f"refs/remotes/origin/{branch}", root)


def verify_action(action: Action, receipt: dict, root: str) -> str | None:
    """Action-specific binding to the receipt commit. Returns a deny reason."""
    verified = str(receipt.get("head"))
    if action.deny:
        return action.deny
    if action.kind == "git-push":
        for src in action.sources:
            full = lib.git_out(["rev-parse", "--symbolic-full-name", src], root) or ""
            if full.startswith("refs/tags/"):
                return f"push of tag {src!r} is not verifiable"
            sha = lib.rev_commit(src, root)
            if sha is None:
                return f"cannot resolve push source {src!r}"
            if sha != verified:
                return (
                    f"push source {src!r} ({sha[:12]}) is not the preflight-verified "
                    f"commit {verified[:12]}"
                )
        return None
    head, _ = lib.head_and_tree(root)
    if action.head:
        resolved = _resolve_head_arg(action.head, root)
        if resolved is None:
            return f"cannot resolve PR head {action.head!r} locally"
        head = resolved
    if head != verified:
        return f"HEAD ({str(head)[:12]}) is not the preflight-verified commit {verified[:12]}"
    if action.kind in ("gh-pr-create", "tool-create-pr"):
        wanted = action.base or "main"
        if wanted != receipt.get("base_branch"):
            return (
                f"PR base {wanted!r} differs from the preflight base "
                f"{receipt.get('base_branch')!r} (re-run with --base)"
            )
    return None


REASON_LIMIT = 800
BYPASS_LINE = (
    f"Bypass is for humans only ({lib.BYPASS_ENV}={lib.BYPASS_VALUE} when launching the CLI)."
)


def compose_reason(
    why: str, head7: str | None, hint: str, failed: list[tuple[str, str]] | None = None
) -> str:
    """Self-sufficient deny text: why, exact fix command, failures, bypass."""
    at = f" (HEAD {head7})" if head7 else ""
    lines = [f"preflight gate: push/PR blocked: {why}{at}."]
    lines.append(
        f"Fix: commit all changes, then run from the repo root: {hint} "
        "(exit 0 = pass; re-run after any new commit), then retry."
    )
    tail = [BYPASS_LINE]
    if failed:
        log_dirs = {os.path.dirname(log) for _cid, log in failed if log}
        ids = [cid for cid, _log in failed]
        if len(log_dirs) == 1:
            logs = f"{log_dirs.pop()}/<check-id>.log"
        else:
            logs = "; ".join(log for _cid, log in failed if log) or "see receipt"
        budget = REASON_LIMIT - len("\n".join([*lines, *tail])) - len(logs) - 40
        shown: list[str] = []
        for cid in ids:
            if len(", ".join([*shown, cid])) > max(budget, 20):
                break
            shown.append(cid)
        more = f" (+{len(ids) - len(shown)} more)" if len(shown) < len(ids) else ""
        lines.append(f"Failed checks: {', '.join(shown)}{more}. Logs: {logs}")
    text = "\n".join([*lines, *tail])
    if len(text) > REASON_LIMIT:
        overflow = len(text) - REASON_LIMIT + 3
        lines[0] = lines[0][: max(len(lines[0]) - overflow, 40)] + "..."
        text = "\n".join([*lines, *tail])
    return text


def _head7(root: str) -> str | None:
    head, _ = lib.head_and_tree(root)
    return head[:7] if head else None


def _log_bypass(git_dir: str, tool: str, actions: list[Action]) -> None:
    try:
        pdir = lib.preflight_dir(git_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        with (pdir / lib.BYPASS_LOG_NAME).open("a", encoding="utf-8") as log:
            texts = " | ".join(a.text for a in actions)
            log.write(f"{lib.now_iso()} {tool} {texts}\n".encode("ascii", "replace").decode())
    except Exception:
        pass


@dataclass
class GateConfig:
    """Injectable environment for run(); defaults derive from this file."""

    scripts_dir: Path = _HERE
    bundle_hash_for: Callable[[str], str | None] = lib.deployed_bundle_hash
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _runner_hint(host_root: str, scripts_dir: Path) -> str:
    runner = scripts_dir / lib.RUNNER_NAME
    try:
        rel = os.path.relpath(runner, host_root)
    except ValueError:
        rel = str(runner)
    return f"python3 {rel}"


def _target_status(action: Action, host: dict[str, str]) -> tuple[str, dict | None]:
    """('out', None) | ('ambiguous', None) | ('in', target_info)."""
    if action.cwd is None or not os.path.isdir(action.cwd):
        return "ambiguous", None
    info = lib.repo_info(action.cwd)
    if info is None:
        return "out", None
    if _samefile(info["toplevel"], host["toplevel"]) or _samefile(
        info["common_dir"], host["common_dir"]
    ):
        return "in", info
    return "out", None


def evaluate(raw: str, env: dict[str, str], config: GateConfig | None = None) -> str | None:
    """Return a deny JSON string, or None for the default flow."""
    cfg = config or GateConfig()
    parsed = parse_payload(raw)
    if parsed is None:
        print("[preflight-gate] unparseable payload; default flow", file=sys.stderr)
        return None
    tool, tool_args, payload_cwd = parsed
    cwd = payload_cwd or os.path.realpath(os.getcwd())
    try:
        actions = classify(tool, tool_args, cwd)
    except Exception as exc:
        command = ""
        if isinstance(tool_args, dict):
            command = " ".join(str(v) for v in tool_args.values() if isinstance(v, str))
        if not looks_gated(command):
            return None
        return _internal_error_deny(exc, cfg)
    if not actions:
        return None
    try:
        return _decide(tool, actions, env, cfg)
    except Exception as exc:
        return _internal_error_deny(exc, cfg)


def _internal_error_deny(exc: BaseException, cfg: GateConfig) -> str:
    hint = f"python3 {lib.DEPLOYED_SCRIPTS_REL}/{lib.RUNNER_NAME}"
    head7 = None
    try:
        host = lib.repo_info(cfg.scripts_dir)
        if host is not None:
            hint = _runner_hint(host["toplevel"], cfg.scripts_dir)
            head7 = _head7(host["toplevel"])
    except Exception:
        pass
    return deny_json(compose_reason(f"preflight gate internal error ({exc!r:.120})", head7, hint))


def _decide(tool: str, actions: list[Action], env: dict[str, str], cfg: GateConfig) -> str | None:
    host = lib.repo_info(cfg.scripts_dir)
    if host is None:
        return None
    hint = _runner_hint(host["toplevel"], cfg.scripts_dir)
    scoped: list[tuple[Action, dict | None]] = []
    for action in actions:
        state, info = _target_status(action, host)
        if state != "out":
            scoped.append((action, info))
    if not scoped:
        return None
    bypass = env.get(lib.BYPASS_ENV, "")
    if bypass == lib.BYPASS_VALUE:
        _log_bypass(host["git_dir"], tool, [a for a, _ in scoped])
        return None
    if bypass:
        print(
            f"[preflight-gate] ignoring {lib.BYPASS_ENV}={bypass!r} "
            f"(only {lib.BYPASS_VALUE!r} bypasses)",
            file=sys.stderr,
        )
    now = cfg.now()
    for action, info in scoped:
        if info is None:
            why = (
                f"cannot resolve the target repository of `{action.text[:80]}`; run it "
                "from the repo root without cd/-C indirection"
            )
            return deny_json(compose_reason(why, _head7(host["toplevel"]), hint))
        root = info["toplevel"]
        if action.deny:
            return deny_json(compose_reason(action.deny, _head7(root), hint))
        receipt, error = lib.read_receipt(info["git_dir"])
        problem = verify_receipt(receipt, error, info, cfg.bundle_hash_for(root), now)
        if problem is None and receipt is not None:
            problem = verify_action(action, receipt, root)
        if problem:
            failed = _failed_checks(receipt) if receipt is not None else []
            return deny_json(compose_reason(problem, _head7(root), hint, failed))
    return None


def main() -> int:
    try:
        raw = sys.stdin.read()
        out = evaluate(raw, dict(os.environ))
        if out:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    try:
        main()
    finally:
        os._exit(0)
