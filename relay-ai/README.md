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

The service stores task results and source snapshots in `relay_tasks.sqlite3` by default. Set `RELAY_DB_PATH` to change the location. `RELAY_APPLIED_ROOT` sets where approved task copies and commits are stored. `RELAY_CORS_ORIGINS` accepts a comma-separated origin list. Set `RELAY_API_TOKEN` to require a shared bearer token; enter it in the dashboard header. The shared token is not per-user authorization.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Service status and docs link |
| `GET` | `/health` | Runtime and sandbox capability summary |
| `GET` | `/ready` | Whether model and sandbox dependencies are ready |
| `GET` | `/tasks?limit=50&offset=0` | List persisted tasks |
| `POST` | `/tasks/report` | Queue a JSON report and codebase snapshot; returns `202` |
| `POST` | `/tasks/submit` | Queue a ZIP upload; returns `202` |
| `GET` | `/tasks/{task_id}` | Retrieve status, results, and timeline |
| `GET` | `/tasks/{task_id}/events` | Read append-only event history |
| `POST` | `/tasks/{task_id}/action` | Approve, reject, apply/commit, cancel, or retry |
| `GET` | `/tasks/{task_id}/patch` | Download the proposed unified diff |
| `GET` | `/scenarios` | Load demonstration scenarios |

Submissions are atomically persisted with a SQLite job record and processed by a lifespan-managed worker. Jobs use leases and bounded attempts; a second process can safely claim work through SQLite transactions, though SQLite remains intended for a local/small single-host deployment. Expired leases are reclaimed, progress and events survive restarts, and cancellation takes effect at stage boundaries. Failed jobs can be manually retried up to their configured attempt limit.

The sandbox image is prepared ahead of time and test containers run without network access, with a read-only source mount, read-only root filesystem, dropped Linux capabilities, and resource limits. The API checks for the prebuilt image and never pulls it at task runtime. If Docker or the image is not available, test execution fails closed. `RELAY_ALLOW_UNSAFE_SUBPROCESS=true` enables a trusted-code-only local fallback; do not use it for untrusted archives.

The automated sandbox tests use small trusted fixtures and opt into the local subprocess fallback themselves. Run them from this directory with `.venv/bin/pytest -v tests/`.

Approval requires passing tests, clean patch application, security `PASS`, and reviewer recommendation `APPROVE`. Applying and committing operate on the stored task snapshot under `.relay_applied/` by default. They do not affect the original source tree submitted by the user. The patch endpoint can retrieve the proposed diff for application elsewhere.
