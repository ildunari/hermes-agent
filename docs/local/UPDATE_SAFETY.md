# Hermes Slim Update Safety

`local/studio-slim` is the deployed Hermes source branch. Upstream updates and
rollbacks are forward-only. Never reset, rebase, or force-push its published
history.

## Before customization work

Run:

```bash
python scripts/carry.py validate
python scripts/carry.py doctor
```

Every managed core path must have one primary carry owner or an unexpired,
reasoned exemption. A feature is connected only when its implementation,
runtime consumer, isolated behavior test, and deployed probe are registered.

The tracked Git hooks provide early feedback. The deployment gate is the actual
guarantee boundary: bypassed local Git state cannot deploy until it is repaired
and fully verified.

## Checkpoints

The 30-minute checkpoint job calls:

```bash
python scripts/hermes_checkpoint.py snapshot
```

Snapshots use `refs/hermes/checkpoints/*` and never move the current branch or
index. `PARTIAL` and `FAILED` are non-success outcomes. Restore into a new
worktree:

```bash
python scripts/hermes_checkpoint.py restore <ref> --to-worktree <new-path>
```

Never reset the live checkout to a checkpoint.

## Updates

The update service is a user LaunchAgent outside Hermes and coding-session
hosts. Interactive agents may only submit `start`, `status`, or `abort` over
its private socket:

```bash
python scripts/hermes_update_service.py request start --mode rehearse
python scripts/hermes_update_service.py request status --run-id <run-id>
python scripts/hermes_update_service.py request abort --run-id <run-id>
```

All merge, test, build, and conflict-worker processes belong to the service.
Do not run direct or background pytest lanes during an update. Tests must use
`scripts/run_tests.sh`; nested test parallelism is forbidden.

Each deployment boundary has durable `PREPARED` and `COMPLETED` receipts.
MacBook prerequisites stage before Studio activation. The live Studio checkout
advances only by `--ff-only`. One signed build and one detached restart are
allowed per controlled deployment attempt.

The Studio update service injects `APPLE_NOTARY_PROFILE=my-notary-profile`
into Desktop builds. Setup therefore requires that profile to validate locally;
the build fails instead of silently shipping an unstapled replacement.

## Recovery

An interrupted run is reconciled from its pinned run bundle and receipts.
`ABORTED` runs never resume. Their worktree, run ref, receipts, and redacted
evidence remain available until explicit cleanup.

Rollback is a new forward commit restoring the last verified tree. It passes
through the same carry, test, build, signing, parity, restart, and runtime
verification pipeline. Never deploy an older artifact while the source branch
points at newer code.
