"""Merge-queue v2 runner (shadow mode): compose, build, publish and prove trains.

Step 3 of the merge-queue v2 plan (#4671). The runner composes trains from
ticketed PRs, builds each train server-side through the merges API, publishes
``queue/t<id>/p<k>-(guard|full)`` refs that ``merge-train.yml`` proves, and logs
"would land #n at T_k". In shadow mode it never writes to main.
"""
