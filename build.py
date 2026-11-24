"""Build the CEO performance report: charts/*.png, report.md, report.html, report.pdf.

Data (never invented):
  final   = table.json from collect.py (JH requests.log, BASE e37400fa vs NEW main)
  interim = JH rn039/table.json (703acd6f -> c0296fab, 15 pairs, W OpenSSL backend);
            used only for install charts while the final bench is pending; its
            install-noop row is VOID (journey-order confound, Atlas perf row 34).
On-track bars = now x measured paired ratios of waiting (open) PRs, from the PERF ledger.
"""
import json, math, os, subprocess, sys, datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CH = os.path.join(HERE, "charts")
INTERIM = "/Users/danielmeppiel/.copilot/session-state/3e13e13b-ca92-4e58-8ec4-98ccb45457df/files/rn039/table.json"
FINAL = os.path.join(HERE, "table.json")
REQLOG = "/Users/danielmeppiel/.copilot/session-state/3e13e13b-ca92-4e58-8ec4-98ccb45457df/files/report-data/requests.log"
BASE_FINAL = "e37400fa"
OSES = ["linux", "macos", "windows"]
OSN = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}
C_BEFORE, C_NOW, C_TRACK = "#a6a6a6", "#1f5fa8", "#ffffff"

ANON, AUTH, ENTA, ENTU = "anon", "auth", "ent-anon", "ent-auth"

SHORT = {"install-cold": "cold", "install-frozen": "frozen", "install-warm": "warm",
         "install-noop": "re-inst.\n#2", "install-noop-replay": "repeat\nno-op"}

JLABEL = {
    "install-cold": "cold\n(first install)",
    "install-frozen": "frozen\n(CI, lockfile)",
    "install-warm": "warm\n(cache filled)",
    "install-noop": "re-install #2\n(unchanged)",
    "install-noop-replay": "re-install #3+\n(repeat user)",
    "install-cold-auth": "cold",
    "install-warm-auth": "warm",
    "update": "update",
    "uninstall": "uninstall",
    "prune": "prune",
    "outdated": "outdated",
    "compile": "compile",
    "audit": "audit",
    "audit-ci": "audit --ci",
    "pack": "pack",
    "install-bundle": "install\nfrom bundle",
    "boot-version": "apm --version",
    "boot-help": "apm --help",
}
TNAME = {k: v.replace("\n", " ") for k, v in JLABEL.items()}
TNAME.update({"install-cold": "install cold", "install-frozen": "install frozen",
              "install-warm": "install warm", "install-noop": "re-install #2 (unchanged)",
              "install-noop-replay": "re-install #3+ (repeat user)",
              "install-cold-auth": "install cold", "install-warm-auth": "install warm"})


# Waiting (open) PRs with a measured paired ratio on the cell (PERF ledger, VP reads).
ON_TRACK = {
    ("install-cold", "windows", ANON): [(5374, 0.961, "VP 20:49, 186e16d8")],
    ("install-cold", "linux", ANON): [(5372, 0.883, "ledger 19:47"), (5382, 0.99, "VP 20:49, on the stack")],
    ("install-cold", "macos", ANON): [(5372, 0.954, "ledger 19:47"), (5382, 0.845, "VP 20:49, on the stack")],
    ("install-warm", "linux", ANON): [(5372, 0.897, "ledger 19:47")],
    ("install-warm", "macos", ANON): [(5372, 0.945, "ledger 19:47")],
    ("install-noop", "linux", ANON): [(5383, 0.515, "45464326641")],
    ("install-noop", "macos", ANON): [(5383, 0.504, "45464458778")],
    ("install-noop", "windows", ANON): [(5383, 0.431, "45464401759")],
    ("install-cold-auth", "linux", AUTH): [(5353, 0.578, "ledger 17:25"), (5366, 0.960, "ledger 18:33")],
    ("install-cold-auth", "windows", AUTH): [(5353, 0.503, "ledger 17:25"), (5366, 0.853, "ledger 18:33")],
}


REPORT = os.environ.get("PERF_REPORT_JSON") or \
    "/Users/danielmeppiel/.copilot/session-state/3e13e13b-ca92-4e58-8ec4-98ccb45457df/files/report-data/report.json"
SNAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _find_report():
    """Newest */files/report-data/report.json under session-state whose base is e37400fa (JH may be re-hired)."""
    import glob as _g
    best = None
    for p in _g.glob("/Users/danielmeppiel/.copilot/session-state/*/files/report-data/report.json"):
        try:
            if str(json.load(open(p)).get("base", "")).startswith("e37400fa"):
                if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                    best = p
        except Exception:
            continue
    return best


if not os.path.exists(REPORT) and not os.environ.get("PERF_REPORT_JSON"):
    REPORT = _find_report() or REPORT
if os.path.exists(REPORT):
    import shutil as _sh
    os.makedirs(SNAP, exist_ok=True)
    _sh.copyfile(REPORT, os.path.join(SNAP, "latest.json"))
    _sh.copyfile(REPORT, os.path.join(SNAP, datetime.datetime.now().strftime("report-%Y%m%d-%H%M.json")))
elif os.path.exists(os.path.join(SNAP, "latest.json")):
    REPORT = os.path.join(SNAP, "latest.json")
ANON, AUTH, ENTA, ENTU = "anon", "auth", "ent-anon", "ent-auth"


def popkey(fixture, journey):
    e = "ghe" in fixture
    a = journey.endswith("-auth")
    return (ENTU if a else ENTA) if e else (AUTH if a else ANON)


def load():
    rows = []
    meta = {}
    if os.path.exists(REPORT):
        d = json.load(open(REPORT))
        meta = {k: d.get(k) for k in ("base", "new")}
        for r in d["rows"]:
            if not r.get("speedup"):
                continue
            if r["journey"] in ("install-noop", "audit-ci") and r.get("harness_rev") != "v2":
                continue  # valid only on the v2 harness (JH 20:4x)
            h = r.get("headline") or {}
            rows.append({"src": "final", "journey": r["journey"], "os": r["os"], "fixture": r["fixture"],
                         "ratio": 1 / r["speedup"], "base_cpu": h.get("base_s", r["base_cpu_s"]),
                         "new_cpu": h.get("new_s", r["new_cpu_s"]), "cls": h.get("cpu_class", "?"),
                         "ref": h.get("reference_class", False), "quality": r.get("quality", ""),
                         "rev": r.get("harness_rev"), "run": r.get("run"), "band": r.get("band"), "pairs": r.get("pairs")})
    for r in (json.load(open(INTERIM)) if os.path.exists(INTERIM) else []):
        if r["base"] == "703acd6f" and r["journey"] != "install-noop":
            rows.append({"src": "interim", "journey": r["journey"], "os": r["os"], "fixture": r["fixture"],
                         "ratio": r["ratio"], "base_cpu": r["base_cpu"], "new_cpu": r["new_cpu"], "cls": "pooled",
                         "ref": False, "quality": "clean", "run": r["run"]})
    for r in rows:
        r["pop"] = popkey(r["fixture"], r["journey"])
        r["tree"] = r["fixture"].split("-ghe")[0].split("-openssl")[0]
        r["tls"] = "OpenSSL" if ("openssl" in r["fixture"] or (r["src"] == "interim" and r["os"] == "windows")) else "default"
        r["prov"] = "provisional" in r["quality"]
    return rows, meta


ROWS, META = load()
NEW_SHA = (META.get("new") or "9ba3589d")[:8]
OLD_NEW = "9ba3589d"
FAT = os.environ.get("PERF_REPORT_FORCE_FAT") == "1" or NEW_SHA != OLD_NEW
STAMP = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def cell(j, os_, pop, tr="tree-50", src=None):
    if os_ == "windows-openssl":
        cands = [r for r in ROWS if r["journey"] == j and r["os"] == "windows" and r["pop"] == pop and r["tree"] == tr
                 and r["src"] == "final" and r["tls"] == "OpenSSL"]
        return cands[0] if cands else None
    cands = [r for r in ROWS if r["journey"] == j and r["os"] == os_ and r["pop"] == pop
             and r["tree"] == tr and (src is None or r["src"] == src)
             and (r["tls"] == "default" or r["src"] == "interim")]
    cands.sort(key=lambda r: (r["src"] != "final", r.get("prov", False)))
    return cands[0] if cands else None


