# RELAY AI

RELAY AI is a local engineering workflow prototype that accepts a bug report and repository snapshot, runs an AI-assisted remediation pipeline, executes tests in an isolated sandbox when available, and presents evidence for human review.

The current build includes a React/Vite dashboard and a FastAPI backend. Task snapshots, source files, events, and jobs are stored in SQLite. A lifespan-managed worker claims queued jobs with leases, recovers abandoned work, supports stage-boundary cancellation, and allows bounded manual retries. By default, approve/apply/commit operate on a durable task-owned repository copy. Operators can optionally configure local Git targets for an explicit, base-checked apply and separate commit.

## Run locally

Requirements: Node.js, Python 3.11+, Git, and (for isolated code execution) Docker.

1. Install frontend dependencies with `npm install`.
2. Create a Python environment in `relay-ai/` and install `pip install -r requirements.txt`.
3. Configure `GEMINI_API_KEY` in the backend environment. The provider defaults to `gemini-2.5-flash` and can be changed with `GEMINI_MODEL`.
4. Build the sandbox image: `docker build -t relay-ai-sandbox:python3.11 relay-ai/sandbox`. For production, capture its immutable ID with `docker image inspect --format '{{.Id}}' relay-ai-sandbox:python3.11` and set `RELAY_SANDBOX_IMAGE` to that `sha256:...` value.
5. Start the API from `relay-ai/`: `uvicorn main:app --host 127.0.0.1 --port 8000`. Running `python main.py` also binds to localhost by default; set `RELAY_HOST` to an IP address for another interface and configure `RELAY_API_TOKEN` or `RELAY_AUTH_USERS_JSON` before doing so.
6. Start the dashboard from the repository root: `npm run dev`.

## Development checks

Run the backend suite from `relay-ai/` after activating its Python environment:

```bash
python -m pytest tests -q
```

Run the dashboard type check and production build from the repository root:

```bash
npm run lint
npm run build
```

GitHub Actions runs these backend and frontend checks for pushes to `main`, pull requests, and manual dispatch. The default backend suite skips opt-in or environment-specific drills; follow the relevant instructions in `TRACKER.md` to run those separately.

For deployment, set `RELAY_ENV=production`; the backend then requires authentication and an explicit non-wildcard CORS origin allow-list and rejects unsafe host-subprocess mode. The systemd service template enables production mode.

The dashboard uses `VITE_API_URL` when set; otherwise it connects to `http://localhost:8000`. Backend options include `RELAY_DB_PATH`, `RELAY_APPLIED_ROOT`, `RELAY_CORS_ORIGINS`, `RELAY_API_TOKEN`, `RELAY_AUTH_USERS_JSON`, `RELAY_AUTH_ROLES_JSON`, `RELAY_SANDBOX_IMAGE`, and `RELAY_ALLOW_UNSAFE_SUBPROCESS`. `RELAY_API_TOKEN` provides one shared credential. For per-user ownership, configure `RELAY_AUTH_USERS_JSON` with username-to-unique-token mappings; it takes precedence over the shared token. Optionally assign `viewer` or `operator` roles with `RELAY_AUTH_ROLES_JSON`; when the role map is set, users omitted from it are viewers. Operators can submit, cancel, retry, approve, reject, apply, commit, and delete their own tasks. Viewers can inspect only their own task data. Enter the active token in the dashboard header; it stays in the browser tab session. Keep the subprocess option disabled when handling untrusted uploads: it runs uploaded code on the API host and is provided only for trusted local development.

## What is implemented

- JSON and ZIP repository submission with file/path/size limits.
- ASGI-level request-body caps before FastAPI parses JSON or multipart uploads.
- Background task execution with persisted task state and stage evidence.
- SQLite-backed worker queue with leases, stage-boundary cancellation, and bounded retry.
- Gemini structured-output triage, patch generation, and review.
- Patch check/application in a temporary workspace, followed by sandbox test execution.
- Task list/detail review UI, scenario loading, diff/evidence display, and human approve/reject/apply/commit actions.
- Optional local Git targets can be explicitly allow-listed, base-checked, applied to, and committed separately.
- Approval policy requiring a clean patch, passing tests, security `PASS`, and reviewer `APPROVE`.

## Current limits

- SQLite queueing and fixed-window rate limits suit a single host, not multi-instance production deployment.
- If Docker is unavailable, tests are not run unless `RELAY_ALLOW_UNSAFE_SUBPROCESS=true` is explicitly enabled.
- Apply and commit create a real Git commit in the task-owned copy; they do not change the original uploaded repository or a user's local checkout.
- The static token maps provide task ownership and optional viewer/operator roles for controlled deployments but do not include token rotation or external identity-provider integration. Multi-instance production queue/rate-limit operations and deployment hardening remain roadmap items.

Before exposing a deployment beyond localhost, review [THREAT_MODEL.md](./THREAT_MODEL.md) for assets, trust boundaries, residual risks, provisional service objectives, and required ownership assignments.

See [BLUEPRINT.md](./BLUEPRINT.md) for the target architecture, [TRACKER.md](./TRACKER.md) for live implementation status, and [OPERATIONS.md](./OPERATIONS.md) for backup, restore, health, and recovery procedures.
