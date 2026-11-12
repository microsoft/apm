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

import contextlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
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
PR_UPDATE_SUFFIX = "update_pull_request"
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
REPO_PULLS_PATH = re.compile(r"(?:^|/)repos/([^/]+)/([^/]+)/pulls/(\d+)/?$")
PR_LOOKUP_TIMEOUT_S = 8.0
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_GIT_CONFIG_ENV = re.compile(r"^GIT_CONFIG[A-Za-z0-9_]*=")
_PUNCT = set(";&|()<>\n")


@dataclass
class PRTarget:
    """An existing PR whose actual head/base must be looked up (ready ops)."""

    ref: str | None = None
    repo: str | None = None
    node_id: str | None = None

    def label(self) -> str:
        if self.node_id:
            return self.node_id
        name = self.ref or "of the current branch"
        return f"{self.repo}#{name}" if self.repo and self.ref else name


@dataclass
class Action:
    """One gated operation found in a tool call."""

    kind: str
    cwd: str | None
    text: str
    sources: list[str] = field(default_factory=list)
    deny: str | None = None
    fix: str | None = None
    base: str | None = None
    head: str | None = None
    pr: PRTarget | None = None
    implicit: bool = False
    config: list[tuple[str, str | None]] = field(default_factory=list)


# Tailored "Fix:" lines for denials that re-running preflight cannot cure.
FIX_PUSH_HEAD = (
    "Fix: push only the current branch HEAD (e.g. `git push origin HEAD`); tags, "
    "--all/--mirror and multi-ref pushes are a human release step."
)
FIX_OTHER_SOURCE = "Fix: push only HEAD of the verified branch (`git push origin HEAD`)."
RUNNER_PLACEHOLDER = "<RUNNER>"
FAILED_IDS_CAP = 6
FIX_UNRESOLVED_SOURCE = "Fix: push an existing local branch or HEAD (e.g. `git push origin HEAD`)."
FIX_PARSE = (
    "Fix: rewrite it as one plain command (balanced quotes, no deep nesting) in its own tool call."
)
FIX_TARGET = "Fix: run the command from the repository root, without cd/-C/--git-dir indirection."
FIX_PR_HEAD = "Fix: push the branch first, then create the PR from it without --head."
FIX_IMPLICIT_PUSH = (
    "Fix: name the source explicitly (e.g. `git push origin HEAD`); configured push "
    "refspecs / push.default=matching can publish other branches."
)
FIX_PR_CREATE_TARGET = (
    "Fix: pass head and base explicitly (e.g. `-f head=<branch> -f base=main`), or use "
    "`gh pr create` from the verified branch."
)
FIX_PR_READY_TARGET = (
    "Fix: name the PR explicitly (e.g. `gh pr ready <number>`) and make sure `gh` is "
    "authenticated and online."
)
FIX_PR_PUSH_FIRST = "Fix: push the verified commit to the PR branch first, then retry."

# Segments that may precede a gated push/PR in the same tool call: they cannot
# move HEAD, rewrite history or change the worktree (D1: chained pushes).
READ_ONLY_COMMANDS = {"cd", "pushd", "popd", "true", "echo", "printf"}
# `fetch` is NOT read-only: `git fetch origin x:x` can move a local branch
# after the gate checked it.
READ_ONLY_GIT = {"status", "log", "diff", "show", "rev-parse"}


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


def _push_action(
    args: list[str],
    cwd: str | None,
    text: str,
    config: list[tuple[str, str | None]] | None = None,
) -> Action | None:
    """Parse `git push` arguments. None when not gated.

    *config* holds the command's `-c` / `--config-env` overrides (value None
    when it is taken from the environment and therefore unknown).
    """
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
            deny=f"`git push {unbindable}` cannot be bound to one verified commit",
            fix=FIX_PUSH_HEAD,
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
                "git-push",
                cwd,
                text,
                deny=f"wildcard refspec {spec!r} is not verifiable",
                fix=FIX_PUSH_HEAD,
            )
        if src.startswith("refs/tags/") or dst.startswith("refs/tags/") or src == "tag":
            return Action(
                "git-push",
                cwd,
                text,
                deny=f"tag push {spec!r} is not verifiable",
                fix=FIX_PUSH_HEAD,
            )
        sources.append(src or "HEAD")
    if refspecs and not sources:
        return None
    if refspecs:
        return Action("git-push", cwd, text, sources=sources)
    overrides = list(config or [])
    if any(value is None and _is_push_config(key) for key, value in overrides):
        return Action(
            "git-push",
            cwd,
            text,
            deny="implicit `git push` with push config taken from --config-env is not verifiable",
            fix=FIX_IMPLICIT_PUSH,
        )
    return Action("git-push", cwd, text, sources=["HEAD"], implicit=True, config=overrides)


