"""Simulate an install killed between deploy and the journal settling (#4863).

``tests/integration/test_abandoned_deploy_journal_replay_lifecycle.py`` puts this
directory on ``PYTHONPATH`` for one engine run (``python -m apm_cli.cli``)
and sets ``APM_TEST_DEPLOY_FAULT``:

* ``kill``: the deploy journal's in-process restore exits the process at once
  (``os._exit``), as SIGKILL or power loss would, so the journal is abandoned.
* ``hash+kill``: also make the post-deploy ``require_hashes`` check refuse,
  then exit at the restore. Install always records a hash for a git row and
  exempts local rows, so that refusal cannot be produced from inputs alone.
* ``settle+kill``: exit when the journal is discarded, right after the
  lockfile reached disk, so the journal records a committed install.
* ``deploy+kill``: exit mid-deploy, before the next journal record once
  ``APM_TEST_DEPLOY_KILL_AFTER`` paths were journaled, written and sealed, or
  before journaling the first path ending in ``APM_TEST_DEPLOY_KILL_BEFORE``.
* ``path+kill``: exit once the write to ``APM_TEST_DEPLOY_KILL_PATH`` (POSIX,
  relative) finished, before its post-write seal.
* ``unsealed+kill``: exit once the ``APM_TEST_DEPLOY_KILL_AFTER``-th journaled
  path is written, before its post-write seal.
* ``retire+kill``: exit right after the first journaled delete is sealed
  (a retire killed partway, #5130).

Python runs only the first ``sitecustomize`` on its path, so this one runs
the next one (the isolated environment's network guard) before anything else.
"""

import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _entry in sys.path:
    _candidate = os.path.join(_entry or os.curdir, "sitecustomize.py")
    if os.path.abspath(_entry or os.curdir) != _HERE and os.path.isfile(_candidate):
        runpy.run_path(_candidate, run_name="sitecustomize")
        break

_FAULT = os.environ.get("APM_TEST_DEPLOY_FAULT", "")
_KILLED_EXIT = 137

if _FAULT:
    from apm_cli.install import deploy_snapshot, integrity

    def _killed(*_args, **_kwargs):
        os._exit(_KILLED_EXIT)

    faults = _FAULT.split("+")
    if "settle" in faults:
        deploy_snapshot.DeployJournal.discard = _killed
    elif "deploy" in faults:
        _after = int(os.environ.get("APM_TEST_DEPLOY_KILL_AFTER", "0") or 0)
        _before = os.environ.get("APM_TEST_DEPLOY_KILL_BEFORE", "")
        _record = deploy_snapshot.DeployJournal.record

        def _record_or_kill(self, path, **kwargs):
            # Every recorded path before this call has also been written.
            reached = str(path).replace(os.sep, "/").endswith(_before) if _before else False
            if reached or (_after and len(self.units) >= _after):
                os._exit(_KILLED_EXIT)
            return _record(self, path, **kwargs)

        deploy_snapshot.DeployJournal.record = _record_or_kill
    elif "path" in faults:
        _target = os.environ["APM_TEST_DEPLOY_KILL_PATH"]
        _seal = deploy_snapshot.DeployJournal.after_write

        def _seal_path_or_kill(self, path):
            if path.as_posix().endswith(f"/{_target}"):
                os._exit(_KILLED_EXIT)
            return _seal(self, path)

        deploy_snapshot.DeployJournal.after_write = _seal_path_or_kill
    elif "retire" in faults:
        _sealed = deploy_snapshot.DeployJournal.after_write

        def _seal_then_kill(self, path):
            _sealed(self, path)
            # A persisted journal just sealed a path that is gone: a delete finished.
            if self.root is not None and not os.path.lexists(path):
                os._exit(_KILLED_EXIT)

        deploy_snapshot.DeployJournal.after_write = _seal_then_kill
    elif "unsealed" in faults:
        _after = int(os.environ["APM_TEST_DEPLOY_KILL_AFTER"])
        _seal = deploy_snapshot.DeployJournal.after_write

        def _seal_or_kill(self, path):
            if len(self.units) >= _after:
                os._exit(_KILLED_EXIT)
            return _seal(self, path)

        deploy_snapshot.DeployJournal.after_write = _seal_or_kill
    else:
        deploy_snapshot.DeployJournal.restore = _killed

    if "hash" in faults:
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
