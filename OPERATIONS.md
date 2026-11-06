# RELAY AI Operations Runbook

This runbook covers the current single-host SQLite deployment. It does not claim high availability or a production multi-host recovery target.

Example localhost-only systemd service and backup timer files are in [`deploy/systemd`](./deploy/systemd/README.md). They require host-specific paths, secret configuration, TLS proxying, and a deliberate Docker socket policy before use.

## Database schema upgrades

The API checks the recorded schema version during startup and applies its idempotent SQLite schema setup in one `BEGIN IMMEDIATE` transaction. Concurrent process startup is serialized. Version 10 stores a SHA-256 digest with each source snapshot, backfills digests for existing rows, and checks the digest whenever a worker or API reads a snapshot. A mismatch fails closed. If the database records a version newer than the application supports, startup stops with an explicit error before changing the database; deploy a compatible application version before reopening it. Keep a verified backup before application upgrades. Per-release migration scripts and downgrade support are not implemented yet.

## Configured local Git targets

`RELAY_REPOSITORIES_JSON` is an optional server-side JSON map from stable IDs to absolute local Git repository roots. Do not accept paths from clients or expose this setting. Run RELAY AI as a dedicated account with write access only to the approved target roots. At submission, the service requires a clean target and records its HEAD. Apply checks that revision again and verifies changed source files against it; commit is a separate action and stages only those changed paths. RELAY serializes target operations with per-repository locks, but external Git clients do not honor those locks; do not edit a configured checkout while an apply or commit is running. The sandbox runs tests against the submitted snapshot, so operators must supply the snapshot for the selected repository base. Back up configured repositories separately from the task database and task-workspace archive.

Review [THREAT_MODEL.md](./THREAT_MODEL.md) before exposing the service beyond localhost. It documents residual risks, deployment requirements, provisional service objectives, and ownership that the deployment operator must assign. Use the [deployment record template](./DEPLOYMENT_RECORD_TEMPLATE.md) to capture host-specific contacts, security decisions, data handling, alert delivery, and restore-drill signoff without putting secrets in the record.

## Health and readiness

- `GET /health` reports runtime versions, sandbox capability, queue counts, active task count, schema version, and worker liveness.
- `GET /ready` returns `200` only when the database schema, worker, provider configuration, and sandbox configuration are available. A missing Gemini key or sandbox image is expected to make the service unready.
- `GET /metrics` exposes Prometheus-compatible queue, task, worker, queue-wait, retained Gemini token/cost gauges, stage duration/failure, job-attempt, and sandbox mode/outcome metrics. Gemini usage values decrease when owners delete tasks because usage is retained with task evidence. Protect the endpoint with the same API authentication as other routes.

## Backup

Task snapshots and evidence live in SQLite. Applied and committed task copies live under `RELAY_APPLIED_ROOT`. Back up both together:

```sh
cd relay-ai
.venv/bin/python backup.py /secure/backups/relay-ai
```

The command uses SQLite's online backup API, so it can capture a consistent database while the service is running. It writes a timestamped `tar.gz` archive with mode `0600`; the destination must be outside `RELAY_APPLIED_ROOT`. Archives contain uploaded source code and task workspaces. Store them in an encrypted, access-controlled location and apply your organization’s retention policy.

Backup creation validates the temporary archive before publishing its final filename. To verify an existing archive before restore:

```sh
.venv/bin/python backup.py --verify /secure/backups/relay-ai/relay-ai-backup-YYYYMMDDTHHMMSSZ.tar.gz
```

The verifier rejects unsafe archive paths and special entries, checks the manifest and archive limits, and runs SQLite integrity and foreign-key checks without extracting task workspaces.

For monitoring, run `.venv/bin/python backup.py --check-latest /secure/backups/relay-ai --max-age-hours 26`. It emits JSON and returns a nonzero exit code if no archive exists or the newest archive is stale. The systemd templates include a daily freshness check and an `OnFailure` webhook notifier. Configure `RELAY_BACKUP_ALERT_WEBHOOK_URL` in the protected `/etc/relay-ai/alerts.env` file, then trigger a controlled stale-backup drill and confirm receipt at the assigned alert destination.

No automatic task expiry is enabled by default. Owners can permanently delete stopped tasks from the dashboard or `DELETE /tasks/{task_id}`. This removes the task source, events, action audit, queue record, usage rows, and task workspace. Active work must be cancelled and stopped first.

For operator-managed retention, run the dry-run-first purge tool from `relay-ai/`:

```sh
.venv/bin/python retention.py --older-than-days 90
.venv/bin/python retention.py --older-than-days 90 --apply --confirm-delete
```

The candidate list includes only tasks last updated before the cutoff with `FAILED`, `CANCELLED`, `REJECTED`, or `COMMITTED` status and an inactive terminal queue record. Awaiting approval, approved, applied-but-uncommitted tasks, and every task bound to a configured repository are retained. Review the dry-run output before scheduling the explicit apply command. The tool rechecks eligibility in a SQLite write transaction before cascading deletion and stages the task workspace by atomic rename; concurrent retries make a task ineligible and cause the workspace move to be rolled back. Automatic scheduling remains the operator's responsibility.

## Restore

1. Restore the archive into a new isolated directory, for example: `.venv/bin/python backup.py --restore-archive /secure/backups/relay-ai/relay-ai-backup-YYYYMMDDTHHMMSSZ.tar.gz --restore-to /private/tmp/relay-ai-restore-drill`.
2. Inspect the restored database and task-workspace tree. This command never overwrites the live data directory and refuses an existing destination.
3. For an actual recovery, stop the API and worker, preserve the live `RELAY_DB_PATH` and `RELAY_APPLIED_ROOT` as a separate rollback copy, then copy the restored database/workspaces into their configured locations.
4. Restore ownership and restrictive permissions for the service account, then start the API.
5. Check `/health`, `/ready`, task listing, and a representative task’s event/action history before resuming normal use.

Keep `GEMINI_API_KEY`, bearer-token configuration, and other deployment secrets in a separate secret manager. They are intentionally not included in task backups.

## Common recovery actions

- **Queued work does not move:** inspect `/health` queue counts and worker state; inspect JSON logs for job-claim or lease-renewal errors. Restarting the API allows expired SQLite leases to be reclaimed.
- **Pipeline fails before stages run:** inspect the task error and `/ready`; configure Gemini and build the preconfigured sandbox image. Do not enable host subprocess execution for untrusted source.
- **Task cannot be deleted:** cancel it and wait until the job is no longer queued or running, then delete it again.
- **A backup is missing:** confirm the process account can read `RELAY_DB_PATH` and `RELAY_APPLIED_ROOT` and write to the output directory. Keep backup output outside the applied-workspace tree.

## Security and retention

Use HTTPS and a secret manager when the API is reachable beyond localhost. Configure unique `RELAY_AUTH_USERS_JSON` credentials for task ownership; the shared-token and local modes do not provide individual identity. Schedule backups according to the acceptable recovery point, and periodically verify a restore in an isolated environment. There is currently no automated backup scheduler, automatic retention purge, external identity provider, or multi-host queue.