def _config_key(key: str) -> tuple[str, str, int]:
    """(section, variable, part count); both are case-insensitive in git."""
    parts = key.split(".")
    return parts[0].lower(), parts[-1].lower(), len(parts)


def _is_push_default(key: str) -> bool:
    return _config_key(key) == ("push", "default", 2)


def _is_remote_push(key: str) -> bool:
    section, var, count = _config_key(key)
    return section == "remote" and var == "push" and count >= 3


def _is_push_config(key: str) -> bool:
    return _is_push_default(key) or _is_remote_push(key)


def _git_subcommand(args: list[str]) -> tuple[str, list[str]]:
    idx = 0
    while idx < len(args):
        cur = args[idx]
        if cur in GIT_VALUE_OPTS:
            idx += 2
        elif cur.startswith("-"):
            idx += 1
        else:
            return cur, args[idx + 1 :]
    return "", []


def _is_read_only(argv: list[str]) -> bool:
    """True for segments allowed before a gated push/PR in one tool call."""
    if not argv:
        return True
    name = os.path.basename(argv[0])
    if name in READ_ONLY_COMMANDS:
        return True
    if name != "git":
        return False
    sub, rest = _git_subcommand(argv[1:])
    if sub in READ_ONLY_GIT:
        return True
    if sub == "remote":
        return all(a in ("-v", "--verbose") for a in rest)
    if sub == "branch":
        return rest == ["--show-current"]
    if sub == "config":
        return bool(rest) and rest[0] in ("--get", "--get-all")
    if sub == "push":
        return any(a == "--dry-run" or (a[:1] == "-" and a[1:2] != "-" and "n" in a) for a in rest)
    return False


def _git_actions(args: list[str], cwd: str | None, text: str) -> list[Action]:
    """Only a literal `push` subcommand is gated; aliases are not expanded."""
    target = cwd
    config: list[tuple[str, str | None]] = []
    idx = 0
    while idx < len(args):
        cur = args[idx]
        if cur == "-C" and idx + 1 < len(args):
            target = _resolve_dir(target, args[idx + 1])
            idx += 2
        elif cur == "-c" and idx + 1 < len(args):
            key, sep, value = args[idx + 1].partition("=")
            config.append((key, value if sep else "true"))
            idx += 2
        elif cur == "--config-env" and idx + 1 < len(args):
            config.append((args[idx + 1].partition("=")[0], None))
            idx += 2
        elif cur.startswith("--config-env="):
            config.append((cur[len("--config-env=") :].partition("=")[0], None))
            idx += 1
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
        action = _push_action(rest, target, text, config)
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
            if "--help" in args or "-h" in args:
                return []
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
            target = PRTarget(
                ref=positionals[2] if len(positionals) > 2 else None,
                repo=_flag_value(args, "-R", "--repo"),
            )
            return [Action("gh-pr-ready", cwd, text, pr=target)]
        return []
    if group == "api":
        api_args = args[args.index("api") + 1 :] if "api" in args else []
        return _gh_api_actions(api_args, cwd, text)
    return []


