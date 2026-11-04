# RELAY AI

RELAY AI is a local engineering workflow prototype that accepts a bug report and repository snapshot, runs an AI-assisted remediation pipeline, executes tests in an isolated sandbox when available, and presents evidence for human review.

The current build includes a React/Vite dashboard and a FastAPI backend. Task snapshots, source files, events, and jobs are stored in SQLite. A lifespan-managed worker claims queued jobs with leases, recovers abandoned work, supports stage-boundary cancellation, and allows bounded manual retries. Approve, apply, and commit operate on a durable task-owned repository copy. They do not mutate the user's original checkout.

## Run locally

Requirements: Node.js, Python 3.11+, Git, and (for isolated code execution) Docker.

1. Install frontend dependencies with `npm install`.
2. Create a Python environment in `relay-ai/` and install `pip install -r requirements.txt`.
3. Configure `GEMINI_API_KEY` in the backend environment. The provider defaults to `gemini-2.5-flash` and can be changed with `GEMINI_MODEL`.
4. Build the sandbox image: `docker build -t relay-ai-sandbox:python3.11 relay-ai/sandbox`.
5. Start the API from `relay-ai/`: `uvicorn main:app --host 127.0.0.1 --port 8000`.
6. Start the dashboard from the repository root: `npm run dev`.

The dashboard uses `VITE_API_URL` when set; otherwise it connects to `http://localhost:8000`. Backend options include `RELAY_DB_PATH`, `RELAY_APPLIED_ROOT`, `RELAY_CORS_ORIGINS`, `RELAY_API_TOKEN`, `RELAY_SANDBOX_IMAGE`, and `RELAY_ALLOW_UNSAFE_SUBPROCESS`. When `RELAY_API_TOKEN` is configured, enter it in the dashboard header; it stays in the browser tab session. The token is a single shared credential, not per-user identity. Keep the subprocess option disabled when handling untrusted uploads: it runs uploaded code on the API host and is provided only for trusted local development.

## What is implemented

- JSON and ZIP repository submission with file/path/size limits.
- Background task execution with persisted task state and stage evidence.
- SQLite-backed worker queue with leases, stage-boundary cancellation, and bounded retry.
- Gemini structured-output triage, patch generation, and review.
- Patch check/application in a temporary workspace, followed by sandbox test execution.
- Task list/detail review UI, scenario loading, diff/evidence display, and human approve/reject/apply/commit actions.
- Approval policy requiring a clean patch, passing tests, security `PASS`, and reviewer `APPROVE`.

## Current limits

- SQLite and FastAPI background work suit a single local instance, not multi-instance production deployment.
- If Docker is unavailable, tests are not run unless `RELAY_ALLOW_UNSAFE_SUBPROCESS=true` is explicitly enabled.
- Apply and commit create a real Git commit in the task-owned copy; they do not change the original uploaded repository or a user's local checkout.
- Authentication beyond an optional shared bearer token, task ownership, multi-instance production queue operations, and deployment hardening remain roadmap items.

See [BLUEPRINT.md](./BLUEPRINT.md) for the target architecture and [TRACKER.md](./TRACKER.md) for live implementation status.
