# RELAY AI Build Tracker

Last updated: 2026-11-05 (Asia/Kolkata)

## Status key

- `DONE` — completed and recorded.
- `IN PROGRESS` — actively being implemented.
- `BLOCKED` — requires a decision or external dependency.
- `TODO` — not started.

## Current milestone: Local end-to-end MVP — finishing durable operations, hardening, and product QA

| ID | Task | Status | Evidence / next step |
|---|---|---|---|
| P0-01 | Write product and engineering blueprint | DONE | [`BLUEPRINT.md`](./BLUEPRINT.md) |
| P0-02 | Fix Python Gemini SDK method name | DONE | Changed orchestrator to call `models.generate_content`, matching the Python SDK. |
| P0-03 | Gate approval on sandbox/reviewer evidence | DONE | Approval now requires clean patch application, passing tests, security `PASS`, and reviewer recommendation `APPROVE`. |
| P0-04 | Harden ZIP limits and normalized path checks | DONE | 25 MiB upload/expanded cap, 500-entry cap, 2 MiB/member cap, normalized path checks, duplicate and symlink rejection, and request snapshot limits. |
| P0-05 | Clarify/implement apply and commit behavior | DONE | Approval-gated apply and real commit operate in a durable task-owned repository copy; original upload is preserved and a patch download is available. |
| P0-06 | Add fake provider seam for deterministic local pipeline | DONE | `StructuredProvider`, Gemini adapter, and explicitly configured fake adapter are in `relay-ai/providers.py`. |
| P0-07 | Update docs to match implemented behavior | DONE | Root and API READMEs now describe the current prototype, setup, limitations, and routes. |

## Backlog

| ID | Task | Status | Milestone |
|---|---|---|---|
| P1-01 | Durable task and event persistence | DONE (local MVP) | SQLite atomically persists task, source snapshot, initial events, and job; stage events are insert-only and paginated. Retention and formal migration tooling remain. |
| P1-02 | Background worker and queue | DONE (local MVP) | Lifespan-managed worker claims SQLite jobs with leases; expired leases recover, retry attempts are bounded, and health reports queue counts. Multi-host production queue remains. |
| P1-03 | Progress polling/event stream, cancellation, retry, pagination | DONE (local MVP) | Dashboard polling, task/event pagination, stage-boundary cancellation, and manual bounded retry are wired to the durable queue. SSE is not needed for current polling UI. |
| P1-04 | Structured logs, readiness, limits, and retry policy | IN PROGRESS | Readiness, request/snapshot limits, worker exception logs, leases, and bounded retries are present; structured correlation logs, provider retry/backoff, and metrics remain. |
| P2-01 | Hardened sandbox adapter; fail closed without isolation | IN PROGRESS | Added prebuilt image discovery with no runtime pulls, no-network/read-only/non-root container settings, time/output limits, and fail-closed behavior; security review and execution verification remain. |
| P2-02 | Immutable repository snapshot and patch scope validation | IN PROGRESS | Source snapshot is persisted and diff headers must match safe declared paths; additional validation against repository content and hostile patch review remain. |
| P2-03 | Authorized target worktree apply and real commit | IN PROGRESS | Real Git apply/commit runs in a task-owned copy; integration to a user's actual local/remote repository remains. |
| P3-01 | Task submission, list, detail, and event timeline UI | IN PROGRESS | Dashboard supports JSON/ZIP submission, task list/detail, and event timeline; still needs visual/accessibility review. |
| P3-02 | Diff/evidence review and approval UI | IN PROGRESS | Diff, test output, reviewer evidence, approve/reject/apply/commit controls and patch download implemented; visual/accessibility review remains. |
| P3-03 | API integration and responsive/error states | IN PROGRESS | Vite UI calls FastAPI, polls in-flight tasks, and handles errors; cross-device and accessibility review remains. |
| P4-01 | Authentication and per-task authorization | IN PROGRESS | Optional shared bearer token and browser-tab token entry added; per-user identity and task ownership still needed. |
| P4-02 | Retention, observability, deployment, backup, runbooks | TODO | Phase 4 |

## Update log

### 2026-11-04 — Discovery and blueprint
- Inspected root React/Vite app, FastAPI API, orchestrator, Pydantic schemas, sandbox runner, and backend tests.
- Confirmed frontend currently renders an empty component and has no API integration.
- Confirmed task state is in-memory; apply and commit actions are simulations.
- Created the architecture, product journeys, data model direction, safety requirements, and phased delivery blueprint.
- Corrected the Python Gemini SDK method spelling in the orchestrator.
- Added an approval gate requiring passing sandbox evidence and an approving security review.

