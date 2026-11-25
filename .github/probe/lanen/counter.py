"""OS-level pass counter: directory listings, read opens and stats per path, bucketed."""
import builtins
import collections
import io
import json
import os
import atexit

_c = {k: collections.Counter() for k in ("list", "read", "stat")}


def install(out):
    root = os.path.abspath(os.getcwd())
    am = os.path.join(root, "apm_modules") + os.sep
    cache = os.path.abspath(os.environ.get("APM_CACHE_DIR", "/nonexistent")) + os.sep

    def key(p):
        try:
            return os.path.abspath(os.fspath(p))
        except Exception:
            return None

    def wrap(name, kind, mod=os):
        orig = getattr(mod, name)

        def f(path=".", *a, **k):
            if isinstance(path, (str, bytes, os.PathLike)):
                kp = key(path)
                if kp:
                    _c[kind][kp] += 1
            return orig(path, *a, **k)

        setattr(mod, name, f)

    wrap("scandir", "list"); wrap("listdir", "list"); wrap("stat", "stat"); wrap("lstat", "stat")
    oopen = builtins.open

    def bopen(file, mode="r", *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)) and not any(c in mode for c in "wax+"):
            kp = key(file)
            if kp:
                _c["read"][kp] += 1
        return oopen(file, mode, *a, **k)

    builtins.open = bopen
    io.open = bopen
    oosopen = os.open

    def osopen(path, flags, *a, **k):
        if not flags & (os.O_WRONLY | os.O_RDWR):
            kp = key(path)
            if kp:
                _c["read"][kp] += 1
        return oosopen(path, flags, *a, **k)

    os.open = osopen

    def bucket(p):
        if p.startswith(am):
            rest = p[len(am):].split(os.sep)
            return "apm_modules", os.sep.join(rest[:2])
        if p.startswith(cache):
            return "cache", ""
        if p.startswith(root + os.sep) or p == root:
            return "project", ""
        return "other", ""

    def dump():
        res = {}
        per_pkg = collections.defaultdict(lambda: collections.Counter())
        reread = []
        for kind, cnt in _c.items():
            agg = collections.defaultdict(lambda: [0, 0])
            for p, n in cnt.items():
                b, pkg = bucket(p)
                agg[b][0] += n
                agg[b][1] += 1
                if b == "apm_modules" and pkg:
                    per_pkg[pkg][kind] += n
                    per_pkg[pkg][kind + "_distinct"] += 1
                    if kind == "read" and n > 1:
                        reread.append((n, p[len(am):]))
            res[kind] = {b: {"calls": v[0], "distinct": v[1]} for b, v in agg.items()}
        pk = list(per_pkg.values())
        res["packages"] = len(pk)
        if pk:
            res["per_package_avg"] = {k: round(sum(c[k] for c in pk) / len(pk), 1)
                                      for k in ("list", "list_distinct", "read", "read_distinct", "stat", "stat_distinct")}
        res["most_reread_files"] = sorted(reread, reverse=True)[:15]
        with oopen(out, "w") as fh:
            json.dump(res, fh)

    atexit.register(dump)
