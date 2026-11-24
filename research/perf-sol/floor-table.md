# Speed-of-light model: `apm install` COLD FRESH (PERF-SOL, v1 09:40 local)

## Already known / already killed (Atlas; not re-tested)
- perf-wave1 row 21: no faithful Python-level lever >= 30% on cold; next order of magnitude = architecture (CAS + link deploy). Row 1: delete stages, don't tune.
- Row 17/18 + B7: cold SPAWN lane KILLED at cap 1.32x (run 45208473159) -- LINUX ONLY (14.37 s, 603 spawns). NEW reason to reopen on Windows only: children = 49% of Windows cold (below), rule S57 "judge syscall/spawn work on the OS where it is expensive".
- Row 17 (path layer killed, faithful -22%), plan 09:4x realpath dedup killed (0.87x L / 0.93x W). Not re-proposed as tuning; containment appears below only as a STAGE to delete/redesign.
- Row 19/20: realistic config + TLS world; 15-pair pre-registered p50 for any bar. B2: delete via verified record; exec-surprises row 97: security co-owns perf (fastest design that keeps the invariant).
- IFP per-invariant shares (py-spy 45272000369, main 703acd6f) and JH per-thread CPU (45354106131) reused, not re-measured.

## Method
All CPU = user+sys of apm + every child (wait4 / Windows Job Object), CI bench, A/A at main 3c62c90a (#5283 in). Absolute CPU differs ~20-40% BETWEEN runs (runner classes); every split below is a ratio WITHIN one run, then applied to the A/A p50. Counters (self CPU, git subcommands, file ops, hashes, proxy bytes) from my harness branch perf-research/arch-sol-counters @039abff3 (A/A 3c62c90a), runs L 45354804686, W 45354870552 (1 pair x 3 runners).

## (a)+(b) Measured today (main 3c62c90a)
| cell | CPU p50 s (A/A run) | children share (counter run) | git spawns (by sub) | requests / connects / RT | bytes down | file writes / reads | hash objects (bytes) | scandir / mkdir / rename / chmod | modules |
|---|---|---|---|---|---|---|---|---|---|
| L t10 | 3.3-4.3 (paired 1.003; 45353723802) | 14% (5.06 self of 5.91) | 146 | 41 / 14 / 69 | 117 KB | 2,281 / 4,251 | 1,926 (3.2 MB) | 1,924 / 486 / 818 / 340 | 933 |
| L t50 | 15.0 (1.012; 45353723802) | 22% (16.2 self of 20.7) | 674: config 220, clone 100, cat-file 100, ls-tree 100, remote 52, init 50, checkout 50 | 201 / 55 / 311 | 592 KB | 5,409 / 14,791 | 9,425 (16.1 MB) | 9,189 / 1,739 / 1,221 / 1,646 | 933 |
| L t200 | 57.3 (1.006; 45353723802) | 22% (61.1 self of 78.0) | 2,624 (same mix x4) | 801 / 205 / 1,211 | 2.37 MB | 21,433 / 56,432 | 37,578 (64.6 MB) | 35,956 / 6,106 / 4,877 / 6,552 | 933 |
| W t10 | 10.2 (0.995; 45353749594) | 41% (5.47 self of 9.25) | 146 | 41 / 14 / 69 | 117 KB | 2,153 / 4,038 | 1,926 (3.2 MB) | 1,924 / 490 / 818 / 178 | 937 |
| W t50 | 48.5 (1.000; 45353749594) | 49% (17.8 self of 35.0) | 674 (same mix) | 201 / 55 / 311 | 593 KB | 4,788 / 13,724 | 9,425 (16.1 MB) | 9,192 / 1,746 / 1,221 / 871 | 937 |
| W t200 | 131.5 (1.002; 45353796519) | pending (est ~45%) | 2,622 | 801 / 205 / 1,211 | ~2.4 MB | pending | pending | pending | 937 |
Requests per package = 4 (2 git conversations: ls-remote info/refs+upload-pack, clone info/refs+upload-pack) + 1 releases API per run (routes, shard JSON of 45353749594). Cold minus fresh-warm (same counter run): L +4.6 s = children +4.1 s, self +0.5 s; W +16.3 s = children +14.8 s, self +1.5 s (550 extra git spawns). Linear in packages (self per package ~0.31-0.33 s L at t50 and t200: no superlinearity).

## Unit costs (source)
| unit | Linux | Windows | source |
|---|---|---|---|
| interpreter + `--version` (264 modules) | 0.115 s | 0.141-0.156 s | boot A/A 45354126834 / 45354202238 |
| full install import set (933 modules) | ~0.77 s | ~1.0 s (est) | IFP 45272000369 imports 4.2% of cold t50 |
| trivial git spawn (config), child CPU | 1.1 ms | 28.8 ms (n=1584) | JH 45354106131 / F&C 45325568657 |
| git clone (local mirror, tiny repo), child CPU | 25 ms | 39.5 ms | same |
| git checkout / ls-tree / cat-file | 3.4 / - / - ms | 30.8 / 31.6 / 15.9 ms | same |
| avg git child per spawn, cold t50 | 6.7 ms (4.5 s / 674) | 25.5 ms (17.2 s / 674) | my counter runs |
| parent Python per extra cold spawn | ~0.9 ms | ~2.7 ms | cold-warm self delta / 550 |
| Python self CPU per package today (fresh) | ~320 ms | ~360 ms | counter runs |
| SHA over content (OpenSSL) | ~1 ms/MB | ~1-2 ms/MB | derived, not benched (16 MB hashed = <30 ms: bytes are NOT the cost; per-object Python is) |
| file create+write+close, in-process | ~0.05-0.1 ms (EST) | ~0.3-0.5 ms (EST) | PENDING runner microbench (next finding) |
| TLS handshake / pooled request, in-process | ~3 ms / ~0.3 ms (EST) | ~4 / ~0.4 ms (EST) | PENDING runner microbench |

## (c) Floor from first principles (per package p; t10/t50/t200 = 10/50/200)
- Bytes: the commit's pack only = today's bytes (11.8 KB/pkg incl. advertisements); bytes are already at floor (0.59 MB at t50). Not a lever.
- Requests: 1 git conversation per unique (url,ref): GET info/refs (resolves the ref) + POST upload-pack (fetch) = 2/pkg (100 at t50) vs 4/pkg today; 1 pooled TLS connection per host (HTTP/1.1 keep-alive) vs 55 connects. Wall floor at rtt 30: (2 connect + 2 per DAG level x 6 levels) x 30 ms = ~0.4 s vs 17 s wall today.
- Spawns: 1 interpreter, 0 git children with an in-process smart-HTTP + pack verify (SHA-1 object ids ARE the integrity check); fallback floor with git CLI = 1 fetch per package.
- Hashes: each fetched object once (pack index) + each deployed file once = ~27/pkg (1,370 at t50) vs 188/pkg today (9,425; each byte hashed ~16x).
- Files written: apm_modules (~14/pkg) + deployed (~13.4/pkg; tree-10 deploys 134) + lockfile + receipts = ~28/pkg (~1,400 at t50) vs 108/pkg today (5,409). Reads: each fetched file once (~14/pkg) vs 296/pkg today.
- CPU floor = interpreter+needed imports + requests + inflate/hash + per-file materialize + graph/lockfile:
| cell | SoL CPU (0 git) | floor with git CLI (1 fetch/pkg) | today | gap today / SoL |
|---|---|---|---|---|
| L t10 | ~0.35 s | ~0.6 s | 3.3-4.3 s | ~10-12x |
| L t50 | ~0.7 s | ~1.9 s | 15.0 s | ~21x |
| L t200 | ~2.2 s | ~7 s | 57.3 s | ~26x |
| W t10 | ~0.6 s | ~1.0 s | 10.2 s | ~17x |
| W t50 | ~1.5 s | ~3.5 s | 48.5 s | ~32x |
| W t200 | ~5 s | ~13 s | 131.5 s | ~26x |
(SoL t50 L = 0.3 interp/imports + 0.1 net + 0.02 inflate/hash + 1,400 x 0.1 ms files + 0.1 graph/lockfile/checks; W uses 0.45 interp, 0.5 ms/file. File and TLS unit costs are ESTIMATES until the runner microbench lands.)

## (d) Gap = waste, top 3 buckets (seconds at A/A p50; shares within-run)
| cell | #1 | #2 | #3 |
|---|---|---|---|
| L t50 (15.0) | per-file Python in integrate/deploy (containment 17%, receipts/ledger/snapshot 16%, writes 9%, copy 5%, hash 4% per IFP) ~7.5 s | git children 22% = 3.3 s (674 spawns vs 0-50) | interpreter/imports/object churn ~2.0 s (13.7%); lockfile build ~1.2 s next |
| W t50 (48.5) | git children 49% = 23.8 s (674 x ~25-30 ms; floor 0-2 s) | per-file Python integrate/deploy ~12 s (est 50% of self 24.7 s; Windows shares pending perf-attr run 45356048720) | resolver+lockfile+imports ~6 s (est) |
| L t200 (57.3) | per-file Python ~29 s | git children 12.6 s | interpreter/object churn ~8 s |
| W t200 (131.5) | git children ~60 s (est 45%) | per-file Python ~36 s | rest ~30 s |
Policy/audit ~0 (IFP). Network-wait CPU ~0 (bytes 0.6 MB). Hashing BYTES ~0; hashing is per-object overhead inside the per-file bucket.

## Headline
Cold fresh is 21-32x above its speed of light. Two stages carry it: (1) the git CLI fetch chain (13.5 spawns/pkg; 49% of Windows cold, 22% of Linux) whose floor is 0-1 spawn/pkg; (2) per-file Python work in integrate (~190 hash objects, ~300 reads, ~108 writes, ~184 scandirs per package vs ~28 writes / ~27 hashes floor). A Windows-only fetch redesign (one in-process or one-spawn fetch per package) has a ceiling of ~1.8-1.9x on Windows cold t50 (48.5 -> ~26 s) BEFORE touching integrate; that is a new, OS-scoped reason vs the Linux-only kill at 1.32x.

## Pending (ETAs)
- Windows/Linux per-check CPU split at c4071af3 (perf-attr PROFILE=1): runs L 45355996747, W 45356048720 -> files/check-measured.md, due 10:50 (lead clock).
- Runner microbench: file create/write/close and TLS/request unit costs on L+W (next hour finding).
- Windows git subcommand mix: my counter parsed "C:\Program Files\Git" badly (624 under "Files\Git..."); same mix as Linux by spawn total; fix in the next counter rev.
