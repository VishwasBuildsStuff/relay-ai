# RELAY AI API and worker

FastAPI service for submitting code snapshots, running the remediation pipeline, and reviewing task evidence. For the product overview and local full-stack setup, see the repository [README](../README.md), [blueprint](../BLUEPRINT.md), and [build tracker](../TRACKER.md).

## Local API setup

Use Python 3.11 or newer. From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
docker build -t relay-ai-sandbox:python3.11 sandbox
export GEMINI_API_KEY="your-key"
uvicorn main:app --host 127.0.0.1 --port 8000
```

Running `python main.py` binds to `127.0.0.1` by default. To bind to a different interface, set `RELAY_HOST` to an IPv4 or IPv6 address literal and configure `RELAY_API_TOKEN` or `RELAY_AUTH_USERS_JSON`; the direct entry point refuses unauthenticated non-loopback binds.

Set `RELAY_ENV=production` for deployment. Production mode requires shared-token or per-user authentication, an explicitly configured non-wildcard `RELAY_CORS_ORIGINS` allow-list, and keeps unsafe host-subprocess execution disabled. The systemd template enables this mode; configure the required values in its protected environment file.

The service stores task results and source snapshots in `relay_tasks.sqlite3` by default. Set `RELAY_DB_PATH` to change the location. `RELAY_APPLIED_ROOT` sets where approved task copies and commits are stored. Optionally configure `RELAY_REPOSITORIES_JSON` as a JSON mapping of stable target IDs to absolute local Git repository roots, for example `{"billing":"/srv/repos/billing"}`. The dashboard can bind a new task to one of those targets without exposing its server path. Only clean repositories are offered; the target HEAD is recorded at submission, and apply checks that HEAD is unchanged and that each changed source file matches the submitted snapshot. Apply and commit remain separate human actions, and commit stages only the approved changed files. The sandbox tests still run against the submitted snapshot, so keep that snapshot aligned with the selected target's base. RELAY operations on a configured target are serialized on the service host; do not edit the same checkout concurrently from other Git clients while apply/commit is running. Task deletion and retention skip applied/bound target tasks so their audit trail remains available. These are local filesystem targets only; remote Git hosts and per-target user ACLs are not implemented. `RELAY_CORS_ORIGINS` accepts a comma-separated origin list. Set `RELAY_API_TOKEN` to require a shared bearer token; enter it in the dashboard header. For per-user ownership, configure `RELAY_AUTH_USERS_JSON` as a JSON object mapping usernames to unique bearer tokens, such as `{"alice":"long-random-token-a","bob":"long-random-token-b"}`. When set, this map supersedes `RELAY_API_TOKEN`; each user can list and access only tasks they created. Optionally set `RELAY_AUTH_ROLES_JSON` with `viewer` or `operator` values for those usernames. If that setting is present, any configured user omitted from it is treated as a viewer. Without a role map, per-user tokens retain operator permissions for compatibility. Store credentials in a secret manager or protected environment, not in source control.

`GET /api/v1/session` reports the current actor and role for the dashboard. Read-only viewers may use `GET` routes and cannot call write methods; operators retain task-owner scoping for all task mutations.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Service status and docs link |
| `GET` | `/health` | Runtime and sandbox capability summary |
| `GET` | `/ready` | Whether SQLite schema, worker, model, and sandbox dependencies are ready |
| `GET` | `/metrics` | Prometheus-compatible queue, task, worker, and Gemini token metrics |
| `GET` | `/api/v1/tasks?limit=50&offset=0&q=...&status=FAILED` | List persisted tasks with optional substring search, status filter, and `X-Total-Count` response header |
| `DELETE` | `/api/v1/tasks/{task_id}` | Delete an owned, stopped task, including stored source, evidence, actions, queue record, and task workspace |
| `POST` | `/api/v1/tasks` | Queue a JSON report and codebase snapshot; returns `202` |
| `POST` | `/api/v1/tasks/upload` | Queue a ZIP upload; returns `202` |
| `GET` | `/api/v1/tasks/{task_id}` | Retrieve status, results, and timeline |
| `GET` | `/api/v1/tasks/{task_id}/events` | Read newest-first pages of the append-only event history with `X-Total-Count` |
| `GET` | `/api/v1/tasks/{task_id}/actions` | Read newest-first pages of the append-only operator action audit with `X-Total-Count` |
| `POST` | `/api/v1/tasks/{task_id}/actions` | Approve, reject, apply/commit, cancel, or retry |
| `GET` | `/api/v1/tasks/{task_id}/patch` | Download the proposed unified diff |
| `GET` | `/api/v1/scenarios` | Load demonstration scenarios |

The earlier unversioned task and scenario paths remain available as compatibility aliases; new clients should use `/api/v1`.

Submissions are atomically persisted with a SQLite job record and processed by a lifespan-managed worker. Jobs use leases and bounded attempts; a second process can safely claim work through SQLite transactions, though SQLite remains intended for a local/small single-host deployment. Expired leases are reclaimed, progress and events survive restarts, and cancellation takes effect at stage boundaries. Failed jobs can be manually retried up to their configured attempt limit.

Task status follows the work lifecycle: new submissions are `QUEUED`, claimed work moves through `RUNNING`, `TRIAGING`, `INVESTIGATING`, `SANDBOX_RUNNING`, and `REVIEWING`, then reaches the corresponding completed or decision state. `current_stage` gives the finer-grained operation name; terminal actions remain `CANCELLED`, `FAILED`, `REJECTED`, `COMMITTED`, or `APPLIED` as applicable.

Application logs are emitted as JSON lines with UTC timestamps and optional task, stage, and attempt context for local inspection or collection by a log service. A best-effort redactor removes configured secret values and common credential formats from logs, task errors, and sandbox output; review diagnostics before sharing because redaction cannot identify every secret format. Successful operator actions have a separate append-only audit with action, outcome, notes, details, timestamp, and a non-secret actor label (username, `local-operator`, or `shared-token`). The shared token does not identify an individual.

Pipeline completion events include elapsed stage time. Gemini response usage metadata is stored with task evidence and exposed as gauges for retained prompt/candidate/total token usage in `/metrics`; deleting a task also removes its usage rows, so these values can decrease. Optional estimated cost attribution uses operator-configured input/output USD rates per million tokens; set both `RELAY_GEMINI_INPUT_USD_PER_MILLION` and `RELAY_GEMINI_OUTPUT_USD_PER_MILLION`, plus `RELAY_GEMINI_PRICING_LABEL` identifying the source/model revision. The rates are stored with each usage record, and `/metrics` separates retained priced from unpriced calls. Estimates are only as accurate and current as the operator-supplied rates and are not provider billing records. With no rates configured, usage remains token-only.

The action endpoint accepts an optional `Idempotency-Key` header. Repeating a successful request with the same key, actor, action, and body replays the original response; reusing the key with different request contents returns `409`.

Task data has no automatic expiry by default. An owner can permanently remove a stopped task from the dashboard or API; deletion rechecks queue inactivity in the same SQLite write transaction that removes task, snapshot, evidence, action, queue, and usage rows, and restores the task-owned Git workspace if a worker claimed it during the operation. Tasks that have applied a patch to a configured repository cannot be owner-deleted, and repository-bound tasks are excluded from retention purges so their audit trail stays available. Operators can use `retention.py --older-than-days N` for a dry run and add `--apply --confirm-delete` to purge old terminal tasks; awaiting approval and applied-but-uncommitted task copies are excluded. Active jobs must be cancelled and stopped before owner deletion.

The local API applies fixed-window per-client limits of 10 task submissions and 60 actions per minute. The limiter stores only a BLAKE2 digest of the client address and cleans up expired buckets during use; multi-instance production deployments should use a shared rate-limit store.

`/metrics` includes queue and task gauges, worker state, retained Gemini token and optional estimated USD cost gauges, persisted queue-wait and per-stage duration histograms, attempt and stage outcome counts, and sandbox execution counts labeled by isolation mode and pass/fail outcome.

JSON and ZIP submissions are bounded at the ASGI request layer before FastAPI parsing (10 MiB JSON, 26 MiB multipart, and 1 MiB for other write bodies), then limited to 500 files, 2 MiB per file, and 1.5 MB of total UTF-8 source context for model calls. ZIP file transport is separately limited to 25 MiB compressed and expanded data, with bounded streaming extraction. Repository paths use portable NFC normalization and reject case-insensitive aliases, file/directory collisions, Windows-reserved names, and unsafe ZIP entry types. Configure upstream proxy limits as well, since the proxy/server may buffer data before application middleware receives it. The sandbox image is prepared ahead of time and test containers run without network access, with a read-only source mount, read-only root filesystem, dropped Linux capabilities, and resource limits. The API checks for the prebuilt image and never pulls it at task runtime. `RELAY_TEST_PROFILE` selects one of the fixed `pytest` or `unittest` argv profiles; arbitrary caller-supplied commands are not accepted. If Docker or the image is not available, test execution fails closed. `RELAY_ALLOW_UNSAFE_SUBPROCESS=true` enables a trusted-code-only local fallback; do not use it for untrusted archives.

The automated sandbox tests use small trusted fixtures and opt into the local subprocess fallback themselves. Run them from this directory with `.venv/bin/pytest -v tests/`.

For online SQLite backup, verification, restore instructions, readiness checks, and recovery procedures, see the repository [operations runbook](../OPERATIONS.md). Run `python backup.py /secure/backups/relay-ai` to write a permission-restricted archive of the database and task workspaces; archives are checked before publication. Use `python backup.py --verify /path/to/archive.tar.gz` to inspect an existing archive or `python backup.py --restore-archive /path/to/archive.tar.gz --restore-to /new/isolated/path` for a non-destructive restore drill.

Approval requires passing tests, clean patch application, security `PASS`, and reviewer recommendation `APPROVE`. Applying and committing operate on the stored task snapshot under `.relay_applied/` by default. They do not affect the original source tree submitted by the user. The patch endpoint can retrieve the proposed diff for application elsewhere.
