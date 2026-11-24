from pathlib import Path

p = Path("src/apm_cli/utils/git_env.py")
s = p.read_text()

old = '''def _urlmatched_header_group(
    remote_url: str,
    headers: Sequence[GitConfigEntry],
    env: dict[str, str],
) -> tuple[GitConfigEntry, ...]:
    """Return the exact extraHeader key group selected by Git for one URL."""
    if not headers:
        return ()
'''
new = '''# PERF RANK-5 PROTOTYPE: compute the spawn too and fail loudly on any difference.
_CONFIG_ONCE_DIFFERENTIAL = True
_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNSUPPORTED_URL_CHARS = frozenset("%*?#@" + chr(92))


def _simple_http_url(url: str) -> tuple[str, str, int, str] | None:
    """``(scheme, host, port, path)`` for a plain http(s) URL, else ``None`` (no shortcut)."""
    if any(ch in _UNSUPPORTED_URL_CHARS or ord(ch) < 0x21 or ord(ch) > 0x7E for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname or parts.username or parts.password:
        return None
    path = parts.path or "/"
    if any(seg in {".", ".."} for seg in path.split("/")) or "//" in path:
        return None
    return scheme, parts.hostname.lower(), port or _DEFAULT_PORTS[scheme], path


def _inprocess_header_group(
    remote_url: str, headers: Sequence[GitConfigEntry]
) -> tuple[GitConfigEntry, ...] | None:
    """PERF RANK-5 PROTOTYPE: Git's http.<url>.extraHeader urlmatch for plain URLs.

    Exact only for keys that are ``http.extraheader`` or ``http.<plain http(s)
    URL>.extraheader`` and a plain remote URL; anything else (users, wildcards,
    percent-encoding, dot segments, ties) returns ``None`` and the caller spawns.
    """
    target = _simple_http_url(remote_url)
    if target is None:
        return None
    best: tuple[int, int] | None = None
    best_keys: set[str] = set()
    for entry in headers:
        lowered = entry.key.lower()
        if lowered == "http.extraheader":
            score = (0, 0)
        else:
            if not (lowered.startswith("http.") and lowered.endswith(".extraheader")):
                return None
            config = _simple_http_url(entry.key[5 : -len(".extraheader")])
            if config is None:
                return None
            if config[:3] != target[:3]:
                continue
            base = config[3].rstrip("/")
            url_path = target[3]
            if not (url_path.rstrip("/") == base or url_path.startswith(base + "/")):
                continue
            score = (1, len(base))
        if best is None or score > best:
            best, best_keys = score, {lowered}
        elif score == best:
            best_keys.add(lowered)
    if best is None:
        return ()
    if len(best_keys) != 1:
        return None  # two spellings of one URL: let Git decide
    (selected,) = best_keys
    return tuple(entry for entry in headers if entry.key.lower() == selected)


def _urlmatched_header_group(
    remote_url: str,
    headers: Sequence[GitConfigEntry],
    env: dict[str, str],
) -> tuple[GitConfigEntry, ...]:
    """Return the exact extraHeader key group selected by Git for one URL."""
    if not headers:
        return ()
    fast = _inprocess_header_group(remote_url, headers)
    if fast is not None:
        if _CONFIG_ONCE_DIFFERENTIAL:
            spawned = _spawned_header_group(remote_url, headers, env)
            if spawned != fast:
                raise GitUrlRewriteProbeError("PERF rank-5 differential: in-process urlmatch != git")
        return fast
    return _spawned_header_group(remote_url, headers, env)


def _spawned_header_group(
    remote_url: str,
    headers: Sequence[GitConfigEntry],
    env: dict[str, str],
) -> tuple[GitConfigEntry, ...]:
    """Ask ``git config --get-urlmatch`` for the extraHeader key group of one URL."""
'''
assert old in s
s = s.replace(old, new)

old = '''    target_existed = target.exists()
    git_dir = target if bare else target / ".git"
    probe_created = not (git_dir / "config").exists()'''
new = '''    target_existed = target.exists()
    git_dir = target if bare else target / ".git"
    probe_created = not (git_dir / "config").exists()
    if probe_created and not (target_existed and any(target.iterdir())):
        once = _config_once_clone_env(remote_url, overrides)
        if once is not None:
            if _CONFIG_ONCE_DIFFERENTIAL:
                full = _probed_clone_env(remote_url, overrides, target, bare=bare)
                if full != once:
                    raise GitUrlRewriteProbeError(
                        "PERF rank-5 differential: config-once env != probe"
                    )
            return once
    return _probed_clone_env(remote_url, overrides, target, bare=bare)


def _has_include(snapshot: _GitConfigSnapshot) -> bool:
    for entry in snapshot.entries:
        key = entry.key.lower()
        if key == "include.path" or (key.startswith("includeif.") and key.endswith(".path")):
            return True
    return False


def _config_once_clone_env(
    remote_url: str, overrides: dict[str, object] | None
) -> dict[str, str] | None:
    """PERF RANK-5 PROTOTYPE: the clone env without a throwaway probe repository.

    A repository ``git init --no-template`` + ``remote add origin`` creates holds
    only core.* and remote.origin.* in its local scope: no http.* / url.* entry,
    which is all the network env keeps from local scope. So its effective
    network config equals the repository-less one (memoized per command) unless
    an include/includeIf can select files by the repository (gitdir:,
    hasconfig:remote): then the probe repository is created as before.
    """
    try:
        parent = _read_effective_git_config(git_subprocess_env())
        child = _read_effective_git_config(git_subprocess_env(overrides))
    except (GitUrlRewriteProbeError, ValueError):
        return None
    if _has_include(parent) or _has_include(child):
        return None
    return git_network_env(remote_url, overrides, git_dir=None)


def _probed_clone_env(
    remote_url: str,
    overrides: dict[str, object] | None,
    target: Path,
    *,
    bare: bool = False,
) -> dict[str, str]:
    """Validate clone config in a throwaway Git directory the target would activate."""
    target_existed = target.exists()
    git_dir = target if bare else target / ".git"
    probe_created = not (git_dir / "config").exists()'''
assert old in s
s = s.replace(old, new)
p.write_text(s)
print("patched")
