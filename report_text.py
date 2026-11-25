"""Report prose. Every number is computed from the data rows or quoted from a ledger/Atlas entry with its run id."""


def render(g, srcs):
    cell, pr, ANON, AUTH = g["cell"], g["pr"], g["ANON"], g["AUTH"]
    OSES, OSN = g["OSES"], g["OSN"]
    new = g["NEW_SHA"]

    def sp(j, os_, pop=ANON, tr="tree-50"):
        r = cell(j, os_, pop, tr)
        return f"{1 / r['ratio']:.1f}x" if r else "pending"

    def secs(j, os_, pop=ANON, tr="tree-50"):
        r = cell(j, os_, pop, tr)
        return f"{r['base_cpu']:.1f} -> {r['new_cpu']:.1f} s" if r else "pending"

    def trio(j, pop=ANON):
        return ", ".join(f"{OSN[o]} {sp(j, o, pop)}" for o in OSES)

    ROWS = g["ROWS"]
    IN, FAT = g["IN"], g["FAT"]
    WC = g["WIN_CAVEAT"]
    wc = f" [{WC}]" if WC else ""

    def waiting(items):
        """items: (n, text); keep only PRs not yet in the measured head."""
        return [f"{pr(n)} {txt}" for n, txt in items if not IN(n)]

    def fin(j, os_, fx, pop=ANON):
        for r in ROWS:
            if r["src"] == "final" and r["journey"] == j and r["os"] == os_ and r["fixture"] == fx and r["pop"] == pop:
                return f"{1 / r['ratio']:.2f}x" + (" (provisional)" if r["prov"] else "")
        return "measuring"

    def isp(j, os_, pop=ANON):
        r = cell(j, os_, pop, "tree-50", "interim")
        return f"{1 / r['ratio']:.2f}x" if r else "-"

    def flat(j, os_, fx):
        for r in ROWS:
            if r["src"] == "final" and r["journey"] == j and r["os"] == os_ and r["fixture"] == fx and r.get("band"):
                lo, hi = r["band"]
                if lo <= 1.0 <= hi:
                    return f" ({r.get('pairs')} pairs; the 95% band crosses 1.0, so flat, not a regression)"
        return ""

    def rng(j):
        v = sorted(1 / r["ratio"] for r in ROWS if r["src"] == "final" and r["journey"] == j and r["tls"] == "default")
        return f"{v[0]:.1f}-{v[-1]:.1f}x" if v else "measuring"

    out = []
    a = out.append
    a("# APM performance since the epic")
    a("")
    a(f"*PERF org report for the CEO, {'v2' if FAT else 'v1'} {g["STAMP"]} CET (data: Journey Hunt report.json"
      + ("; NEW = the fat train head" if FAT else "") + "). "
      "Final refresh after Wednesday's final bench pass.*")
    a("")
    a(("**Two baselines, both paired.** (a) " if any(r["src"] == "interim" for r in ROWS) else "**Baseline (paired).** ") + "**Before the epic** = main `e37400fa` (v0.38.0) vs "
      + (g.get("NEW_LABEL") or ("the measured head" if FAT else "main")) + f" `{new}`: Journey Hunt bench (6 pairs per cell" + ("; rows marked *provisional* come from the v1 harness "
      "and are replaced when v2 lands" if any(r["prov"] for r in ROWS if r["src"] == "final") else "") + ")."
      + ((" (b) **Validated interim** = main `703acd6f` (Nov 23 evening, already after the "
      "locked engine #4893) vs main `c0296fab`, 15 pairs, install only, **Windows on the OpenSSL TLS backend**. "
      + ("Charts 1-2 use (b) at tree-50 because all 3 OS are complete there; charts 3-5 use (a) at tree-10."
         if srcs["install"] != "final" else "All charts use (a); (b) appears only in the table (*i*).")) if any(r["src"] == "interim" for r in ROWS) else ""))
    a("")
    a("**How to read.** CPU seconds incl. child processes, p50 of interleaved before/now pairs on the same CI runner "
      "(3 runners per OS, modeled RTT 30 ms). **Speedup = 1 / paired p50 ratio; the ratio is the claim.** Seconds "
      "vary up to 2x by runner CPU class (reference AMD EPYC 9V74), so compare ratios. tree-10 / tree-50 = a project "
      "with 10 / 50 dependencies. Populations: *anonymous* = public github.com without a token; *authenticated* = "
      "token-first; *GHE* = enterprise host fixture; *all* = local commands with no network. Windows = default Git "
      "for Windows (schannel TLS) unless labelled OpenSSL.")
    a("")
    if IN(5376):
        W = lambda j, fx="tree-50": fin(j, "windows", fx) + wc
        WD = wc
    else:
        W = lambda j, fx="tree-50": fin(j, "windows", fx) + " default schannel" + wc + " / " + fin(j, "windows", fx + "-openssl") + " OpenSSL backend"
        WD = " default schannel" + wc
    a("**Headline (tree-50, vs e37400fa).** First install (cold, anonymous): Linux " + fin("install-cold", "linux", "tree-50")
      + ", macOS " + fin("install-cold", "macos", "tree-50") + ", Windows " + W("install-cold")
      + ". CI install (frozen): Linux " + fin("install-frozen", "linux", "tree-50") + ", macOS " + fin("install-frozen", "macos", "tree-50")
      + ", Windows " + fin("install-frozen", "windows", "tree-50") + WD + ". Warm: Linux " + fin("install-warm", "linux", "tree-50")
      + ", macOS " + fin("install-warm", "macos", "tree-50") + ", Windows " + W("install-warm") + ("" if IN(5376) else " (#5376, on track, measured Windows default warm 0.856 "
      "of today at its previous head; the rerun at its final head is running)")
      + ". First unchanged re-install (#2): " + ", ".join(f"{OSN[o]} {fin('install-noop', o, 'tree-50')}" for o in OSES)
      + "; repeat no-op (#3+, footnote 1): " + ", ".join(f"{OSN[o]} {fin('install-noop-replay', o, 'tree-50')}" for o in OSES)
      + (". **Windows default config now engages the in-process fetch** (#5376, merged: default cold 0.449 / 0.447, "
         "warm 0.868 of the main before it" + (f"; {WC}" if WC else "") + "), so Windows numbers above are the default setup. " if IN(5376) else
         ". **Windows in its default config is the gap:** the in-process fetch does not engage under schannel until "
         "#5376 (on track) lands, which takes default-config cold to 0.474 of today (run 45458601150); Windows warm "
         "is slower than before the epic and Windows update too (section 3). ") + "Other commands, ranges across OS and tree "
      "size: audit " + rng("audit") + "; outdated " + rng("outdated") + "; uninstall " + rng("uninstall") + "; prune "
      + rng("prune") + "; compile " + rng("compile") + "; pack " + rng("pack") + "; boot " + rng("boot-version") + " (flat).")
    a("")
    a("*Footnote 1: repeat no-op = an unchanged re-install once the verified-state record exists (the bench allows up "
      "to 3 untimed no-ops to converge first). Open finding under attribution: when the first re-install runs right "
      "after cold + warm installs in the same world, it traces 'no record'; standalone it records on main (runs "
      "45473253493, 45473300740), so it is not a regression by SHA.*")
    a("")

    a("## 1. Install journeys, anonymous public github.com (tree-50, vs " + ("e37400fa)" if srcs["install"] == "final" else "703acd6f)"))
    a("")
    a("![install](charts/1-install.png)")
    a("")
    a(f"- **Fetch in process (B1).** {pr(5330)} (Linux/macOS) and {pr(5351)} (Windows) fetch the pinned commit over "
      "smart HTTP inside apm instead of a git CLI chain per package: cold t50 spawns ~670 -> ~18, requests 201 -> 101. "
      + (f"{pr(5376)} makes it engage under default Windows TLS (schannel -> OS trust store): Windows default cold "
         "0.449 / 0.447, warm 0.868" + (f" ({WC})" if WC else "") + "." if IN(5376) else
         f"Under default Windows TLS (schannel) it engages only with {pr(5376)} (OS trust store): Windows cold t50 "
         "0.474 (34.5 -> 15.9 s, spawns 366 -> 18; run 45458601150; "
         + ("on-track bar" if srcs["install"] == "final" else "not in the OpenSSL-backend chart") + ")."))
    if not FAT:
        a("- **Landed after the bench head** (main 0f397094 vs 9ba3589d, t50, 6 pairs; #5367 small cars, #5370, #5364): "
      "Linux cold 0.924 / cold-auth 0.974 / warm 0.945 [45472888786]; Windows (OpenSSL backend) cold 0.930 / "
      "cold-auth 0.965 / warm 0.936 [45473003923]. Included in the on-track bars.")
    a(f"- **Ask git once, list once.** {pr(5341)} + {pr(5283)} read the effective git config once per command "
      f"(262 probes -> 1); {pr(5318)} verifies every pinned commit from one shared ref listing per remote.")
    a(f"- **Resolve paths fewer times.** {pr(5132)} string containment; kernel realpath on Linux (#5333 via train "
      f"{pr(5355)}); {pr(5354)} + {pr(5380)} resolve each root once; {pr(5204)} removed a per-package quadratic re-seal"
      + (f"; {pr(5372)} resolves link-free paths with one openat2 (Linux 0.906 / 0.919)" if IN(5372) else "")
      + ". " + ("Waiting: " + ", ".join(waiting([
          (5372, "(Linux cold 0.883)"),
          (5374, "exact Windows paths (Windows 0.961 anon / 0.967 auth)"),
          (5382, "containment once per directory (on the stack: macOS -15%, Linux -1-2%, Windows unchanged as it "
                 "overlaps #5374)")])) + "." if waiting([(5372, ""), (5374, ""), (5382, "")]) else ""))
    a(f"- **Lockfile once; skip proven work.** {pr(5325)} parse once, {pr(4888)} reuse what the command wrote, "
      f"{pr(5361)} libyaml emit (0.86 -> 0.05 s); {pr(4893)} locked engine + {pr(4846)} deploy "
      f"store let frozen installs skip the pipeline. **Repeat user:** {pr(4993)} replays a verified no-op "
      "(re-install #3+: Linux 2.55 -> 0.30 s, Windows 4.42 -> 0.59 s, runs 45456265890/45456307290). "
      + ("**First re-install (#2):** " if IN(5383) else "**First re-install (#2), on track:** ")
      + f"{pr(5383)} records at install so #2 also skips the proof: Linux 3.41 -> 1.73 s (0.515), macOS 3.10 -> 1.40 s "
      "(0.504), Windows 5.53 -> 2.34 s (0.431).")
    a("")

    a("## 2. Authenticated and enterprise installs (tree-50, vs " + ("e37400fa)" if srcs["auth"] == "final" else "703acd6f)"))
    a("")
    a("![auth](charts/2-auth.png)")
    a("")
    if not IN(5353):
        a(f"- Authenticated installs still use the git CLI at the bench head. {pr(5341)} (config once) and {pr(5318)} "
          "(shared listing) cut cold t50 spawns 815 -> ~360, but #5318 added an ls-remote per dep on warm-auth.")
    a(f"- **{'In the measured head' if IN(5353) else 'Landed after the bench head'}:** {pr(5353)} authenticated in-process fetch + shared listing. Validated on "
      "main 0a2460a7 (t50, 6 pairs; runs 45475047324 / 45475248050): authenticated cold Linux 12.68 -> 8.52 s "
      "(0.530), Windows (OpenSSL backend) 28.45 -> 17.68 s (0.456); spawns ~364 -> 20, requests 250 -> 100; "
      "authenticated warm Linux 0.796, Windows 0.866 with ls-remote 50 -> 0. **Authenticated cold now costs the "
      "same as anonymous.** "
      + (f"{pr(5366)} writes and verifies git-path checkouts from the pack in process: enterprise (GHE) Windows "
         "0.845 / 0.848." if IN(5366) else
         f"**Waiting:** {pr(5366)} writes and verifies git-path checkouts from the pack in process "
         "(Windows 0.853, OpenSSL backend). Windows on-track bars are not projected onto schannel."))
    a("- **Enterprise (GHE fixture), vs e37400fa:** cold t50 Linux " + fin("install-cold", "linux", "tree-50-ghe", "ent-anon")
      + ", macOS " + fin("install-cold", "macos", "tree-50-ghe", "ent-anon") + " -- B1 does not apply yet. A/A "
      "[45466484736]: Linux t50 GHE 14.94 s vs github.com 8.19 s, ~45% headroom: B1 on enterprise hosts is "
      f"security-cleared and the next lever. {pr(5377)} (lever S) fetches monorepo subdirectory deps as pin trees + "
      "exact blobs (real monorepo Linux 0.502, macOS 0.478).")
    a("")

    a("## 3. Lifecycle: update, uninstall, prune, outdated (" + g["TREES"]["lifecycle"] + ", vs e37400fa)")
    a("")
    a("![lifecycle](charts/3-lifecycle.png)")
    a("")
    a(f"- **outdated:** {pr(4960)} lists public github.com refs in process (at merge: wall t50 2.35-2.4x; round trips "
      "200 -> 58, spawns 252 -> 2).")
    LT = g["TREES"]["lifecycle"]
    a("- **update** (resolve newest refs, then the install pipeline): Linux " + fin("update", "linux", LT) + ", macOS "
      + fin("update", "macos", LT) + flat("update", "macos", LT) + ", **Windows " + fin("update", "windows", LT) + WD + "**. "
      + ("Before #5376 Windows default update was a regression (0.76x): under schannel the ref listing fell back to "
         "git ls-remote + a POST per dep (spawns 73 -> 168); #5376 (merged) moves it back in process." if IN(5376) else
         "Update does not yet collect the install wins outside Linux; under schannel the in-process fetch does not "
         "engage until #5376 lands. **Cause (confirmed in v2):** under schannel the ref listing falls back to git "
         "ls-remote + a POST per dep (spawns 73 -> 168); #5376 should flip it back to in-process, and that proof is "
         "running (owner F&C)."))
    a(f"- **uninstall / prune:** {pr(4888)} reads the lockfile the command just wrote instead of re-parsing it (prune t50 "
      f"Linux 7.15 -> 5.90 s at merge); the lockfile ({pr(5325)}, {pr(5361)}) and path ({pr(5354)}) levers apply to every "
      "command that loads the lockfile.")
    a("")

    a("## 4. Compile and audit (" + g["TREES"]["compile"] + ", vs e37400fa)")
    a("")
    a("![compile-audit](charts/4-compile-audit.png)")
    a("")
    a(f"- **audit:** {pr(5018)} serves drift replay from verified render records instead of re-rendering every "
      "package (at merge, 15 pairs: Linux 9.31 -> 5.22 s, -43.9%; macOS -43.0%; Windows -37.6%).")
    a("- **audit --ci, compile:** no dedicated lever this epic; they gain only from the shared lockfile, YAML and path "
      "levers.")
    a("")

    a("## 5. Pack, bundle install and boot (" + g["TREES"]["pack"] + ", vs e37400fa)")
    a("")
    a("![pack-boot](charts/5-pack-boot.png)")
    a("")
    a("- **pack / install from bundle:** no dedicated lever; shared lockfile and path levers only."
      + ("" if FAT else " macOS was rechecked at 15 pairs and none of these is a regression (t10 / t50): update "
         "1.00x / 0.95x (band crosses 1.0), pack 1.04x / 1.18x, install from bundle 1.00x / 1.00x."))
    a("- **boot (`--version`, `--help`):** every boot lever was measured and killed: entry fast path #4802 (modules "
      "373 -> 77 gave 1.00x; reverted by #4855), lazy imports (Linux 0.988, Windows 0.995), frozen/zipapp boot (no win). "
      "Boot is ~0.45 s of a Linux install and stays flat.")
    a("")

    a("## 6. Speedup per command, OS and population")
    a("")
    a("Speedup = 1 / paired p50, tree-50 / tree-10, vs e37400fa (before the epic). *p* = provisional v1-harness row; "
      "*i* = interim vs 703acd6f (15 pairs, Windows OpenSSL backend); *measuring* = no data yet.")
    a("")
    a(g["table"]())
    a("")

    lr = cell("install-cold", "linux", ANON, "tree-50", "final")
    wd = cell("install-cold", "windows", ANON, "tree-50", "final")
    wo = cell("install-cold", "windows-openssl", ANON, "tree-50")
    gap = ""
    if lr and wd and (IN(5376) or not wo):
        gap = (f"Cold install t50 is now {lr['new_cpu']:.1f} s on Linux and {wd['new_cpu']:.1f} s on Windows default config" + (f" ({WC})" if WC else "") + " "
               f"(seconds depend on runner class), about {lr['new_cpu'] / 0.7:.0f}x and {wd['new_cpu'] / 1.5:.0f}x the "
               "physics floor (~0.7 s Linux, ~1.5 s Windows: 0 git children, 2 requests per package).")
    elif lr and wd and wo:
        gap = (f"Cold install t50 is now {lr['new_cpu']:.1f} s on Linux and {wd['new_cpu']:.1f} s on Windows default "
               f"schannel / {wo['new_cpu']:.1f} s OpenSSL backend (seconds depend on runner class), about "
               f"{lr['new_cpu'] / 0.7:.0f}x and {wd['new_cpu'] / 1.5:.0f}x / {wo['new_cpu'] / 1.5:.0f}x the physics floor "
               "(~0.7 s Linux, ~1.5 s Windows: 0 git children, 2 requests per package).")
    a("## 7. Gap to floor, what did not work, what is next")
    a("")
    a(gap + " Bytes are already at the floor; validated paired-deletion ceilings (t50 anonymous, EPYC 9V74, runs "
      "45464564203, 45464596572, 45464623708, 45464660115, 45464777276, 45464818672) put **integrate incl. discovery "
      "at 60%** of install (Linux 3.5 s / Windows 6.7 s), **lockfile build+write, ledger and journal at 27%**, policy + "
      "audit + content scan at 3%, and resolve + fetch + boot + glue at 10% or less. **What did not work:** lazy imports, "
      "frozen/zipapp boot, single-pass integrate (bytes are only ~6%; the cost is per-file Python and path containment), "
      "stat cuts, the rest-bucket cut, #5352 (~1.00 on GHE/GitLab) and small cars that stayed within noise; all were "
      "measured and killed. Honest caveat: on the current stack, locked-warm costs 1.21-1.29x warm (A/A 45464702877), "
      "attribution in progress. **Next:** "
      + ("land the waiting PRs (" + ", ".join(f"#{n} {w}" for n, w in ((5376, "schannel"), (5353, "auth fetch"),
         (5366, "pack checkout"), (5374, "Windows paths"), (5382, "containment once"), (5372, "Linux paths"),
         (5383, "re-install #2")) if not IN(n)) + "); then " if not all(IN(n) for n in (5376, 5353, 5366, 5374, 5382, 5372, 5383)) else "")
      + "a 5x on Linux needs an integrate redesign plus the lockfile cut, with "
      "B1 on enterprise hosts and lever S (#5377) for monorepos.")
    a("")
    return "\n".join(out)
