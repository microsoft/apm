# Can B1 (in-process smart-HTTP v2 pinned fetch) engage on GHE / GitLab hosts? (research lead, 17:2x CET; PAPER, CI-only inputs)

## Gap (JH fixture #5368, run 45430227156, Linux tree-10 cold, CPU incl children, per VP 17:16)
github.com 2.40 s (14 spawns, 0 clones, B1 engaged) | GHE 5.16 s (85 spawns, 20 clones) | GitLab 3.62 s (84 spawns). Enterprise pays 1.5-2.1x CPU, 6x spawns.

## Why B1 is github.com-only today (main, src/apm_cli/cache/inprocess_fetch.py)
- _gate(): `if url != f"https://github.com{path}": raise _Refuse(...)` and the upload URL is hard-coded to github.com; anonymous only (credential-bearing attempts go to git unless A' on github.com).
- W3d F10 (row 6): "SHA-1 without collision detection is acceptable only because B1 is github.com-only: GitHub rejects sha1dc-flagged objects at push, and TLS rules out MITM. Any widening needs a separate review (sha1dc, or SHA-256)."

## Paper ceiling (same shape as github.com)
- Linux t10: GHE 5.16 -> ~2.4-2.6 s (0.47-0.50, ~2x); GitLab 3.62 -> ~2.4-2.6 s (0.66-0.72). Scales with packages (t50 bigger in seconds, W much bigger: W git children ~18 s at t50 auth today [45427206298]).
- Cost of the faithful sha1dc form below: +1 spawn/pkg (L ~1 ms, W ~21 ms) -> W t50 +~1 s, still a large net win.

## Missing invariants (to be cleared by W3d) and the fastest form for each
1. SHA-1 collision detection (F10). GHES runs GitHub's server stack (rejects sha1dc objects at push) but GitLab/self-hosted cannot be assumed. Fastest faithful form: pipe the fetched pack through ONE `git index-pack --stdin --strict` per package (git's sha1dc + fsck), then B1's in-process tree walk/write; or engage only on SHA-256 object-format repos. No new dependency (no vendored sha1dc).
2. Host allow-list: engage only for hosts AuthResolver classifies (github.com, *.ghe.com, configured GHES, GitLab if classified) AND that advertise protocol v2 with `fetch` + `shallow`; anything else -> git.
3. Auth: enterprise repos are mostly private -> needs the A' form per host (W3d A1-A10 today = github.com only): token only from AuthResolver for that exact host, header bytes == what APM's git path sends (GHES basic; GitLab form to be confirmed), per-request, exact origin, max_hops=0, never logged; any challenge -> git.
4. TLS: private CAs are common on GHES -> honor http.<url>.sslCAInfo/sslCAPath/GIT_SSL_CAINFO exactly as git per host (W3 mapping), client certs (http.sslCert/sslKey) -> git.
5. want-by-SHA: servers may refuse `want <sha>` for unadvertised commits (uploadpack.allowReachableSHA1InWant/allowAnySHA1InWant); a refusal ('not our ref') -> git for that package (no retry storm; breaker per host).
6. Rate limit / breaker: per host (GitLab 429 + Retry-After semantics), same R1-R6 shape.
7. #4970 commit admission (F1) and the F12 differential (identical lockfile + receipts) per host; realistic-config cells (GHES custom CA, proxy, GitLab token form).

## Verdict (paper)
Engaging B1 on GHE is the biggest enterprise lever (~2x at t10 L); GitLab ~1.4x. Blocking invariant is #1 (sha1dc) -- cheapest faithful form costs 1 spawn/pkg. Needs a W3d ruling before any prototype; then a CI ceiling on JH's #5368 fixture (tree-10/50 ghe + gitlab, L + W).

## W3d ruling 17:4x CET (row w3d:ruling:b1-enterprise): OK-with-conditions EH1-EH10; *.ghe.com joins github.com (no sha1dc spawn); GHES/GitLab: one git index-pack --stdin per package (not --strict); EH3 auth x-access-token/oauth2 only; EH4 TLS sslCAInfo/sslCAPath only (schannel -> git). Builder F&C after #5353.