### 2026-11-05 — Phase 0 implementation and product foundation
- Hardened JSON/ZIP snapshots with file count, upload size, per-file size, expanded-size, path, duplicate, and symlink checks.
- Added Gemini and injectable structured-provider adapters plus an explicit fake adapter seam.
- Persisted tasks and source snapshots in SQLite; added an append-only event table and paginated event endpoint, task listing, and `/ready` health detail.
- Changed submissions to `202 Accepted` and in-process background execution; stage callbacks persist progress for UI polling.
- Implemented verified human approval, apply to a durable task-owned repository snapshot, actual Git commit creation, and patch download. The original upload is not changed.
- Added optional shared-token middleware and a session-only token entry in the dashboard for single-operator deployments.
- Added patch path validation against declared changed-file metadata and blocked `.git` paths in snapshots to prevent user-provided Git hooks/configuration from executing during task workspace initialization.
- Added explicit `APPLIED` and `COMMITTED` lifecycle states so UI and API state reflect real repository-copy operations.
- Removed simulated apply/commit success and implemented real apply/commit in a task-owned snapshot workspace.
- Built the React dashboard for JSON/ZIP submission, demo scenarios, task history, triage, diff, test evidence, reviewer findings, and approve/reject/apply/commit actions, with token-aware patch downloads.
- Added Docker sandbox image discovery and hardened runtime flags; runtime never pulls an image, and code execution fails closed unless the explicitly named unsafe development option is enabled.
- Rewrote setup and API documentation to match behavior.
- Resolved npm's Vite/esbuild peer conflict by aligning esbuild and removing unused server/model dependencies from the web package.
- Frontend production build, TypeScript type check, and Python `compileall` all pass. `npm install` reports zero vulnerabilities.
- Installed backend requirements in the ignored project virtual environment and confirmed the FastAPI app imports with its SQLite store.
- Updated existing lifecycle and sandbox test fixtures for evidence-gated approval, real task-copy commits, and explicitly trusted local sandbox tests. The test suite has not been run.
- Backend app import reports 15 routes. The configured isolated sandbox image was not detected in this environment, so `/ready` will remain unavailable until Docker and the image are present.

### 2026-11-05 — Durable worker and lifecycle continuation
- Replaced in-process FastAPI background execution with a lifespan-managed worker consuming SQLite job records.
- Made task creation atomic across task snapshot, initial event rows, and job queue insertion.
- Added transactional job claiming and leases, heartbeat renewal, stale lease recovery, failed-job reconciliation, cancellation requests at stage boundaries, and bounded manual retries.
- Exposed queue counts through `/health`; worker claim failures now log and recover instead of terminating the worker thread.
- Updated dashboard queued-state messaging and stopped polling terminal cancelled tasks.
- Corrected root and backend setup documentation to describe the worker actually in use.
- Verification this continuation: Python syntax compilation and FastAPI import passed (15 routes; queue initially empty). No test suite was run. Docker sandbox image remains unavailable locally.

## Decision log

| Date | Decision | Reason |
|---|---|---|
| 2026-11-04 | Treat repository apply/commit as unavailable until a target repository/worktree and authorization are explicit. | Current implementation does not mutate or commit any repository, despite its API description. |
| 2026-11-04 | Do not claim sandbox isolation when the local subprocess fallback is used. | Running untrusted repository code on the host is not a security boundary. |
| 2026-11-04 | Keep Gemini behind a provider seam and add a fake provider before expanding the pipeline. | Allows reproducible local operation and focused development without model credentials. |
| 2026-11-04 | Require test pass, clean patch application, reviewer security PASS, and recommendation APPROVE before human approval. | Prevents the API from authorizing patches the pipeline itself says failed or unsafe. |
| 2026-11-05 | Use SQLite task snapshots and in-process FastAPI background tasks for the first local async milestone. | Delivers durable task history and responsive submissions without adding external services; replace the worker mechanism before multi-instance production. |
| 2026-11-05 | Use a SQLite lease queue with a local lifespan-managed worker for the durable MVP. | Enables restart recovery and safe atomic claims on one host without an additional broker; production multi-host deployments should move to a server database and queue. |
| 2026-11-05 | Disable host subprocess execution by default and require an explicit opt-in for trusted local code only. | Repository tests execute arbitrary code, so the API must not silently run them on the host. |
| 2026-11-05 | Apply/commit against a durable task-owned copy of the uploaded snapshot. | Enables real Git operations without changing the user's original repository; patch file remains available for local application. |
| 2026-11-05 | Store event history separately with insert-only sequence records. | Preserves an auditable event trail even as the current task snapshot is updated. |
| 2026-11-05 | Add optional shared-token protection for single-operator deployments. | Protects local/controlled instances without implying per-user identity; full auth and task ownership remain required before shared deployment. |

## Verification log

No tests have been run. Previously, `npm install`, `npm run build`, `npm run lint` (TypeScript check), Python syntax compilation, and FastAPI app import passed. In this continuation, Python syntax compilation and FastAPI app import passed after the worker refactor. Lifecycle and sandbox fixtures have been updated but are not executed. The sandbox image is not available in the current environment.