def _api_fields(api_args: list[str]) -> tuple[dict[str, str], bool]:
    """`gh api` -f/-F key=value fields; True when some field is opaque (file/stdin)."""
    fields: dict[str, str] = {}
    opaque = False
    idx = 0
    while idx < len(api_args):
        cur = api_args[idx]
        value: str | None = None
        typed = False
        if cur in GH_API_FIELD_OPTS and cur != "--input":
            value = api_args[idx + 1] if idx + 1 < len(api_args) else None
            typed = cur in ("-F", "--field")
            idx += 2
        elif cur.startswith(("--raw-field=", "--field=")):
            value = cur.split("=", 1)[1]
            typed = cur.startswith("--field=")
            idx += 1
        elif cur == "--input" or cur.startswith("--input="):
            opaque = True
            idx += 2 if cur == "--input" else 1
            continue
        elif cur[:2] in ("-f", "-F") and len(cur) > 2:
            value = cur[2:]
            typed = cur[:2] == "-F"
            idx += 1
        elif cur in GH_API_VALUE_OPTS:
            idx += 2
            continue
        else:
            idx += 1
            continue
        if value is None:
            continue
        key, sep, val = value.partition("=")
        if not sep:
            continue
        if typed and val.startswith("@"):
            opaque = True
            continue
        fields[key] = val
    return fields, opaque


def _graphql_arg(query: str, name: str, fields: dict[str, str]) -> str | None:
    """Literal or `$variable` value of input argument *name* in *query*."""
    match = re.search(rf'\b{name}\s*:\s*(?:"((?:[^"\\]|\\.)*)"|\$(\w+))', query)
    if not match:
        return None
    if match.group(2):
        return fields.get(match.group(2))
    return match.group(1)


def _gh_api_actions(api_args: list[str], cwd: str | None, text: str) -> list[Action]:
    """API PR writes keep the requested head/base or the target PR to look up."""
    api_pos = _gh_positionals(api_args, GH_API_VALUE_OPTS | GH_REPO_OPTS)
    path = api_pos[0] if api_pos else ""
    method = _flag_value(api_args, "-X", "--method")
    has_fields = any(
        a in GH_API_FIELD_OPTS or a.startswith(("--field=", "--raw-field=", "--input="))
        for a in api_args
    )
    effective = (method or ("POST" if has_fields else "GET")).upper()
    fields, opaque = _api_fields(api_args)
    if path == "graphql":
        query = fields.get("query") or " ".join(api_args)
        found = set(GRAPHQL_PR_MUTATION.findall(" ".join(api_args)))
        actions = []
        if "createPullRequest" in found:
            head = _graphql_arg(query, "headRefName", fields)
            action = Action(
                "gh-api-pulls",
                cwd,
                text,
                base=_graphql_arg(query, "baseRefName", fields),
                head=head,
            )
            if head is None:
                action.deny = "cannot establish the head of the GraphQL createPullRequest"
                action.fix = FIX_PR_CREATE_TARGET
            actions.append(action)
        if "markPullRequestReadyForReview" in found:
            node = _graphql_arg(query, "pullRequestId", fields)
            action = Action("gh-api-pulls", cwd, text, pr=PRTarget(node_id=node))
            if not node:
                action.deny = "cannot establish the PR of markPullRequestReadyForReview"
                action.fix = FIX_PR_READY_TARGET
            actions.append(action)
        return actions
    clean = path.split("?")[0]
    if effective not in ("POST", "PATCH") or not PULLS_PATH.search(clean):
        return []
    numbered = REPO_PULLS_PATH.search(clean)
    if numbered:
        owner, name, number = numbered.groups()
        repo = None if "{" in owner + name else f"{owner}/{name}"
        return [Action("gh-api-pulls", cwd, text, pr=PRTarget(ref=number, repo=repo))]
    if PULLS_PATH.search(clean).group(2):
        return [
            Action(
                "gh-api-pulls",
                cwd,
                text,
                deny=f"cannot establish the PR targeted by `{clean}`",
                fix=FIX_PR_READY_TARGET,
            )
        ]
    action = Action("gh-api-pulls", cwd, text, base=fields.get("base"), head=fields.get("head"))
    if opaque or not action.head:
        action.deny = "cannot establish the head of the API PR creation"
        action.fix = FIX_PR_CREATE_TARGET
    return [action]


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
            return [
                Action(
                    "unparseable", cwd, script[:200], deny="command nesting too deep", fix=FIX_PARSE
                )
            ]
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
                    deny="could not parse the command",
                    fix=FIX_PARSE,
                )
            ]
        return []
    segments = _split_simple(tokens)
    actions: list[Action] = []
    backticked: list[Action] = []
    for inner in re.findall(r"`([^`]*)`", script):
        backticked += _classify_script(inner, cwd, depth + 1)
    writers: list[str] = []
    stack: list[str | None] = []
    current = cwd
    for words, delta in segments:
        for _ in range(max(delta, 0)):
            stack.append(current)
        for _ in range(max(-delta, 0)):
            current = stack.pop() if stack else current
        found: list[Action] = []
        for word in words:
            for inner in _substitutions(word):
                found += _classify_script(inner, current, depth + 1)
        argv, env_dir = _strip_prefix(words)
        head = os.path.basename(argv[0]) if argv else ""
        if head in ("cd", "pushd"):
            targets = [a for a in argv[1:] if not a.startswith("-") or a == "-"]
            if not targets:
                current = os.path.expanduser("~")
            elif targets[0] == "-":
                current = None
            else:
                current = _resolve_dir(current, targets[0])
        elif head == "popd":
            current = None
        elif argv:
            run_dir = _resolve_dir(current, env_dir) if env_dir else current
            here = _command_actions(argv, run_dir, depth)
            if any(_GIT_CONFIG_ENV.match(w) for w in words[: len(words) - len(argv)]):
                _deny_implicit_push(here)
            found += here
        if writers:
            _mark_chained(found, writers[0])
        actions += found
        if not _is_read_only(argv):
            writers.append(" ".join(words)[:80])
    if backticked and writers:
        _mark_chained(backticked, writers[0])
    return backticked + actions


