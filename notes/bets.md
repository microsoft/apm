# PERF-BETS: architectural bets for apm install COLD FRESH (t10/t50, Linux + Windows)
Owner: [PERF] Researcher, Architectural Bets (e7b91082). Lead: d7d7be0d. Base main a9c60afd5cecf3156c736d3eba3924d8d6f260f2.

## Baseline (current main-era, #5283 verdict runs at 6bd6c708, t50, paired harness, CPU incl. children)
| OS | cold CPU s | fresh-warm CPU s | cold-only stage s (share) | git spawns cold/warm | requests cold/warm | run |
|---|---|---|---|---|---|---|
| Windows | 47.95 | 26.56 | 21.4 (45%) | 666/116 | 200/50 | 45339565645 |
| macOS | 21.07 | 11.93 | 9.1 (43%) | 666/116 | 200/50 | 45339597780 |
| Linux | 20.26 | 15.67 | 4.6 (23%) | 666/116 | 200/50 | 45339544398 |
JH Linux cold profile (45354106131): git clone x100 = 2.50 s, git config x220 = 0.25 s, checkout x50 = 0.17 s child CPU; realpath 3.71 s self.

## Ranking (09:56; lead accepted 09:20 msg)
1. B1 REVIVED (new reason: Windows/macOS share 43-45%, spawn lane was Linux-only): zero-subprocess cold fetch. KILL 11:56.
2. B2 interpreter+imports: SIZE ONLY (boot CPU vs install CPU on runners). Kill if < 30%.
Dropped for cold fresh: B3 parallel/pipelined fetch (CPU-neutral, wall-only), B4 cross-project CAS (empty store on cold = 0), B5 warm daemon (subsumed by B2).

