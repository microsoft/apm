from pathlib import Path

p = Path("src/apm_cli/utils/realpath.py")
s = p.read_text()
s += '''

# PERF RESEARCH CEILING (never ships): memoize os.path.realpath per process (one
# command) to size what "resolve each root once, derive the rest from validated
# components" could remove. NOT faithful (a swapped link is not observed); the
# compliant design keeps a per-created-directory reparse/link check instead.
def _install_realpath_ceiling() -> None:
    import ntpath
    import posixpath

    for module in (ntpath, posixpath):
        original = module.realpath
        memo: dict = {}

        def memoized(path, *args, _original=original, _memo=memo, **kwargs):
            try:
                key = (os.fspath(path), args, tuple(sorted(kwargs.items())))
            except TypeError:
                return _original(path, *args, **kwargs)
            hit = _memo.get(key)
            if hit is None:
                hit = _original(path, *args, **kwargs)
                _memo[key] = hit
            return hit

        module.realpath = memoized


_install_realpath_ceiling()
'''
p.write_text(s)
print("patched")