def _deny_implicit_push(actions: list[Action]) -> None:
    """GIT_CONFIG_* assignments can set push.default / remote.<name>.push."""
    for action in actions:
        if action.implicit and action.deny is None:
            action.deny = "implicit `git push` under GIT_CONFIG_* overrides is not verifiable"
            action.fix = FIX_IMPLICIT_PUSH


def _mark_chained(actions: list[Action], segment: str) -> None:
    """D1: a gated action preceded by a mutating segment in the same call."""
    for action in actions:
        if action.deny is None:
            action.deny = (
                "run the push/PR command as its own tool call after preflight; this call "
                f"runs '{segment}' before it"
            )
            action.fix = (
                f"Fix: commit in its own tool call, run `{RUNNER_PLACEHOLDER}`, then run "
                f"`{action.text[:120]}` alone."
            )


def _marks_ready(args: dict) -> bool:
    """update_pull_request only publishes when it clears draft / marks ready."""
    draft = args.get("draft")
    if draft is False or (isinstance(draft, str) and draft.strip().lower() == "false"):
        return True
    for key in ("ready", "ready_for_review", "mark_ready", "markReadyForReview"):
        value = args.get(key)
        if value is True or (isinstance(value, str) and value.strip().lower() == "true"):
            return True
    return str(args.get("state", "")).strip().lower() == "ready"


def _tool_ready_action(name: str, args: dict, cwd: str | None) -> Action:
    """update_pull_request marking ready: the PR number is required to look it up."""
    number = next(
        (
            args[k]
            for k in ("pullNumber", "pull_number", "pr_number", "number")
            if args.get(k) not in (None, "")
        ),
        None,
    )
    owner, repo = args.get("owner"), args.get("repo")
    full = f"{owner}/{repo}" if isinstance(owner, str) and isinstance(repo, str) else None
    if isinstance(number, bool) or not str(number or "").strip().isdigit():
        return Action(
            "tool-pr-ready",
            cwd,
            name,
            deny=f"cannot establish which PR `{name}` marks ready (no PR number)",
            fix=FIX_PR_READY_TARGET,
        )
    return Action("tool-pr-ready", cwd, name, pr=PRTarget(ref=str(number).strip(), repo=full))


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
    if lowered.endswith(PR_UPDATE_SUFFIX):
        return [_tool_ready_action(name, args, cwd)] if _marks_ready(args) else []
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
    """A PR head names a branch: the local one, else origin's remote-tracking ref."""
    branch = value.split(":", 1)[1] if ":" in value else value
    if not branch or branch.startswith("-"):
        return None
    return lib.rev_commit(f"refs/heads/{branch}", root) or lib.rev_commit(
        f"refs/remotes/origin/{branch}", root
    )


