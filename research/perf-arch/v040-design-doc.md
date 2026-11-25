# APM v0.40.0 -- "5x of floor" install architecture (DRAFT)

Owner: [PERF] Lead, Perf Architecture Research. Co-signers: W3d (security), PERF VP.
Status: DRAFT, assembled 2026-11-25. Due WED 12:00 CET.
Scope: `apm install` cold fresh, warm and locked journeys at tree-50 (50 deps, 684 deployed files).

## 0. Summary

- **Target** (reference runner class EPYC 9V74, cold t50, default-on, 0 errors): Linux <= 3.5 s, Windows <= 7.5 s.
- **Main today (1e4a208e):** Linux 6.1 s, Windows 13.2 s. The physics floor is ~0.7 s Linux / ~1.2 s Windows, so the gap is 8.7x Linux / 11x Windows.
- **Where the gap is** (measured by paired deletion and a per-phase CPU probe on main):
  - integrate is ~45% Linux / ~50% Windows;
  - resolve + fetch is ~33%, of which the second copy + re-verify alone is 15-20% on every OS.
- **Architecture: one manifest-driven deploy.**
  - The in-process fetch emits a per-package manifest (path, mode, content hash, primitive class).
  - Materialization writes each file once (M2), and integrate consumes the manifest, with no re-walk, re-hash or re-classify.
  - Containment is proved by construction for APM-created trees.
