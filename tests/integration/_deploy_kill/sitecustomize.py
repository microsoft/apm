"""Simulate an install killed between deploy and the refusal's restore (#4863).

``tests/integration/test_abandoned_deploy_journal_replay_lifecycle.py`` puts this
directory on ``PYTHONPATH`` for one engine run (``python -m apm_cli.cli``)
and sets ``APM_TEST_DEPLOY_FAULT``:

* ``kill``: the deploy journal's in-process restore exits the process at once
  (``os._exit``), as SIGKILL or power loss would, so the journal is abandoned.
* ``hash+kill``: also make the post-deploy ``require_hashes`` check refuse,
  then exit at the restore. Install always records a hash for a git row and
  exempts local rows, so that refusal cannot be produced from inputs alone.
"""

import os
import sys

_FAULT = os.environ.get("APM_TEST_DEPLOY_FAULT", "")
_KILLED_EXIT = 137

if _FAULT:
    from apm_cli.install import deploy_snapshot, integrity

    def _killed(self):
        os._exit(_KILLED_EXIT)

    deploy_snapshot.DeployJournal.restore = _killed

    if "hash" in _FAULT.split("+"):
        _judge_hashes = integrity.enforce_installed_hash_policy

        def _refuse_hashes(ctx, lockfile):
            # Refuse at the post-deploy gate only; check_before_deploy passes.
            if sys._getframe(1).f_code.co_name != "check_before_lockfile":
                return _judge_hashes(ctx, lockfile)
            from apm_cli.install.phases.policy_gate import PolicyViolationError

            raise PolicyViolationError(
                "security.integrity.require_hashes is enabled but these locked "
                "dependencies have no content hash (fail-closed): pkg."
            )

        integrity.enforce_installed_hash_policy = _refuse_hashes
