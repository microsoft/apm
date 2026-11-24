from pathlib import Path

p = Path("src/apm_cli/cache/git_cache.py")
s = p.read_text()
s = s.replace(
    "\n\ndef _packed_commit(",
    "\n\n# PERF PROTOTYPE: compute git's answer too and fail loudly on any difference.\n"
    "_GITPATH_DIFFERENTIAL = True\n\n\ndef _packed_commit(",
    1,
)
old = '''        known = _packed_commit(bare_dir, sha)
        if known is not None:
            return known  # PERF PROTOTYPE (lever 2b)
'''
new = '''        known = _packed_commit(bare_dir, sha)
        if known is not None and not _GITPATH_DIFFERENTIAL:
            return known  # PERF PROTOTYPE (lever 2b)
'''
assert old in s
s = s.replace(old, new)
old = '''            return result.returncode == 0 and "commit" in result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return False'''
new = '''            spawned = result.returncode == 0 and "commit" in result.stdout.strip()
            if known is not None and known != spawned:
                raise RuntimeError("PERF lever 2b differential: pack idx != git cat-file -t")
            return spawned
        except (subprocess.TimeoutExpired, OSError):
            return False'''
assert old in s
s = s.replace(old, new)
old = '''        if _packed_commit(staged, sha):
            return  # PERF PROTOTYPE (lever 2b): the pack already holds the commit
'''
new = '''        packed = _packed_commit(staged, sha)
        if packed and not _GITPATH_DIFFERENTIAL:
            return  # PERF PROTOTYPE (lever 2b): the pack already holds the commit
'''
assert old in s
s = s.replace(old, new)
old = '''            if probe.returncode != 0 or "commit" not in probe.stdout:
                self._fetch_sha_into(staged, url, sha, env=env, partial=partial)'''
new = '''            spawned = probe.returncode == 0 and "commit" in probe.stdout
            if packed is not None and packed != spawned:
                raise RuntimeError("PERF lever 2b differential: pack idx != git cat-file -t")
            if not spawned:
                self._fetch_sha_into(staged, url, sha, env=env, partial=partial)'''
assert old in s
s = s.replace(old, new)
p.write_text(s)

p = Path("src/apm_cli/utils/git_sparse.py")
s = p.read_text()
old = '''    if key is not None and key in _LINK_PLANS:
        return _LINK_PLANS[key]
    result = _plan_tracked_symlinks_uncached('''
new = '''    if key is not None and key in _LINK_PLANS:
        cached = _LINK_PLANS[key]
        if not _GITPATH_DIFFERENTIAL:
            return cached
        fresh = _plan_tracked_symlinks_uncached(
            git_exe, repo_dir, paths, env=env, timeout=timeout, extra_git_args=extra_git_args
        )
        same_failures = {k: (type(v), str(v)) for k, v in fresh[1].items()} == {
            k: (type(v), str(v)) for k, v in cached[1].items()
        }
        if fresh[0] != cached[0] or not same_failures:
            raise RuntimeError("PERF lever 3 differential: memoized link plan != ls-tree")
        return cached
    result = _plan_tracked_symlinks_uncached('''
assert old in s
s = s.replace(old, new)
s = s.replace(
    "_LINK_PLANS: dict[",
    "# PERF PROTOTYPE: recompute and fail loudly on any difference.\n"
    "_GITPATH_DIFFERENTIAL = True\n_LINK_PLANS: dict[",
    1,
)
p.write_text(s)
print("patched")