## B1: in-process git smart-HTTP v2 fetch (perf-research/arch-inproc-fetch)
Hypothesis: the cold-only stage (per package: bare clone + local clone + checkout + cat-file verify + link validation, ~11 git spawns) is deletable; one protocol-v2 POST per (url, sha) parsed in-process replaces it.
Stage DELETED: every git child on the cold fetch path (clone --bare, cat-file -t, clone --local --shared, checkout, cat-file --batch verify, ls-files link validation) plus git's own index-pack/checkout work and the separate verify re-read of the worktree.
Prototype: b73a3e04 (from a9c60afd). cache/inprocess_fetch.py: gate = F4a anonymous_ref_listing gate verbatim (plain https github.com, same git config classifier incl. credential.helper lists, http.*, url.insteadOf, includeIf via effective config, libcurl proxy model, git's CA via the truststore-free stdlib context); POST git-upload-pack (Git-Protocol: version=2; want <sha>, deepen 1, ofs-delta, done); pack checksum; every object inflated and SHA-1 hashed to its id; commit id must equal the pin; tree walked from it; worktree written with O_CREAT|O_EXCL|O_NOFOLLOW into an APM-created staged dir; minimal .git (loose objects, HEAD, shallow, pinned config, origin url) so later git use and cache trust checks work; checkout_verify manifest registered from the computed ids (require_verified_copy unchanged).
Fallback to git (unchanged path) on: non-github URL, any gate doubt, non-200 (incl. 401 private), non-v2 answer, thin/odd pack, sha256, links, submodules, .gitattributes/.gitmodules, unsafe or Windows-reserved names, sparse requests.
Runs: fast check install-cold t10,t50 PAIRS=2 L + W (branches perf-bench/b73a3e04-1795508324-90c203, -1795508371-8c98a9). Results Linux fast check (45355043376, a9c60afd -> b73a3e04, 6 pairs): t50 cold 17.67 -> 12.71 s CPU (paired 0.703, -28.1%); t10 4.22 -> 3.44 s (0.817); git spawns t50 674 -> 24 (hit 100%), requests 201 -> 101, connects 55 -> 5, errors 0; modeled wall t50 17.53 -> 13.57 s. Windows fast check 45355141217 PENDING. Verdict (15 pairs, c4071af3 -> f4236cf0, cold+warm t10/t50, L/W/M): perf-bench/f4236cf0-1795508662-b7f058 (L), -1795508677-79c7c4 (W), -1795508687-45ead3 (M). Stacks with realpath lever: yes (different stage). KILL rule now (CEO 10:06): measured win at current main or killed.
Security invariants kept: I-1 pin (commit id computed == pin), I-2/I-3 every byte hashed to an id reached from the pin (same verdict as checkout_verify, computed while writing; copy check unchanged), path containment (names validated, O_EXCL|O_NOFOLLOW in APM-created dir, no links ever created), policy/admission (admit_and_resolve and require_reachable_commit run before, unchanged), receipts/lockfile (downstream unchanged), credentials (none sent; anonymous only; 401 -> git).
Unsafe shortcuts in the prototype: none known on the integrity side; auth for private repos is NOT implemented (falls back to git, so private-repo users get today's cost). Cost of the safe authenticated version: token from AuthResolver as an Authorization header on the same pooled session (0 spawns) or `git credential fill` once per host (1 spawn per host, not per package).
KILL: 11:56.

## B2: interpreter + imports sizing
Linux (45355265691): boot-version 0.130 s, boot-help 0.143 s CPU, 264 modules; install-cold imports 934 modules. Interpreter+boot < 1% of t50 cold (17.7 s). Remaining: CPU of importing the install path (670 extra modules); expected 0.5-1.3 s (3-7%). Windows run 45355306975 PENDING.

## B1 update (host clock 09:3x)
UPPER BOUND (b73a3e04, pre-F1-F10; base a9c60afd), fast check 6 pairs:
| OS | t10 cold CPU s | t50 cold CPU s | paired t50 | spawns t50 | req t50 | connects t50 | run |
|---|---|---|---|---|---|---|---|
| Linux | 4.22 -> 3.44 (0.817) | 17.67 -> 12.71 | 0.703 (1.42x, -28%) | 674 -> 24 | 201 -> 101 | 55 -> 5 | 45355043376 |
| Windows | 10.56 -> 6.11 (0.570) | 47.49 -> 21.74 | 0.466 (2.15x, -54%) | 674 -> 24 | 201 -> 101 | 55 -> 5 | 45355141217 |
Hit rate (bench world): 100% on both (spawns 674 -> 24; remaining 24 are not fetch).
Attribution (perf-attr harness, c4071af3 -> f4236cf0, 1 pair): L t50 19.26 -> 14.39 (0.733) run 45356060346; W 55.55 -> 30.19 (0.544) run 45356136984. Requests after: 50 GET info/refs (F4a in-process listing) + 50 POST upload-pack + 1 API.
In-process path's own CPU (L t50, per-thread sampler, 45356060346): _write_file 0.069 s + _write_git_dir 0.027 s + ssl read 0.108 s self; pack parse/inflate/SHA-1 below the 0.020 s self floor; fetch_worktree inclusive < 0.60 s (listing floor). Cost line: ~0.25 s per 50 packages = ~5 ms CPU/pkg, 1 POST/pkg, 0 spawns/pkg.
CONFORMING (b0f7fde6 = F1-F10 per W3d check-ledger-w3d.md row 6) verdict runs, 15 pairs, base c4071af3, install-cold + install-warm, t10/t50: perf-bench/b0f7fde6-1795508980-71bc49 (L), -1795509002-eb069b (W), -1795509045-66f017 (M). PENDING.
F-conditions in b0f7fde6: F1 admission untouched (admit_and_resolve + require_reachable_commit run first) | F2 only pin + reached trees/blobs written to .git/objects | F3 decompress(max_length) bounded by declared size, 256 MiB object cap, 1 GiB total inflate cap, delta dst_size checked before build, body cap 64 MiB | F4 iterative resolution, depth cap 50, O(objects) | F5 120 s wall deadline + (10, 30) s timeouts | F6 one validator: checkout_verify._names_git_dir extended with HFS+ ignorables, used by both | F7 proven by construction, not fallback: git's path always runs -c core.autocrlf=false -c core.eol=lf -c core.symlinks=false (outrank system/global), so git writes raw blob bytes unless an attribute applies; in-commit .gitattributes refuses, staged clone has no info/attributes, and the gate refuses if core.attributesFile, the XDG/HOME global attributes file, or the system attributes file (git var GIT_ATTR_SYSTEM, 1 spawn per command) exists | F8 verify skip keyed by landed path AND (dev, ino); require_verified_copy unchanged | F9 url must equal https://github.com/<validated path>; CR/LF/"/[/\ in any config value refuses | F10 github.com only.
Windows F7 fallback rate: measured by the b0f7fde6 W run (spawns stay ~674 if Git for Windows ships a system gitattributes file).

## Other forms (CEO: meet the same guarantee another way)
| constraint | today in B1 | other forms (same guarantee) | pick |
|---|---|---|---|
| F7 line endings | gate refuses on any attributes source outside the commit | (a) question it: APM pins autocrlf=false/eol=lf for every git checkout, so the guarantee is "deployed bytes = blob bytes"; (b) parse the global/system attributes file in-process and refuse only if a rule can match a package path with text/eol/filter/crlf/ident/working-tree-encoding; (c) apply git's eol conversion in-process (pure byte transform) + differential cell | (a) now; (b) if the W run shows Windows falls back |
| F3/F4 bombs | per-object + total caps, depth 50 | cap by the server-declared pack object count x max package size; or stream-inflate straight to disk with a running hash | caps (cost ~0) |
| F8 verify skip | writer verdict bound to (path, dev, ino) + copy re-hash | (a) hash once: carry the blob id from the pack as the receipt/content key so integrate never re-hashes (ledger rows 2/3); (b) whole-package tree hash == pin, stat-proof cache across commands | (a) next (ledger row #2 candidate) |
| containment | O_EXCL/O_NOFOLLOW in an APM-created staged dir, names validated | dir-fd openat chain from one root fd (W3d row 1 mechanism) | shared with realpath lever |
| 2 requests/pkg | F4a GET info/refs + POST upload-pack | resolve via v2 ls-refs in the same POST session (one request per pkg); or skip listing when a tag ref resolves via the commits API | later |

## B2 sizing (interpreter + imports)
Linux: boot-version 0.130 s, boot-help 0.143 s CPU, 264 modules (45355265691); install-path import_cpu 0.377 s + startup 0.018 s of the profiled cold t50 run (45356060346) = ~2.6% of t50 cold (paired CPU ~14.4 s). Windows: boot-version 0.195 s, boot-help 0.203 s (45355306975); Windows import share pending (no per-thread profile on W in that run).
Verdict: ceiling <= 0.4 s L (2.6%); lazy imports cannot be a material win on cold fresh. PARK (recorded, not built).

## B1 Windows delta W1-W6 (host 09:5x CET)
Verdicts at b0f7fde6: Linux PASS 0.711 (45356624712), macOS PASS 0.558 (45356804641), Windows NO ENGAGEMENT 1.000, spawns 882 -> 888 (45356679140). Cause: my F7 gate refused when a system gitattributes existed (GfW ships etc/gitattributes with diff drivers only). The bench world pins http.sslBackend=openssl, so W3 is the real-user cause, not the bench one.
Fix SHA: perf-research/arch-inproc-fetch-win @ 17cd2f6c (on #5330 3e0af042). W1 WWW-Authenticate/non-200 -> git; W2 credential.* inert, forcing keys named; W3 schannel -> truststore OS-only context, openssl -> git CA file; W4 attributes allowlist parse; W5 component differential (bytes + content_hash, GfW-like world); W6 unit cells. One SSLContext + pool per trust source per process.
Runs: Windows fast check 45360893854, Linux sanity 45360919220 (base 3e0af042, t50 cold, 6 pairs). PENDING.
Known gap (other forms needed): `git var GIT_ATTR_SYSTEM` needs git >= 2.42; older git (Apple CLT 2.39) -> refuse -> 0% hit for those users. Options: (a) derive sysconfdir from `git config --system --show-origin` (ETC_GITATTRIBUTES = sysconfdir/gitattributes); (b) `git version --build-options` prefix; (c) accept fallback on old git.

## B3 (a)+(b) first ceiling (b69efdf0 vs b0f7fde6, 6 pairs)
Linux t50 cold 12.16 -> 11.43 s (paired 0.932, -6.0%), t10 3.28 -> 3.22 (0.984) [45358796767]; deep attr L 0.938 [45358987432]. Windows 1.004 [45358824098] (B1 not engaged at b0f7fde6). B3 moved to SoL 2ec55fc5 (VP 09:59); split offered.
## B1 Windows delta, folds
perf-research/arch-inproc-fetch-win @ c8477254 (one commit on #5330 f265aa9b): W1-W6 + W3d R1-R6, F12 differential via APM_NO_INPROCESS_FETCH=1 + windows_compat. W engagement verified by lead at 17cd2f6c: W t50 34.76 -> 16.23 s (0.467), spawns 678 -> 36 [45360893854]; L 0.989 [45360919220]. R-folded fast checks: perf-bench/3a3705f8-1795511166-b4eb98 (W), -1795511183-409228 (L).
R-folded 3a3705f8 vs f265aa9b: W t50 cold 47.03 -> 23.57 s (0.497, 2.0x), spawns 678 -> 128 [45362011826]; L 8.10 -> 8.18 (1.014), spawns 26 -> 127 [45362076164] = regression (+2 spawns/pkg from per-call config read + attrs probe).
VP 10:20 DECOUPLE: #5330 lands L+M at 59a386ca; Windows = follow-up PR. Fix SHA 3678b212 (on 59a386ca): classify from the env git_network_env froze (0 children), system attrs path per git binary. Checks: W 45364055975, L 45364135292.
## B1 Windows follow-up: GATE MET (30bb564c on #5330 59a386ca; W1-W6, R1-R6, probe-once, G1-G7)
Caller trace found the regression: git_network_env's own config probes 2/pkg + racing git var; #5330's +4 W = git var x4 on fallback.
W t50 cold 24.06 -> 10.81 s (0.444, 2.25x), spawns 678 -> 27, req 201 -> 101 [45369015657]; L 0.995, spawns 28 -> 25 [45369078371]; L trace 0.990 [45369125020].
