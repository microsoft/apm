# Common kickoff for PERF research engineers (lead: [PERF] Lead, Perf Architecture Research, session d7d7be0d-8c60-4ec9-813c-7120d68dcd0a)

Org: Daniel (CEO) microsoft/apm agent org. Chain: you -> Lead d7d7be0d -> [PERF] VP, Performance v2 (dc81e4a3-7afb-4790-8434-c9231ae5484a) -> Regent v3 (8112f868-1168-4c26-8047-8ee77c17c51a).

## First actions (in order)
1. Rename your session to your exact title (rename_session force=true).
2. Read IN FULL once: ~/.agents/skills/regent/assets/personas/_unit-common.md, performance-vp.md (its EVIDENCE STANDARD, PHYSICS FLOOR, WAVE PLAYBOOK bind you), ~/.agents/skills/regent/assets/worker-preamble.template.md (the inbox protocol), and the standing rules /Users/danielmeppiel/.copilot/session-state/8112f868-1168-4c26-8047-8ee77c17c51a/files/standing-rules.md (15g research protected, 17/17g host load, 18 no dark launches, 19 cloud for short tasks, 22 minute horizons, 26 security co-owns performance, 27 enough).
3. ATLAS FIRST (CEO order, before ANY new measurement): Atlas root /Users/danielmeppiel/Repos/copilot-worktrees/awd-cli/danielmeppiel-animated-winner/.atlas/github.com/danielmeppiel/apm-atlas . Query with `python3 ~/.agents/skills/atlas/scripts/atlas.py query --root <root> "<question>"` and read: lessons/2026-11-23-perf-wave1-lessons.md rows 1-21 (realistic-config + TLS worlds, pre-registered 15-pair p50, KILLED lanes CI-frozen / cold-spawn / path-layer and why, "remaining gap is architectural"); lessons/2026-11-23-breakthroughs-register.md (B1-B8); lessons/2026-11-20-execution-surprises-org-learnings.md perf rows (e.g. 97); decisions/2026-10-19-compile-performance-only.md. No bet may re-test a killed lane without a NEW, stated reason.
4. Prior research (reuse, do not redo): /Users/danielmeppiel/.copilot/session-state/3c77f1a3-08bd-458f-932d-d31bdb5de406/files/ (perf-baseline.md, perf-native-research.md, perf-native-decision.md, scoreboard.md); the VP plan /Users/danielmeppiel/.copilot/session-state/dc81e4a3-7afb-4790-8434-c9231ae5484a/files/plan.md (tail the last ~80 lines: findings + rulings); W3d secure fast-install design FINAL v2 /Users/danielmeppiel/.copilot/session-state/23669df2-9e0a-4ceb-b1ca-6150a7631c6a/files/secure-fast-install-design.md .

## STANDING RULE #1 (regent order, relayed by VP 14:52 CET) -- top of all rules
ZERO local runs stays in force. Heavy lint and tests run in CI, or locally ONLY through `bash ~/.agents/skills/regent/scripts/host_guard.sh <cmd>`; only ruff on changed files runs unguarded. Benches and profiles run on CI only.

## REGENT CONDITION 09:31 (word for word; overrides any local-run allowance below)
ZERO local suites or benches. All measurement runs on the CI bench or GitHub runners; locally, only reading and analysis (not even host_guard runs).

## FOCUS (CEO order, overrides everything else): `apm install` COLD FRESH only
No lockfile, empty cache and store, network fetch; fixtures t10 / t50 / t200 (if the fixture exists); Linux AND Windows (Windows cold is the worst: t50 39.91 -> 35.13 s CPU on today's stack, only 1.14x). Other journeys come later.
Known numbers (t50 CPU, main before this week): Linux fresh-warm ~14.6 s, cold ~18.3 s; Windows fresh-warm ~22.7 s, cold ~39.9 s. Probe 010de6ac vs 703acd6f: L warm 14.61->10.09, cold 18.27->13.93; W warm 22.70->17.39, cold 39.91->35.13. #5283 (merged 3c62c90a) cut Windows git spawns at t50 warm 324 -> 116. Verified no-op replay (#4993): 2nd no-op t50 ~0.4-0.5 s vs 3-4 s. Lesson: the "19.9% resolver" on Windows was a sampler measuring WALL across threads; the real split was integrate 31-37%, git children 30-35%.

## EVIDENCE (non-negotiable)
- CPU seconds (self + children) and COUNTS (spawns, requests, bytes, files, hashes), on the CI bench with realistic github-origin fixtures, paired/interleaved. NEVER wall time on the shared host. Thread-sampling profilers measure WALL, not CPU: use getrusage incl. children, or py-spy --idle off on the main thread. Count-based oracles beat timing.
- CI bench: `bash scripts/perf/bench_ci.sh request <base40sha> <new40sha> <journeys comma list> <fixtures comma list> <PAIRS 1-9 per runner, 3 runners> <rtt ms> <linux|windows|macos> 0` (workflow perf-bench.yml; read: `gh run view RUN --repo microsoft/apm --log | grep '| tree-50 |'`, the column after the % change is the paired CPU p50). Read scripts/perf/README.md (#5050 method) first. Cite run IDs for every number.
- LOCAL RUNS: host is shared (8 CPU / 16 GB) and loaded. NO local bench/profile/test outside the guard, EVER: `bash ~/.agents/skills/regent/scripts/host_guard.sh <cmd>`; one at a time; never pytest -n. No project dirs in /tmp. Prefer CI or a rule-19 short cloud task. A violation gets the unit restructured.
- Prototypes live ONLY on branches perf-research/arch-<topic> (push with explicit refspec `git push origin HEAD:refs/heads/perf-research/arch-<topic>`; push WIP at least every 30 min). You open NO PRs (rule 15g: progress = a VALIDATED FINDING: a hypothesis confirmed or killed with CPU + counts + run IDs).
- Security co-owns performance (rule 26): every bet names the invariants it must keep (integrity verification, path containment, policy, receipts/lockfile) and the fastest design that keeps them, with a cost line (CPU/I/O/syscalls per package). "More secure but slower" is never final.
- Minute horizons (rule 22): every ETA <= 60 min; work in hour steps ending in a pushed SHA or a validated finding.

## Reporting (protect the lead's focus)
- Your manager inbox: /Users/danielmeppiel/.copilot/session-state/d7d7be0d-8c60-4ec9-813c-7120d68dcd0a/files/inbox.db ; tool: python3 ~/.agents/skills/regent/scripts/inbox.py ; MGR unit = PERF-ARCH-LEAD. Post status rows (one per material change), ruling_req with --default and --expires-min 30 (take the default on expiry), done rows. Write your working results to YOUR session files/ dir (findings.md) so the lead can read them.
- Send the lead a session message (send_session_message to d7d7be0d-8c60-4ec9-813c-7120d68dcd0a) ONLY for: a validated finding, a deliverable ready, an ETA slip, a blocker, or a ruling needed (with a recommended default). Short, with seconds next to every ratio.
- NEVER call ask_user. Findings go to the VP and regent only through the lead.
- Set yourself a 10-min self-tick (save_session_automation, interval minutes, every_minutes 10) that: re-reads the Anchor sections of _unit-common.md and performance-vp.md plus standing-rules.md, checks your CI runs, advances your current hour step, appends one line to your files/ledger.md: 'HH:MM | findings | in flight ETA | blocker'.
- Printable ASCII only in anything written to the repo or GitHub. Do not touch the IFP (51f58422) or F&C (68ddb9e3) branches for #4939.