def lookup_pr(root: str, target: PRTarget) -> dict | None:
    """{headRefOid, baseRefName} of an existing PR via `gh`, or None.

    Bounded by PR_LOOKUP_TIMEOUT_S so the hook stays well inside its timeout.
    """
    if target.node_id:
        query = (
            "query($id: ID!) { node(id: $id) { ... on PullRequest { headRefOid baseRefName } } }"
        )
        argv = ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"id={target.node_id}"]
        argv += ["--jq", ".data.node"]
    else:
        argv = ["gh", "pr", "view", *([target.ref] if target.ref else [])]
        argv += ["--json", "headRefOid,baseRefName"]
        if target.repo:
            argv += ["-R", target.repo]
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed gh argv, no shell
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        out, _ = proc.communicate(timeout=PR_LOOKUP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("headRefOid"), str) or not isinstance(data.get("baseRefName"), str):
        return None
    return data


PRLookup = Callable[[str, PRTarget], "dict | None"]
Problem = tuple[str, str | None]


def _base_problem(wanted: str, receipt: dict, hint: str) -> Problem | None:
    if wanted == receipt.get("base_branch"):
        return None
    why = f"PR base {wanted!r} differs from the preflight base {receipt.get('base_branch')!r}"
    fix = (
        f"Fix: target `{receipt.get('base_branch')}`, or re-run preflight against that "
        f"base: {hint} --base origin/{wanted}"
    )
    return why, fix


def _implicit_push_problem(action: Action, root: str) -> str | None:
    """An implicit push publishes more than HEAD under matching / remote refspecs."""
    default = None
    for key, value in action.config:
        if _is_push_default(key):
            default = value
    if default is None:
        default = lib.git_out(["config", "--get", "push.default"], root)
    if (default or "").strip().lower() == "matching":
        return "implicit `git push` with push.default=matching can publish every matching branch"
    remote_push = any(_is_remote_push(key) for key, _ in action.config) or lib.git_out(
        ["config", "--get-regexp", r"^remote\..*\.push$"], root
    )
    if remote_push:
        return "implicit `git push` uses configured remote.<name>.push refspecs"
    return None


def _verify_existing_pr(
    action: Action, receipt: dict, root: str, hint: str, lookup: PRLookup
) -> Problem | None:
    """Ready operations publish the PR's actual head, not the local HEAD."""
    target = action.pr or PRTarget()
    info = lookup(root, target)
    if info is None:
        return f"cannot establish the head of PR {target.label()}", FIX_PR_READY_TARGET
    verified = str(receipt.get("head"))
    head = str(info["headRefOid"])
    if head != verified:
        current, _ = lib.head_and_tree(root)
        why = (
            f"PR {target.label()} head ({head[:12]}) is not the preflight-verified "
            f"commit {verified[:12]}"
        )
        return why, (FIX_PR_PUSH_FIRST if current == verified else None)
    return _base_problem(str(info["baseRefName"]), receipt, hint)