# Final (e37400fa -> 9ba3589d) charts: factors from PRs landed after the bench head ("L") or waiting ("W").
# #5382 is scoped off win32 (overlaps #5374); factors measured on the stack (VP 20:49).
ON_TRACK_FINAL = {
    ("install-cold", "linux", ANON): [(5367, 0.924, "45472888786"), (5372, 0.883, "ledger 19:47"), (5382, 0.99, "VP 20:49")],
    ("install-cold", "macos", ANON): [(5372, 0.954, "ledger 19:47"), (5382, 0.845, "VP 20:49")],
    ("install-cold", "windows", ANON): [(5376, 0.474, "45458601150"), (5374, 0.961, "VP 20:49, 186e16d8")],
    ("install-cold", "windows-openssl", ANON): [(5367, 0.930, "45473003923"), (5374, 0.961, "VP 20:49, 186e16d8")],
    ("install-warm", "linux", ANON): [(5367, 0.945, "45472888786"), (5372, 0.897, "ledger 19:47")],
    ("install-warm", "macos", ANON): [(5372, 0.945, "ledger 19:47")],
    ("install-warm", "windows", ANON): [(5376, 0.856, "VP 21:47, previous head")],
    ("install-warm", "windows-openssl", ANON): [(5367, 0.936, "45473003923")],
    ("install-noop", "linux", ANON): [(5383, 0.515, "45464326641")],
    ("install-noop", "macos", ANON): [(5383, 0.504, "45464458778")],
    ("install-noop", "windows", ANON): [(5383, 0.431, "45464401759")],
    ("install-cold-auth", "linux", AUTH): [(5353, 0.530, "45475047324, main 0a2460a7"), (5366, 0.960, "ledger 18:33")],
    ("install-warm-auth", "linux", AUTH): [(5353, 0.796, "45475047324, main 0a2460a7")],
    # #5353/#5366 Windows ratios were measured on the OpenSSL backend; not projected onto schannel.
    ("install-cold-auth", "windows", AUTH): [],
}


def IN(n):
    """PR n is part of the measured NEW head (bench re-run at the fat train head and n merged)."""
    return FAT and gh_state(n) == "MERGED"


def ontrack(j, os_, pop, now, src="interim"):
    if src == "final" or os_ == "windows-openssl":
        prs = ON_TRACK_FINAL.get((j, os_, pop))
        if FAT and prs:
            prs = [p for p in prs if gh_state(p[0]) == "OPEN"]
    else:
        prs = ON_TRACK.get((j, os_, pop))
    if not prs or now is None:
        return None, []
    return now * math.prod(p[1] for p in prs), prs


