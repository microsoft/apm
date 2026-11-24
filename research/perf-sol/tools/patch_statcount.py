from pathlib import Path

p = Path("tests/benchmarks/cpu_attr.py")
s = p.read_text()

old = '''        _realpath_calls[owner] += 1
        _realpath_where[f"{where_class(path)} <- {owner}"] += 1
        return original(path, *args, **kwargs)'''
assert old in s
new = old + '''

    def make_stat_counter(name, fn):
        def counted_stat(path, *args, **kwargs):
            frame, owner, depth = sys._getframe(1), "<no apm frame>", 0
            while frame is not None and depth < 60:
                where = _where(frame.f_code)
                if where.startswith("apm_cli/"):
                    owner = f"{where}:{frame.f_code.co_name}"
                    break
                frame, depth = frame.f_back, depth + 1
            _stat_calls[f"{name} | {where_class(path)} <- {owner}"] += 1
            return fn(path, *args, **kwargs)

        return counted_stat

    import genericpath
    import ntpath
    import posixpath

    os.stat = make_stat_counter("stat", os.stat)
    os.lstat = make_stat_counter("lstat", os.lstat)
    for module in (ntpath, posixpath, genericpath):
        for fname in ("exists", "lexists", "isdir", "isfile", "islink"):
            fn = getattr(module, fname, None)
            if fn is not None:
                setattr(module, fname, make_stat_counter(fname, fn))'''
s = s.replace(old, new, 1)
s = s.replace(
    "_realpath_where = collections.Counter()\n",
    "_realpath_where = collections.Counter()\n_stat_calls = collections.Counter()\n",
    1,
)
old = '''        "realpath_calls_by_where_owner": top(_realpath_where, 120),'''
assert old in s
new = old + '''
        "stat_calls_total": sum(_stat_calls.values()),
        "stat_calls_by_kind_where_owner": top(_stat_calls, 150),'''
s = s.replace(old, new, 1)
p.write_text(s)
print("patched")