def verify_action(
    action: Action,
    receipt: dict,
    root: str,
    hint: str = "",
    lookup: PRLookup | None = None,
) -> Problem | None:
    """Action-specific binding to the receipt commit: (why, fix) or None.

    A None fix means "re-run preflight" (the default Fix line).
    """
    verified = str(receipt.get("head"))
    if action.deny:
        return action.deny, action.fix
    if action.pr is not None:
        return _verify_existing_pr(action, receipt, root, hint, lookup or lookup_pr)
    current, _ = lib.head_and_tree(root)
    if action.kind == "git-push":
        if action.implicit:
            why = _implicit_push_problem(action, root)
            if why:
                return why, FIX_IMPLICIT_PUSH
        for src in action.sources:
            full = lib.git_out(["rev-parse", "--symbolic-full-name", src], root) or ""
            if full.startswith("refs/tags/"):
                return f"push of tag {src!r} is not verifiable", FIX_PUSH_HEAD
            sha = lib.rev_commit(src, root)
            if sha is None:
                return f"cannot resolve push source {src!r}", FIX_UNRESOLVED_SOURCE
            if sha != verified:
                why = (
                    f"push source {src!r} ({sha[:12]}) is not the preflight-verified "
                    f"commit {verified[:12]}"
                )
                return why, (None if sha == current else FIX_OTHER_SOURCE)
        return None
    head = current
    if action.head:
        resolved = _resolve_head_arg(action.head, root)
        if resolved is None:
            return f"cannot resolve PR head {action.head!r} locally", FIX_PR_HEAD
        head = resolved
    if head != verified:
        why = f"HEAD ({str(head)[:12]}) is not the preflight-verified commit {verified[:12]}"
        return why, (None if head == current else FIX_OTHER_SOURCE)
    if action.kind in ("gh-pr-create", "tool-create-pr", "gh-api-pulls"):
        return _base_problem(action.base or "main", receipt, hint)
    return None


REASON_LIMIT = 800
BYPASS_LINE = (
    f"Bypass is for humans only ({lib.BYPASS_ENV}={lib.BYPASS_VALUE} when launching the CLI)."
)


def preflight_fix(hint: str) -> str:
    return (
        f"Fix: commit all changes, then run from the repo root: {hint} "
        "(exit 0 = pass; re-run after any new commit), then retry."
    )


def compose_reason(
    why: str,
    head7: str | None,
    hint: str,
    failed: list[tuple[str, str]] | None = None,
    fix: str | None = None,
) -> str:
    """Self-sufficient deny text: why, exact fix, failed checks + logs, bypass."""
    at = f" (HEAD {head7})" if head7 else ""
    if fix:
        fix = fix.replace(RUNNER_PLACEHOLDER, hint)
    elif failed:
        fix = f"Fix: fix the failed checks, commit, re-run `{hint}` from the repo root, then retry."
    lines = [f"preflight gate: push/PR blocked: {why}{at}.", fix or preflight_fix(hint)]
    if failed:
        ids = [cid for cid, _log in failed]
        more = f" (+{len(ids) - FAILED_IDS_CAP} more)" if len(ids) > FAILED_IDS_CAP else ""
        dirs = sorted({os.path.dirname(log) for _cid, log in failed if log})
        logs = f"; logs: {dirs[0]}/<check-id>.log" if len(dirs) == 1 else ""
        lines.append(f"Failed checks: {', '.join(ids[:FAILED_IDS_CAP])}{more}{logs}")
    lines.append(BYPASS_LINE)
    text = "\n".join(lines)
    if len(text) > REASON_LIMIT:
        overflow = len(text) - REASON_LIMIT + 3
        longest = max(range(len(lines) - 1), key=lambda i: len(lines[i]))
        lines[longest] = lines[longest][: max(len(lines[longest]) - overflow, 40)] + "..."
        text = "\n".join(lines)
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
    pr_lookup: PRLookup = lookup_pr


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
            why = f"cannot resolve the target repository of `{action.text[:80]}`"
            return deny_json(compose_reason(why, _head7(host["toplevel"]), hint, fix=FIX_TARGET))
        root = info["toplevel"]
        if action.deny:
            return deny_json(compose_reason(action.deny, _head7(root), hint, fix=action.fix))
        receipt, error = lib.read_receipt(info["git_dir"])
        why = verify_receipt(receipt, error, info, cfg.bundle_hash_for(root), now)
        if why:
            failed = _failed_checks(receipt) if receipt is not None else []
            return deny_json(compose_reason(why, _head7(root), hint, failed))
        if receipt is not None:
            problem = verify_action(action, receipt, root, hint, cfg.pr_lookup)
            if problem:
                return deny_json(compose_reason(problem[0], _head7(root), hint, fix=problem[1]))
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