- **Result** (stacked measured ceiling, ONE head = integrate ceiling ea0ec45e + M2 bound c2, unsafe upper bounds):
  - L 0.701 (~4.3 s on 9V74 vs 3.5; miss ~0.8 s) [45530064444].
  - M 0.663 [45530082924].
  - W [re-measuring on a correctly stacked #5374 head; first read 0.719 held as suspect].
  - The levers stack slightly better than paper (0.701 vs 0.716-0.734).
- **Native-core gate on Linux:** MET.
  - walk+stat+hash+copy = 45.5% of L CPU on main [45520769932] and 45.2% at the stacked head [45530101839] (65% with realpath).
  - Hash is 0.2%: the candidate is the filesystem-op layer, not hashing.
- **Linux bar (REGENT RULING):** L <= 3.5 s stays the North Star; v0.40 ships the Python manifest architecture at a measured ~4.3 s L milestone. A native fs-op core is a 48 h research lane (Lane N).

## 1. Physics floor per phase (on main)
[from SoL design-sec1-floor.md; table: phase share, seconds, floor, gap, method; confound notes]

## 2. Integrate redesign
[from Bets design-sec2-integrate.md]
- **Invariant first** (W3d co-signed 2.1, IR1-IR9).
- **Shapes:**
  - A: plan-once (+ parse-once);
  - B: bulk deploy (staged per regular file);
  - C: containment by construction.
- **Ceilings:**
  - W, main + #5374: 0.718 (-4.45 s).
  - L, A+B+parse+skill trees: 0.899. L residual: classify 1.15, preflight 0.82, acquire 0.75, skill copy 0.73 ms/file.
- **Plain statement:** the integrate redesign alone clears W, not L.

- _trusted_hash per-root memo (E2): NEUTRAL on main (L 1.001 [45528890914] / W 1.005 [45528948324]) and on the containment stub (L 0.994 / W 1.002). The ledger resolve chain is not a cost; the ledger's ~5% W (deletion 0.946 [45518297332]) is spread across per-file record/claim work. See #5384.

## 3. Materialize-once (M2)
- Today each file is fetched into the cache, copied into apm_modules, and the copy is re-verified (github_downloader.py:1922-1927).
- Deletion bound (c2): cold W 0.802 / L 0.817 / M 0.856; warm W 0.833 / L 0.794.
- The cost is Python per-file copy machinery (~1.4 ms Linux / 3.5 ms Windows per file).
- W3d ruled M1 and M2 OK (MS1-MS7) and M3 hardlinks rejected.

## 4. Manifest-driven deploy
- The manifest is NOT a trust root (W3d MD1-MD5, amended sentence verbatim from row w3d:ruling:manifest-deploy).
- Warm installs re-derive the manifest from pinned tree objects.

## 5. Security surface (consolidated)
GAPS RULED (W3d row w3d:ruling:v040-gaps, 03:10 CET, cost 0 each):
- G1: IR1 governs. "By construction" = every entry made by APM via exclusive, no-follow, per-file creates in THIS invocation: the lever-C landing, M2, an M1 per-file clone into an exclusive stage, and IR6 stages. copytree(symlinks=True), dir clonefile and git checkout take the I2 walk.
- G2: the MD2 manifest proof joins the K8/IR9 allow-list with the IR2 lifetime. Its only producer is the landing; its only consumers are package_plan/integrate. It is published via the guarded path, with an arch guard plus a MutationCase.
- G3: ONE generation rule for ALL proof kinds. A shared generation is bumped by every forget (forget_proofs / release_held / forget_invocation_memos) under the lock with the clear-all, and is captured before the first observation the proof rests on.
- G4: MS4 tree-entry mode applies to apm_modules. Deployed targets keep today's deploy-mode rule: fchmod BEFORE the IR6 seal, and the seal checks mode. Compare modes on L/M; on W compare ACL inheritance per #5388.
- #5418: exactly 3 lockfile fields are known-variant; everything else is asserted byte-equal; the #5418 fix removes the exemption.
[from Bets design-sec3-security.md: K1-K9, IR1-IR9, MS1-MS7, MD1-MD5, with test cells and cost lines]

## 6. Linux bar -- REGENT RULING (approved, via PERF VP 03:25 CET)
"L <= 3.5 s stays the North Star; v0.40 ships the Python manifest architecture at a measured ~4.3 s L milestone."
- Measured basis: the stacked ceiling, L 0.701, ~4.3 s on 9V74 [45530064444].
- The native-core gate is MET: walk+stat+hash+copy = 45% of L CPU (65% with realpath; hash 0.2%) [45520769932 / 45530101839].
- The native filesystem-op core is a RESEARCH lane (Lane N, section 7). It is not part of the v0.40 commitment.
- Alternative considered, not chosen: revise the L bar to ~4.3 s with no native lane.
W: no W number is claimed until the correctly stacked W re-run on main+#5374 (cc5aac78) reads.

## 7. Build plan (phased cars, default-on, kill bars)
[detail from Bets design-sec4-build-plan.md, bets-notes 9a70a49e]

OWNERS (PERF VP ruling 03:12 CET; work starts after the doc lands WED 12:00):
| car | scope | expected | kill bar | owner | W3d gate |
|---|---|---|---|---|---|
| 1 | plan-once + classify-once (A) | W ~0.97 / L ~0.95 | <= 0.98 or kill | E2 Warm Train (ab60b9b2) | ready |
| 2 | M2 materialize-once | ~0.85-0.88 | <= 0.90 on 2 OSes or kill | E1 Windows Paths (3c0e465f) | MS1-MS7; ruled parts may start |
| 3 | C containment by construction | W ~0.85 | W <= 0.90, L/M neutral, or kill | Bets (e7b91082); E1 reviews W | G1-G4 RULED (ready) |
| 4 | B bulk skills | -- | DROPPED (measured ~0; IR6 makes the faithful car cost more than its ceiling); folded into 5 | -- | -- |
| 5 | manifest-driven deploy | stacked ceiling L 0.701 / M 0.663 / W pending | per section 6 | research lead (architect) + L5 (6ffa7618, builder, after #5418) | G1-G4 RULED (ready) |
Every car: default-ON, stacked bench on CURRENT main, W3d row per car, differential (bytes + modes; #5418 fields known-variant).

LANE N (research; regent ruling via PERF VP 03:25 CET):
| lane | scope | deliverable | end date | owner | ship |
|---|---|---|---|---|---|
| N | native filesystem-op core: ONE hot loop (walk/stat/realpath/copy) behind the same W3d invariants (IR/MS/MD) | measured upper bound, L/W/M cold t50, vs the stacked Python ceiling | FRI 27 Nov 03:30 CET (48 h); no bound by then = killed | SoL (2ec55fc5) under the research lead; starts after SoL's doc sections are in | CEO gate: only if >= 0.5 s on L, default-on, across all 3 OS. Then a decision memo: cost, QL/security supply-chain review, wheel matrix |

## 8. What did not work (kills, with numbers)
- lazy imports (L 0.988 / W 0.995);
- frozen/zipapp boot (no win);
- single-pass integrate (memo engaged, L 0.993);
- records batching (knock-on; ~3% W);
- #5414 locked-warm routing (2.2x worse on main);
- stat-cut beyond form B (~1-2% W);
- frozen + MCP decline (trace artifact);
- warm cache-hit remainder (~0.5 s W);
- "lock-phase 21-23%" (confound).
- **Lessons:**
  - re-measure ceilings on current main;
  - profiles rank, paired deletion sizes;
  - every headline claim needs one real-network confirmation (S88);
  - Windows has its own resolve_path (realpath.py redefines it under sys.platform == "win32"), so any lexical/stub ceiling on realpath must override AFTER the platform block, or it silently does nothing on W. The first stacked W read (0.719) was invalid for this reason. The 2026-11-24 containment ceiling 365324c6 W 0.748 may also be partial on W.

## 9. Evidence index
[run IDs per row]

## Side finding
- #5418: the lockfile embeds absolute paths for monorepo sibling packages. Pre-existing; v0.40 with a rekey.