def chart(name, journeys, pop, tr, src, track=True, height=2.3, openssl=False):
    openssl = openssl and not IN(5376)
    panels = OSES + (["windows-openssl"] if openssl else [])
    fig, axes = plt.subplots(1, len(panels), figsize=(7.4, height), sharey=False,
                             gridspec_kw={"width_ratios": [len(journeys)] * 3 + ([2] if openssl else [])})
    any_track = any_prov = False
    for ax, os_ in zip(axes, panels):
        if os_ == "windows-openssl":
            js_here = [j for j in journeys if cell(j, os_, pop, tr)]
        else:
            js_here = journeys
        w = 0.27 if track else 0.38
        for i, j in enumerate(js_here):
            r = cell(j, os_, pop, tr, src)
            if not r:
                ax.text(i, 0, "measuring", ha="center", va="bottom", fontsize=6, color="#888", rotation=90)
                continue
            b, n = r["base_cpu"], r["new_cpu"]
            xb, xn = (i - w, i) if track else (i - w / 2, i + w / 2)
            ax.bar(xb, b, w, color=C_BEFORE)
            ax.bar(xn, n, w, color=C_NOW)
            star = "*" if r["prov"] else ""
            any_prov = any_prov or r["prov"]
            ax.text(xn - (w * 0.25 if track else 0), n, f"{1 / r['ratio']:.1f}x{star}", ha="center", va="bottom", fontsize=6.5 if len(journeys) < 5 else 5.6,
                    color=C_NOW, fontweight="bold")
            if track:
                t, prs = ontrack(j, os_, pop, n, src)
                if t:
                    any_track = True
                    ax.bar(i + w, t, w, color=C_TRACK, edgecolor=C_NOW, hatch="////", linewidth=0.6)
                    ax.text(i + w * 1.25, t, f"{b / t:.1f}x", ha="center", va="bottom", fontsize=5.4, color="#555")
        ax.set_xticks(list(range(len(js_here))))
        lab = SHORT if len(journeys) > 4 else JLABEL
        ax.set_xticklabels([lab.get(j, JLABEL[j]) for j in js_here], fontsize=6.0 if len(panels) > 3 else 6.3)
        if os_ == "windows-openssl":
            ax.set_title("Windows\n(OpenSSL)", fontsize=7.5)
        elif os_ == "windows":
            ax.set_title(("Windows\n(default config)" if IN(5376) else "Windows\n(default: schannel)") if src == "final" else "Windows\n(OpenSSL backend)", fontsize=7.5)
        else:
            ax.set_title(OSN[os_], fontsize=8)
        ax.tick_params(axis="y", labelsize=6.5)
        ax.spines[["top", "right"]].set_visible(False)
        ax.margins(y=0.18)
    axes[0].set_ylabel("CPU seconds (p50)", fontsize=7)
    from matplotlib.patches import Patch
    base_lbl = f"Before the epic (e37400fa)" if src == "final" else "Before (703acd6f, Nov 23 eve)"
    now_lbl = f"Now (main {NEW_SHA})" if src == "final" else "Now (main c0296fab)"
    h = [Patch(color=C_BEFORE, label=base_lbl), Patch(color=C_NOW, label=now_lbl + "; label = speedup"
                                                       + ("; * = provisional" if any_prov else ""))]
    if any_track:
        h.append(Patch(facecolor=C_TRACK, edgecolor=C_NOW, hatch="////",
                       label="On track: now x ratios of PRs landed since / waiting"))
    fig.legend(handles=h, loc="upper center", ncol=3, fontsize=6.3, frameon=False, bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    fig.savefig(os.path.join(CH, name), dpi=200)
    plt.close(fig)
    return src


def fmt(r):
    if not r:
        return "-"
    tag = "i" if r["src"] == "interim" else ("p" if r["prov"] else "")
    return f"{1 / r['ratio']:.2f}x{tag}"


def table():
    order = [("install-cold", ANON), ("install-frozen", ANON), ("install-warm", ANON), ("install-noop", ANON),
             ("install-noop-replay", ANON), ("install-cold-auth", AUTH), ("install-warm-auth", AUTH),
             ("install-cold", ENTA), ("install-warm", ENTA), ("install-noop", ENTA), ("install-cold-auth", ENTU),
             ("update", ANON), ("uninstall", ANON), ("prune", ANON), ("outdated", ANON),
             ("compile", ANON), ("audit", ANON), ("audit-ci", ANON), ("pack", ANON), ("install-bundle", ANON),
             ("boot-version", ANON), ("boot-help", ANON)]
    popshort = {ANON: "anonymous", AUTH: "authenticated", ENTA: "GHE anonymous", ENTU: "GHE authenticated"}
    out = ["| command | population | Linux t50 / t10 | macOS t50 / t10 | Windows t50 / t10 |",
           "|---|---|---|---|---|"]
    for j, pop in order:
        cs = []
        for os_ in OSES:
            a, b = cell(j, os_, pop, "tree-50"), cell(j, os_, pop, "tree-10")
            cs.append(f"{fmt(a)} / {fmt(b)}" if (a or b) else "measuring")
        if j in ("install-noop-replay", "install-noop", "update", "uninstall", "prune", "outdated", "compile",
                 "audit", "audit-ci", "pack", "install-bundle", "boot-version", "boot-help") and pop == ANON:
            p = "all" if j.startswith(("install-noop", "compile", "audit", "pack", "install-bundle", "boot")) else popshort[pop]
        else:
            p = popshort[pop]
        out.append(f"| {TNAME[j]} | {p} | " + " | ".join(cs) + " |")
    return "\n".join(out)


def gh_state(n, cache={}):
    p = os.path.join(HERE, "pr-state.json")
    if not cache and os.path.exists(p):
        cache.update(json.load(open(p)))
    if str(n) not in cache:
        out = subprocess.run(["gh", "pr", "view", str(n), "--repo", "microsoft/apm", "--json", "state", "-q", ".state"],
                             capture_output=True, text=True).stdout.strip()
        cache[str(n)] = out or "?"
        json.dump(cache, open(p, "w"))
    return cache[str(n)]


def pr(n):
    s = gh_state(n).lower()
    s = {"merged": "merged", "open": "open", "closed": "closed"}.get(s, s)
    return f"[#{n}](https://github.com/microsoft/apm/pull/{n}) ({s})"


def main():
    nfinal = sum(r["src"] == "final" for r in ROWS)
    if nfinal < 50:
        sys.exit(f"only {nfinal} final rows (data {REPORT}); nothing written, Desktop copy NOT refreshed")
    if "--refresh-pr" in sys.argv and os.path.exists(os.path.join(HERE, "pr-state.json")):
        os.remove(os.path.join(HERE, "pr-state.json"))
    os.makedirs(CH, exist_ok=True)
    def full(js, pop):
        return all(cell(j, o, pop, "tree-50", "final") for o in OSES for j in js)
    i_js = ["install-cold", "install-frozen", "install-warm"]
    if full(i_js, ANON):
        s1 = chart("1-install.png", i_js + ["install-noop", "install-noop-replay"], ANON, "tree-50", "final", openssl=True, height=2.6)
    else:
        s1 = chart("1-install.png", i_js, ANON, "tree-50", "interim")
    a_js = ["install-cold-auth", "install-warm-auth"]
    s2 = chart("2-auth.png", a_js, AUTH, "tree-50", "final" if full(a_js, AUTH) else "interim", height=2.1)
    def tr_for(js):
        return "tree-50" if full(js, ANON) else "tree-10"
    TREES = {}
    for key, fn, js in (("lifecycle", "3-lifecycle.png", ["update", "uninstall", "prune", "outdated"]),
                        ("compile", "4-compile-audit.png", ["compile", "audit"]),
                        ("pack", "5-pack-boot.png", ["pack", "install-bundle", "boot-version", "boot-help"])):
        TREES[key] = tr_for(js)
        chart(fn, js, ANON, TREES[key], "final", track=False)
    s3 = s4 = s5 = "final"
    globals()["TREES"] = TREES
    srcs = {"install": s1, "auth": s2, "lifecycle": s3, "compile": s4, "pack": s5}
    json.dump({"srcs": srcs, "new": NEW_SHA, "final_rows": sum(r["src"] == "final" for r in ROWS)},
              open(os.path.join(HERE, "build-state.json"), "w"), indent=1)
    import report_text
    md = report_text.render(globals(), srcs)
    open(os.path.join(HERE, "report.md"), "w").write(md)
    bad = [c for c in md if ord(c) > 126 or (ord(c) < 32 and c not in "\n")]
    if bad:
        sys.exit(f"non-ASCII in report.md: {set(bad)}")
    import markdown
    body = markdown.markdown(md, extensions=["tables"])
    css = open(os.path.join(HERE, "style.css")).read()
    html = f"<!doctype html><html><head><meta charset='ascii'><style>{css}</style></head><body>{body}</body></html>"
    open(os.path.join(HERE, "report.html"), "w").write(html)
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={os.path.join(HERE, 'report.pdf')}", "file://" + os.path.join(HERE, "report.html")],
                   capture_output=True, timeout=120)
    import shutil
    if os.environ.get("PERF_REPORT_NO_COPY") == "1":
        print("Desktop copy skipped (PERF_REPORT_NO_COPY=1)")
        pdf = open(os.path.join(HERE, "report.pdf"), "rb").read()
        print("pages:", pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages"))
        return
    dst = "/Users/danielmeppiel/Desktop/apm-perf-report"
    os.makedirs(os.path.join(dst, "charts"), exist_ok=True)
    for f in ("report.pdf", "report.md"):
        shutil.copyfile(os.path.join(HERE, f), os.path.join(dst, f))
    for f in os.listdir(CH):
        if f.endswith(".png"):
            shutil.copyfile(os.path.join(CH, f), os.path.join(dst, "charts", f))
    pdf = open(os.path.join(HERE, "report.pdf"), "rb").read()
    print("pages:", pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages"), "srcs:", srcs,
          "final rows:", sum(r["src"] == "final" for r in ROWS))


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    main()
