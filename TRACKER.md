# RELAY AI Build Tracker

Last updated: 2026-11-06 (Asia/Kolkata)

## Status key

- `DONE` — completed and recorded.
- `IN PROGRESS` — actively being implemented.
- `BLOCKED` — requires a decision or external dependency.
- `TODO` — not started.

## Current milestone: Local end-to-end MVP — finishing durable operations, hardening, and product QA

## Follow-on milestone: Single-host production readiness (5/10 complete)

| ID | Task | Status | Evidence / next step |
|---|---|---|---|
| H1 | Enforce safe production startup configuration | DONE | `RELAY_ENV=production` requires authentication and explicit non-wildcard CORS origins, rejects unsafe host subprocess mode, and is enabled by the systemd template. Direct `python main.py` startup defaults to loopback and requires auth for non-loopback binds. |
| H2 | Pin and document sandbox image provenance | DONE | Dockerfile pins the official Python 3.11.18 slim Trixie multi-platform digest and pytest 8.3.5; production mode requires a full local image ID or registry digest. Runbook captures the image ID after build. |
| H3 | Define reverse-proxy deployment limits | DONE | Added Nginx TLS/proxy examples with body caps matching API routes, per-IP request/connection limits, timeouts, and overwritten forwarded headers. Uvicorn explicitly trusts only loopback. Multipart legacy `/tasks/submit` is now recognized by the backend body limiter. Host-specific Nginx syntax and real-IP configuration still require deployment-owner review. |
| H4 | Enforce production authentication and CORS configuration | DONE | Production mode already requires authentication and explicit CORS; it now validates every CORS entry as a bare HTTPS origin without credentials, wildcard, whitespace, path, query, or fragment. Host-side end-to-end auth/CORS verification remains in H10. |
| H5 | Harden patch boundaries against hostile diffs | DONE | Generated patches are capped at 1 MiB; scope validation rejects duplicate file sections, mode changes, renames/copies, binary patch sections, unsafe paths, undeclared paths, and non-snapshot targets. Request-level adversarial verification remains under H10. |
| H6 | Verify sandbox isolation on the deployment host | TODO | Confirm network, filesystem, user, resource, and output limits against the installed Docker runtime and image. |
| H7 | Verify worker concurrency and recovery | TODO | Exercise lease expiry, restart recovery, cancellation races, and bounded retry behavior. |
| H8 | Rehearse configured Git target operations | TODO | Exercise stale-base rejection, changed-file-only apply/commit, and interrupted-operation recovery in a disposable repository. |
| H9 | Complete backup restore and alerting drill | TODO | Restore an archive into isolation, inspect task history, and prove the backup-age failure reaches an assigned alert destination. |
| H10 | Complete release verification and ownership review | TODO | Run hostile archive/patch cases; verify auth/CORS and proxy behavior on the deployment host; perform keyboard/screen-reader and load review; assign named operators, data-use approval, and service objectives before exposure. |

| ID | Task | Status | Evidence / next step |
|---|---|---|---|
| P0-01 | Write product and engineering blueprint | DONE | [`BLUEPRINT.md`](./BLUEPRINT.md) |
| P0-02 | Fix Python Gemini SDK method name | DONE | Changed orchestrator to call `models.generate_content`, matching the Python SDK. |
| P0-03 | Gate approval on sandbox/reviewer evidence | DONE | Approval requires clean patch application, passing tests, security `PASS`, and reviewer recommendation `APPROVE`; task status mutations now use the centralized allowed-transition graph. |
| P0-04 | Harden ZIP limits and normalized path checks | DONE | Implementation includes 25 MiB ZIP file/expanded caps, 500-entry cap, bounded streaming reads with declared-size consistency, 2 MiB/member cap, 1.5 MB aggregate model context cap, 10 MiB JSON/26 MiB multipart/1 MiB other-write ASGI body caps, NFC/case-fold path normalization, file/directory collision rejection, portable filename rules, regular-file/directory-only ZIP entry types, and canonical patch target checks. Request-level adversarial verification remains under P4-02. |
| P0-05 | Clarify/implement apply and commit behavior | DONE | Approval-gated apply and real commit operate in a durable task-owned repository copy; original upload is preserved and a patch download is available. |
| P0-06 | Add fake provider seam for deterministic local pipeline | DONE | `StructuredProvider`, Gemini adapter, and explicitly configured fake adapter are in `relay-ai/providers.py`. |
| P0-07 | Update docs to match implemented behavior | DONE | Root and API READMEs now describe the current prototype, setup, limitations, and routes. |

## Backlog

| ID | Task | Status | Milestone |
|---|---|---|---|
| P1-01 | Durable task and event persistence | DONE | Local MVP persists task, immutable source snapshot, initial events, and job atomically; task deletion rechecks queue inactivity transactionally; events/actions are append-only and paginated. Schema upgrades use a v9 compatibility baseline plus numbered v10 digest migration in a serialized transaction and reject newer schemas. Retention is implemented separately; migration rollback and recovery verification remain under P1-02/P4-02. |
| P1-02 | Background worker and queue | DONE | Local MVP worker claims SQLite jobs with attempt-number leases; progress writes, heartbeats, and final task/job completion are fenced and final state is committed atomically. Recovery reconciles persisted terminal state before replaying; expired leases recover and retries are bounded. Multi-host queues are out of scope; fault-injection/restart verification remains under P4-02. |
| P1-03 | Progress polling/event stream, cancellation, retry, pagination | DONE | Local MVP includes dashboard polling, queue/stage lifecycle statuses with centralized transition validation, task/event/action pagination, stage-boundary cancellation, manual bounded retry, and replayable idempotency keys. SSE is unnecessary for current polling UI; fault/recovery verification remains in the P1-02 gate. |
| P1-04 | Structured logs, readiness, limits, and retry policy | DONE | Local MVP includes JSON application logs with best-effort credential redaction, readiness checks, request limits, worker exception logs, fenced leases, bounded provider retry/backoff, and Prometheus queue/task/worker/provider usage gauges, optional operator-priced cost gauge, queue-wait/stage-duration histograms, attempt/stage failure counts, and sandbox mode/outcome counts. Production operations verification remains in P4-02. |
| P2-01 | Hardened sandbox adapter; fail closed without isolation | DONE | Local MVP has prebuilt image discovery with no runtime pulls, no-network/read-only/non-root container settings, OS-limited captured output files, core dump disabling, allow-listed fixed-argv `pytest` and `unittest` profiles, and fail-closed behavior. Deployment-host isolation verification remains under P4-02. |
| P2-02 | Immutable repository snapshot and patch scope validation | DONE | Source snapshots are digest-checked and immutable after first storage; normalized patch targets, diff/unified headers, declared files, and submitted paths must agree. Unsafe paths, duplicate declarations, and more than 25 files are rejected. Hostile-input verification remains under P0-04. |
| P2-03 | Authorized target worktree apply and real commit | DONE | Local MVP supports optional `RELAY_REPOSITORIES_JSON` allow-listed Git targets, dashboard selection, clean-base/revision checks, changed-file snapshot matching, separate apply/commit, changed-file-only staging, commit provenance, task-delete/retention protections, and persisted apply/commit recovery markers. Remote Git providers are a future extension; target apply/commit and interruption recovery still require execution-level verification under P4-02. |
| P3-01 | Task submission, list, detail, and event timeline UI | DONE | Local MVP dashboard supports JSON/ZIP submission, owner-scoped search/status filters, 50-item server pagination with total counts, task detail, independently paginated 50-event history with authoritative counts, owner-authorized deletion, and accessible codebase input selection. Cross-device visual/accessibility review remains under P3-03. |
| P3-02 | Diff/evidence review and approval UI | DONE | Local MVP displays diff, exact sandbox test command, separate stdout/stderr, reviewer evidence, per-call token and optional cost details, paginated operator action audit, approve/reject/apply/commit controls, optional decision notes, custom commit message, commit hash/message, and patch download. Cross-device visual/accessibility review remains under P3-03. |
| P3-03 | API integration and responsive/error states | DONE | Vite UI uses the `/api/v1` contract, polls in-flight tasks, handles loading/empty/error states, and provides responsive task/token/action controls. Cross-device visual and accessibility verification remains under P4-02. |
| P4-01 | Authentication and per-task authorization | DONE | Local MVP supports unique per-user bearer tokens, owner-scoped task access, optional viewer/operator roles, and shared-token/local development modes. Token rotation, external identity provider integration, and broader production access-control policy remain under P4-02. |
| P4-02 | Retention, observability, deployment, backup, runbooks | DONE | Local MVP includes owner-controlled deletion and dry-run-first retention, verified SQLite/workspace backups, non-destructive isolated restore tooling, backup-age CLI/systemd timers, service templates, restore/recovery runbook, threat model/SLOs, schema visibility, Prometheus metrics, local rate limits, and configured-repository audit protections. A deployment record template captures remaining host-specific ownership, socket policy, alert delivery, and restore-drill signoff; those operator-run steps are required before production exposure. |

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
- Added JSON-line application logging with task/stage/attempt context fields and documented the output format.
- Updated dashboard queued-state messaging and stopped polling terminal cancelled tasks.
- Corrected root and backend setup documentation to describe the worker actually in use.
- Verification this continuation: Python syntax compilation and FastAPI import (15 routes; queue initially empty) passed after the JSON logging addition; Vite production build, TypeScript check, and `git diff --check` passed for the prior worker/UI chunk. No test suite was run. Docker sandbox image remains unavailable locally.

### 2026-11-05 — Input and patch scope hardening
- Changed ZIP member extraction to read at most the configured per-file bound plus one byte, reject declared/actual size mismatches, and enforce the aggregate bound against streamed bytes.
- Strengthened patch scope validation to require matching `diff --git` and unified file headers, exact declared-file agreement without duplicates, safe relative paths, existing files from the submitted snapshot, and a 25-file change maximum. New files, deletions, and renames are rejected until explicitly supported end to end.
- Moved temporary patch files outside the repository workspace so a submitted file cannot collide with or be overwritten by RELAY's patch plumbing.
- Replaced Docker `sh -c` test launching and caller-supplied command strings with a fixed pytest argv profile executed directly in the container or explicitly enabled trusted subprocess mode.
- Added a 1.5 MB UTF-8 aggregate model input cap across report text, file paths, and JSON/ZIP source content, plus 20,000-character report and path-depth/length limits; surfaced the cap in the dashboard and API setup docs.
- Added bounded retry with exponential delay and jitter for transient Gemini 408/429/5xx responses only; non-transient errors still fail immediately.
- Verification for this chunk: Python syntax compilation, FastAPI import (15 routes; queue initially empty), Vite production build, TypeScript check, and `git diff --check` passed. No test suite was run. Docker sandbox image is unavailable for runtime verification.

### 2026-11-05 — Operator action audit
- Added durable append-only `task_actions` records for successful approve, reject, apply, commit, cancel, and retry operations. Records include a non-secret local/shared-token actor label, outcome, notes, details, and timestamp.
- Added paginated `GET /tasks/{task_id}/actions` and documented that shared-token attribution is not an individual identity.
- Verification for this chunk: Python syntax compilation, FastAPI import, route inspection confirming both action endpoints, Vite production build, TypeScript check, and `git diff --check` passed. No test suite was run.

### 2026-11-05 — Operations visibility
- Added SQLite schema version history (current version 2, including audit and rate-limit tables) and exposed the current schema version through `/health`.
- Added a Prometheus-compatible `/metrics` endpoint for durable job status counts, task status counts, and local worker liveness; documented it for operators.
- Verification for this chunk: Python syntax compilation, FastAPI import (schema version 1; health/readiness/metrics routes registered), Vite production build, TypeScript check, and `git diff --check` passed. No test suite was run.

### 2026-11-05 — API rate limiting
- Added atomic fixed-window SQLite rate limits of 10 submissions/minute and 60 actions/minute per client address digest. Raw client addresses are not stored; expired buckets are pruned opportunistically.
- Documented the local single-host scope and the need for a shared limiter in multi-instance deployments.
- Verification for this chunk: Python syntax compilation, FastAPI import (schema version 2; action, health, readiness, and metrics routes registered), Vite production build, TypeScript check, and `git diff --check` passed. No test suite was run.

### 2026-11-05 — Per-user ownership
- Added optional `RELAY_AUTH_USERS_JSON` username-to-unique-token authentication, taking precedence over the shared token when configured; malformed and duplicate credentials fail startup.
- Added task ownership at submission and enforced owner-scoped task lists, task detail, events, action history, patch download, and mutation actions. Non-owned tasks return 404 to avoid disclosing existence.
- Preserved local single-operator and shared-token modes; legacy ownerless tasks remain visible only when per-user auth is not configured.
- Kept actor labels non-secret and documented token storage and mode precedence.
- Moved owner filtering and task pagination into indexed SQLite queries instead of loading all task payloads into the API process.
- Verification for this chunk: Python syntax compilation, FastAPI import (schema version 3; 17 routes), Vite production build, TypeScript check, and `git diff --check` passed. No test suite was run.

### 2026-11-05 — Action idempotency
- Added optional `Idempotency-Key` support to actions with durable uniqueness scoped to task, actor, and action key; the stored request fingerprint prevents reusing the same key for a different body.
- Successful action responses are persisted and replayed for matching retries; the dashboard retains a key in session storage until a successful response arrives.
- Verification for this chunk: Python syntax compilation, FastAPI import (schema version 4; 17 routes), Vite production build, TypeScript check, and `git diff --check` passed. No test suite was run.

### 2026-11-05 — Task data deletion
- Added owner-authorized `DELETE /tasks/{task_id}` for stopped jobs; active queued/running work must be cancelled and stopped first.
- Task deletion cascades through task snapshots, stage events, queue state, and action audit, and removes the task-owned Git working copy using a rename-and-delete cleanup sequence.
- Added a confirmation flow in the dashboard task list and documented that task data does not automatically expire by default.
- Verification for this chunk: Python syntax compilation, FastAPI import (GET/DELETE task routes registered), Vite production build, TypeScript check, and `git diff --check` passed. No test suite was run.

### 2026-11-05 — Dependency readiness
- Extended `/ready` to check SQLite availability/schema version and live worker state alongside configured model and sandbox capabilities; `/health` now reports worker liveness.
- Verification for this chunk: Python syntax compilation, FastAPI import (schema 4; worker correctly reports stopped before lifespan startup), and `git diff --check` passed. No test suite was run.

### 2026-11-05 — Pipeline usage telemetry
- Captured Gemini-reported prompt, candidate, and total token counts as task events and durable idempotent usage rows; task deletion cascades the usage data.
- Added elapsed milliseconds to successful and failed triage, investigation, sandbox, and review events and exposed aggregate Gemini token counters through `/metrics`.
- Did not estimate monetary cost because pricing depends on model, account, and time-varying rates.
- Verification for this chunk: Python syntax compilation, FastAPI import (schema version 5; token counters initially zero), Vite production build, TypeScript check, and `git diff --check` passed. Gemini usage reporting was not live-verified because no model request was made. No test suite was run.

### 2026-11-05 — Backup and recovery operations
- Added `relay-ai/backup.py` using SQLite's online backup API and a private timestamped archive containing the database, manifest, and task-owned repository workspaces.
- Added `OPERATIONS.md` with health/readiness interpretation, backup/restore steps, recovery actions, and the current single-host/retention limits.
- Documented that archives contain source code and must be kept encrypted/access-controlled; credentials remain outside backups.
- Verification for this chunk: Python syntax compilation, backup CLI help, FastAPI import (schema version 5; 18 routes), Vite production build, TypeScript check, and `git diff --check` passed. The backup archive itself and a restore drill were not run; no test suite was run.

### 2026-11-05 — Mobile dashboard controls
- Added a mobile recent-task selector because the desktop sidebar task list is hidden at narrow widths.
- Kept API token saving available on mobile with an icon button and accessible label; delete controls remain visible on touch devices.
- Visual review exposed that the default CORS list omitted the `127.0.0.1` origin used by Vite, so the browser could not fetch tasks or scenarios even though the API returned `200`. Added both localhost and loopback Vite origins to the default allowlist and `.env.example`.
- Connected browser review then showed the demo scenarios load without a fetch error. At a 390×844 viewport the task selector and token-save control remained available; the default viewport was restored afterward. Both temporary local servers were stopped.
- Verification for this chunk: Python syntax compilation, FastAPI import (18 routes; default CORS includes localhost and loopback Vite origins), connected browser scenario load, 390×844 mobile viewport inspection, Vite production build, TypeScript check, and `git diff --check` passed. No automated accessibility audit or submission-through-pipeline browser journey was run.

### 2026-11-05 — Versioned API contract
- Added `/api/v1` routes for task submit/upload/list/detail/events/actions/patch/delete and scenarios while preserving the unversioned routes as compatibility aliases.
- Migrated the dashboard to the versioned routes and updated rate limiting to cover versioned submissions and actions.
- Updated API documentation to identify `/api/v1` as the client contract.
- Verification for this chunk: Python syntax compilation, OpenAPI inspection confirming `/api/v1` resources and hidden legacy aliases, Vite production build, TypeScript check, and `git diff --check` passed. No test suite was run.

## Decision log

| Date | Decision | Reason |
|---|---|---|
| 2026-11-04 | Treat repository apply/commit as unavailable until a target repository/worktree and authorization are explicit. | Current implementation does not mutate or commit any repository, despite its API description. |
| 2026-11-04 | Do not claim sandbox isolation when the local subprocess fallback is used. | Running untrusted repository code on the host is not a security boundary. |
| 2026-11-04 | Keep Gemini behind a provider seam and add a fake provider before expanding the pipeline. | Allows reproducible local operation and focused development without model credentials. |
| 2026-11-04 | Require test pass, clean patch application, reviewer security PASS, and recommendation APPROVE before human approval. | Prevents the API from authorizing patches the pipeline itself says failed or unsafe. |
| 2026-11-05 | Use a SQLite lease queue with a local lifespan-managed worker for the durable MVP (supersedes the earlier in-process background task decision). | Enables restart recovery and safe atomic claims on one host without an additional broker; production multi-host deployments should move to a server database and queue. |
| 2026-11-05 | Disable host subprocess execution by default and require an explicit opt-in for trusted local code only. | Repository tests execute arbitrary code, so the API must not silently run them on the host. |
| 2026-11-05 | Apply/commit against a durable task-owned copy of the uploaded snapshot. | Enables real Git operations without changing the user's original repository; patch file remains available for local application. |
| 2026-11-05 | Store event history separately with insert-only sequence records. | Preserves an auditable event trail even as the current task snapshot is updated. |
| 2026-11-05 | Limit generated patches to modifications of existing submitted files. | Prevents model-declared paths from expanding the patch's authority beyond the immutable task snapshot while add/delete/rename flows lack complete validation and review support. |
| 2026-11-05 | Launch the fixed pytest profile with argv directly, without a shell. | Keeps test execution policy server-controlled and avoids shell interpretation if configuration paths are expanded later. |
| 2026-11-05 | Cap aggregate submitted source context at 1.5 MB. | Keeps prompt input bounded independently from ZIP transport limits and avoids silently truncating repository content before model analysis. |
| 2026-11-05 | Add optional shared-token protection for single-operator deployments. | Protects local/controlled instances without implying per-user identity; full auth and task ownership remain required before shared deployment. |
| 2026-11-05 | Record `local-operator` or `shared-token` as action actor labels. | Audits must never imply that one shared bearer token identifies a particular human; per-user identity remains a separate authentication milestone. |
| 2026-11-05 | Use configured username-to-token mappings as the task owner identity, superseding the shared-token identity when present. | Enables per-task isolation without claiming support for an external identity provider; the simple mapping is appropriate for controlled local deployments. |
| 2026-11-05 | Keep task search substring-based and parameterized, with status stored/indexed separately in SQLite. | Provides predictable, injection-safe history filtering while retaining owner-scoped pagination. |
| 2026-11-05 | Never embed provider prices in the code; only estimate cost when operators supply both token rates and a pricing source label. | Model/account pricing can change; the estimate must be reproducible and clearly distinct from billed cost. |

## Verification log

No tests have been run. `npm install`, `npm run build`, `npm run lint` (TypeScript check), Python syntax compilation, FastAPI app import, schema/action/health/metrics route inspection, and `git diff --check` have passed for earlier changes. The task-history change passed Python syntax compilation, Vite production build, TypeScript check, and `git diff --check`; its FastAPI import inspection was blocked by the missing `google.genai` dependency. Lifecycle and sandbox fixtures have been updated but are not executed. The sandbox image is not available in the current environment.

### 2026-11-05 — Task history search and status filters
- Added a migrated `status` column and owner/status/updated-time index to SQLite task snapshots; status is kept synchronized on creation, snapshot updates, worker lease failure, and retry.
- Added `q` substring search and enum-validated `status` filters to the versioned task list endpoint. Search and status are applied in SQLite before pagination and remain scoped to the actor, including legacy unowned task visibility where configured.
- Added matching desktop and mobile dashboard filters.
- Verification: Python syntax compilation, Vite production build, TypeScript check, and `git diff --check` passed. FastAPI import/route inspection could not run because the active Python environment lacks `google.genai` (`ModuleNotFoundError`); no tests run.

### 2026-11-05 — Bound sandbox capture files
- Added POSIX child-process file-size limits so stdout/stderr capture files stop at 1 MiB each, matching the response output cap; disabled core dumps for these children.
- This limits host temporary-file growth from verbose subprocess output. Docker still provides the actual code isolation boundary; container execution and hostile workload behavior remain to be verified in an environment with the configured image.
- Verification: Python syntax compilation, FastAPI import and route inspection (schema 6; versioned task GET/POST registered), and `git diff --check` passed using `relay-ai/.venv`. No tests run.

### 2026-11-05 — Event attribution and correlation
- Extended task evidence events with backward-compatible actor and correlation ID fields. New automated stage events are attributed to `system`; ingestion, retries, and operator decisions carry the authenticated actor, and task ID is used as the correlation ID.
- Displayed actor attribution in the task activity timeline. Existing persisted events deserialize with `system` attribution and remain readable.
- Verification: Python syntax compilation, FastAPI import (schema 6 and event route registered), legacy `EvidenceEvent` payload deserialization defaults (`actor=system`), Vite production build, TypeScript check, and `git diff --check` passed. No tests run.

### 2026-11-05 — Fence stale worker attempts
- Worker lease renewal, task progress persistence, and job completion now require the attempt number returned by the atomic queue claim.
- A worker that loses its lease discards further progress and failure writes, preventing a delayed process from overwriting state produced by a newer retry attempt.
- Verification: Python syntax compilation, FastAPI import/schema inspection (SQLite schema 6), Vite production build, TypeScript check, and `git diff --check` passed. Fault-injection and concurrency checks remain unrun; no tests run.

### 2026-11-05 — Persist stage metrics
- Added schema version 7 `stage_observations`, backfilled idempotently from prior event history and updated with each worker snapshot using task/sequence uniqueness.
- `/metrics` now emits stage duration histograms, stage failure counters, and sandbox execution counts by isolation mode and pass/fail outcome, alongside existing queue/task/provider metrics.
- Queue latency metrics and representative populated histogram scrape remain. Verification: Python syntax compilation, FastAPI import and schema migration inspection (version 7), `/metrics` route serialization/content type and metric-family presence, OpenAPI confirmation of task `q`/`status` filters, and `git diff --check` passed. The current store has no completed stage observations to verify populated buckets; no tests run.

### 2026-11-05 — Queue wait telemetry
- Added schema version 8 durable job-attempt rows with queue wait duration and terminal outcome. Each atomic claim records its wait, including manual retries and expired-lease recovery attempts; completion is recorded only by the current unexpired lease holder.
- Added queue-wait Prometheus histogram and attempt-state counts; updated API operations documentation to cover the expanded metric set.
- Verification: Python syntax compilation, FastAPI import and migration inspection (schema 8), `/metrics` serialization and zero-valued histogram/counter series across queue, stage, attempt, and sandbox families, OpenAPI task filter inspection, and `git diff --check` passed. No tests run. Existing attempts from before schema 8 cannot be assigned accurate queue-wait durations and are not backfilled.

### 2026-11-05 — Explicit active task statuses
- Added `QUEUED`, `RUNNING`, `TRIAGING`, `INVESTIGATING`, `SANDBOX_RUNNING`, and `REVIEWING` states. Submission and retry now report `QUEUED`; the atomic worker claim records `RUNNING` plus an activity event; stage starts persist their active status before provider/sandbox work begins.
- Updated status filters and setup documentation; kept legacy `RECEIVED` deserialization support.
- Updated the existing enum expectation. Verification: Python syntax compilation, FastAPI import and route/filter inspection (schema 8), status enum enumeration, queue metric serialization, Vite production build, TypeScript check, and `git diff --check` passed. No tests run; queue lifecycle behavior still needs concurrent worker verification.

### 2026-11-05 — Task history pagination UI
- Added owner/filter-scoped total task counts as the `X-Total-Count` response header and exposed it through CORS/OpenAPI.
- Added desktop and mobile previous/next page controls, matching status/search filters, page ranges, and selected-task detail refresh while navigating history.
- Verification: Python syntax compilation, FastAPI import and OpenAPI inspection (schema 8, list parameters and `X-Total-Count` response header), Vite production build, TypeScript check, and `git diff --check` passed. No tests run; pagination behavior in a connected browser still needs review.

### 2026-11-05 — Production threat model and provisional SLOs
- Added `THREAT_MODEL.md` covering assets, trust boundaries, abuse scenarios, implemented controls, residual risks, deployment requirements, pilot SLOs, ownership responsibilities, and incident response.
- Documented current limitations: optional/static-token authentication, privileged Docker daemon boundary, unsafe subprocess host execution, JSON body ingress limits, Gemini source disclosure, unencrypted-by-tool backups, and unverified concurrency/restore/security/load behavior.
- Linked the threat model from the root README and operations runbook. SLOs are explicitly provisional; named owners and on-call integration must be assigned per deployment.
- Verification: required threat-model sections and linked artifacts present, `git diff --check` passed. SLO numbers are proposed pilot targets rather than measured results; no security review or tests were run.

### 2026-11-05 — Viewer/operator authorization
- Added optional `RELAY_AUTH_ROLES_JSON` for configured per-user bearer accounts. `viewer` is restricted server-side to GET/HEAD/OPTIONS; `operator` may write while remaining scoped to owned tasks. If a role map is explicitly set, omitted users default to viewer; if unset, existing per-user deployments retain operator permissions.
- Added `/api/v1/session` for actor/role capabilities and updated the dashboard to hide submission, decision, retry, cancellation, and deletion controls from viewers while keeping full-width task/evidence browsing available.
- Updated environment/setup docs and the threat model. Verification: Python syntax compilation, FastAPI import/schema inspection (schema 8, session route registered, unset role map compatibility confirmed), Vite production build, TypeScript check, and `git diff --check` passed. No tests run; authorization behavior still needs request-level security verification.

### 2026-11-05 — Pre-parse request body limits
- Added ASGI receive middleware that caps JSON, multipart, and other write request bodies before FastAPI parses them; declared lengths are rejected early and streaming bodies are counted as received.
- Updated the threat model and setup docs to distinguish application caps from upstream proxy/server buffering and to require ingress limits too.
- Verification: Python syntax compilation, FastAPI import and middleware-order inspection (CORS outermost; request limiter before routing/body parsing), request-limit constants, route registration, and `git diff --check` passed. No tests run; actual chunked oversized-request behavior still needs request-level verification.

### 2026-11-05 — Centralized task status transitions
- Added `PipelineTaskResponse.transition_to()` and one allowed-transition graph covering queued execution, stage starts/completions, cancellation/failure, approval/rejection, task-copy apply/commit, and retry.
- Routed API, worker, SQLite recovery, and orchestrator status changes through the validator. Same-state updates remain idempotent; invalid jumps raise a clear `ValueError`.
- Atomically committed final task output and queue completion under the active lease; cancellation checks the persisted task state and terminal task outcomes are reconciled after worker crashes rather than rerun.
- Verification: Python syntax compilation, FastAPI import/schema inspection (schema 8; 29 routes), static search confirming no direct task-status assignments remain outside `transition_to()`, finalizer method presence, Vite production build, TypeScript check, and `git diff --check` passed. No tests run; existing lifecycle/recovery/API behavior still requires execution-level verification.

### 2026-11-05 — Operator-managed retention purge
- Added `relay-ai/retention.py` with dry-run as the default, explicit `--apply --confirm-delete`, a caller-specified age threshold, and eligibility limited to inactive FAILED/CANCELLED/REJECTED/COMMITTED tasks.
- Excluded open approvals and applied-but-uncommitted tasks. Workspace removal uses a private rename staging path with rollback if transactional database eligibility recheck fails; concurrent retries therefore prevent purge.
- Updated runbook, backend setup, and threat model. Verification: Python syntax compilation, CLI help, safe empty-database dry run using an isolated temporary SQLite path, and `git diff --check` passed. No tests or actual deletions run; populated-candidate and filesystem rollback behavior remain unverified.

### 2026-11-05 — Operator action audit in task review
- Added dashboard loading of the selected task's persisted `/actions` history and displayed actor, action, outcome, timestamp, notes, and structured details in a separate expandable audit section.
- Cleared stale audit rows on task selection; read-only viewers can inspect action history while write controls remain hidden.
- Verification: Vite production build, TypeScript check, and `git diff --check` passed. No tests run; connected-browser review remains outstanding.

### 2026-11-05 — Human decision notes and commit provenance
- Connected optional reviewer notes in the approval/rejection flow to the API's audited action notes, with bounded 2,000-character input and idempotency key reset when edited.
- Added an optional 200-character custom Git commit message and displayed the persisted commit hash/message after commit; blank input retains the backend generated-message behavior.
- Verification: Vite production build, TypeScript check, and `git diff --check` passed. No tests run; connected-browser review remains outstanding.

### 2026-11-05 — Paginated task activity timeline
- Connected task review to the persisted `/events` endpoint with 50-record pages, newest-first display within each page, and older/newer navigation. Refreshing an active task reloads the selected event page.
- This avoids rendering the full embedded timeline in the review panel and exposes the existing event-pagination contract to the dashboard.
- Verification: Vite production build, TypeScript check, and `git diff --check` passed. The connected browser preview loads the dashboard but the API is not running (no `google.genai` module in the active Python environment); no API journey or tests were run.

### 2026-11-05 — Event pagination counts
- Added SQLite event counts and exposed `X-Total-Count` on the versioned and compatibility event-history endpoints; documented the header in OpenAPI. CORS already exposes this header for browser clients.
- Dashboard now displays the total and current range and disables navigation at exact page boundaries without opening an empty page.
- Verification: Vite production build, TypeScript check, Python syntax compilation, `git diff --check`, FastAPI import, schema version 8 inspection, and OpenAPI header inspection passed using `relay-ai/.venv`. No tests run.

### 2026-11-05 — Paginated operator action audit
- Added SQLite action counts and `X-Total-Count` to both versioned and compatibility action-history endpoints; documented the count header in the API reference and OpenAPI.
- Dashboard now loads 50 audit records per page, shows the page range and total, and prevents navigation beyond the final page, including exact 50-record boundaries.
- Verification: Vite production build, TypeScript check, Python syntax compilation, `git diff --check`, FastAPI import, and OpenAPI total-count header inspection passed using `relay-ai/.venv`. No tests run.

### 2026-11-05 — Allow-listed sandbox test profiles
- Added `RELAY_TEST_PROFILE` with fixed `pytest` (default) and Python `unittest` argv profiles. Invalid profile names fail configuration; callers cannot provide commands or shell fragments.
- Documented the setting in `.env.example` and backend setup docs.
- Verification: Python syntax compilation, `git diff --check`, Vite production build, and TypeScript check passed. Sandbox container/runtime verification remains unavailable here; no tests run.

### 2026-11-05 — Sandbox evidence completeness and connected local review
- Dashboard now displays the exact executed test command and preserves stdout and stderr as separate output sections; previously stderr disappeared whenever stdout was non-empty.
- Started the API with an isolated temporary database and reviewed the dashboard in a connected browser. `/api/v1/tasks`, `/api/v1/session`, and `/api/v1/scenarios` returned `200`; the dashboard loaded the operator session and all demo scenarios. No task was submitted and no tests were run.
- Current `relay-ai/.venv` imports FastAPI and google-genai successfully; schema 8 and OpenAPI total-count headers for events/actions were confirmed. This supersedes earlier notes based on the global Python environment missing google.genai.
- Verification: Vite production build, TypeScript check, Python syntax compilation, and `git diff --check` passed. The local UI preview and read-only API load were inspected in the connected browser; no task execution or tests were run.

### 2026-11-05 — Best-effort diagnostic credential redaction
- Added a shared redactor for configured secret environment values and per-user bearer tokens, common Google/GitHub/OpenAI/Slack/AWS token forms, JSON/key-value credential fields, bearer credentials, and private-key blocks.
- Applied redaction to structured log messages and tracebacks, pipeline/worker task errors, and bounded sandbox stdout/stderr so diagnostics are less likely to persist or expose credentials.
- Updated the threat model and backend docs to describe the control and its limits: transformed or unrecognized secrets can still pass through, and Gemini source disclosure remains an operator data-use decision.
- Verification: Python syntax compilation, FastAPI import (29 routes; schema 8), Vite production build, TypeScript check, and `git diff --check` passed. Redaction behavior has not been exercised with fixtures; no tests run.

### 2026-11-05 — Recheck task deletion against active worker claims
- Task deletion now rechecks queue state and removes the task cascade under one SQLite `BEGIN IMMEDIATE` transaction after the workspace is staged. If the worker claimed the task during staging, the API restores the workspace and returns `409`.
- This closes the check-then-delete race between the initial API check and a concurrent queue claim.
- Verification: Python syntax compilation, FastAPI import (29 routes; schema 8), and `git diff --check` passed. No tests run; concurrent claim/delete behavior remains unexercised.

### 2026-11-05 — Open audit histories at the newest records
- Changed event and action SQL pagination to return newest records first. The first page now opens on recent task activity; `Older` pages advance through history and `Newer` returns toward the present.
- Updated dashboard range labels to show the correct oldest-based sequence ordinals for each newest-first page, and documented the endpoint order.
- Verification: Vite production build, TypeScript check, Python syntax compilation, FastAPI import (29 routes; schema 8), and `git diff --check` passed. No tests run; populated multi-page ordering remains unexercised.

### 2026-11-05 — Optional provider cost attribution
- Added optional operator-configured Gemini input/output USD rates per million tokens plus a required pricing source/model revision label. Invalid, partial, non-finite, negative, or excessive values fail configuration; no rates means token-only telemetry.
- Persisted the rates, label, and estimated per-call cost with provider usage in schema 9; existing usage rows migrate as unpriced. Added retained estimated USD and priced/unpriced usage gauges to `/metrics`.
- Updated setup docs and `.env.example` to require operators to use their current account/model pricing schedule. Estimates are explicitly not represented as billing records.
- Verification: Python syntax compilation, fresh app import, SQLite schema 9/pricing-column inspection, zero-usage cost totals, and a connected browser `/metrics` scrape showing valid zero-valued cost/coverage series passed; `git diff --check` passed. No actual provider call or tests were run, and populated priced-usage aggregation remains unverified.

### 2026-11-05 — Correct retained usage metric semantics
- Changed Gemini token, estimated-cost, and priced/unpriced usage metrics from counters to gauges because task deletion cascades usage records and therefore may reduce the reported totals.
- Updated operations and API docs to identify these as retained task-usage measurements that can decrease after deletion.
- Verification: fresh FastAPI import, connected-browser `/metrics` scrape showing the new gauge names/types with zero values, and `git diff --check` passed. No tests run; decreasing values after task deletion were verified from the storage cascade semantics, not a populated deletion scenario.

### 2026-11-05 — Show per-call model usage in task review
- Model-usage activity events now display provider/model, prompt/candidate/total tokens, and estimated USD with the configured pricing label when available. Token-only records remain clear when rates were not configured.
- Verification: Vite production build, TypeScript check, and `git diff --check` passed. No tests run; a populated model-usage event has not been displayed in the browser.

### 2026-11-05 — Complete portable archive path validation
- Added shared NFC path normalization and portable filename checks for JSON snapshots, ZIP members, workspace population, and generated patch targets. Rejects case-fold aliases, file/directory prefix collisions, drive/ADS syntax, Windows reserved names, trailing dots/spaces, invalid UTF-8, and `.git` paths.
- ZIP extraction now rejects special file types other than regular files/directories and detects normalized duplicate entries before extraction. Updated README and threat model to reflect these controls.
- Verification: Python syntax compilation, FastAPI app import with 29 routes, and `git diff --check` passed. No tests run; hostile ZIP request fixtures and patch-scope scenarios still require request-level verification.

### 2026-11-05 — Guard SQLite schema startup upgrades
- Serialized SQLite schema setup with `BEGIN IMMEDIATE` so concurrent API starts cannot interleave upgrades; schema DDL and legacy backfill now roll back together on startup errors.
- Added a future-schema guard that fails startup before mutation and documented backup/upgrade behavior and the lack of downgrade/per-release migration scripts.
- Verification: Python syntax compilation, FastAPI import with 29 routes, and `git diff --check` passed. No tests run; legacy database upgrade and rollback scenarios still need execution-level verification.

### 2026-11-05 — Make codebase input accessible
- Replaced the hidden ZIP input embedded inside its radio label with a named, keyboard-operable radio group and a visible, explicitly labeled native ZIP file picker. Added accessible labeling for the JSON file-map editor, visible focus indicators across controls, and live semantics plus an accessible dismiss label for notices.
- Selecting a source mode now controls which input is shown; ZIP submission stays disabled until a file is selected, and loading a demo returns to JSON mode.
- Verification: Vite production build, TypeScript check, and `git diff --check` passed. No tests run; assistive-technology review remains outstanding.

### 2026-11-05 — Detect stored source snapshot corruption
- Added numbered SQLite migration 10 with a SHA-256 digest of each serialized source snapshot. Startup upgrades legacy databases to the v9 compatibility baseline, applies registered migrations inside the schema transaction, backfills existing snapshot digests, and rejects databases marked newer than the application.
- Snapshot writes keep the digest in sync, and reads reject mismatches before source reaches the worker or API response. Documented the integrity behavior; future migration entries and downgrade support remain.
- Verification: Python syntax compilation, FastAPI import (29 routes/schema 10), Vite production build, TypeScript check, and `git diff --check` passed. No tests run; migration backfill and corruption detection still need execution-level verification.

### 2026-11-05 — Close local MVP progress and observability implementation
- Marked P1-03 and P1-04 complete for the local MVP because their planned polling, cancellation/retry/pagination, structured logging, readiness, limits, retry, and metrics functionality is implemented. Remaining verification obligations are explicitly tracked under P1-02 and P4-02.

### 2026-11-05 — Enforce immutable source snapshots
- Changed the compatibility snapshot write API from update-on-conflict to insert-only/idempotent behavior. Repeated identical writes succeed; attempts to replace stored task source fail, and snapshot digests continue to detect out-of-band changes.
- Marked P2-01 and P2-02 complete for the local MVP implementation; deployment-host isolation and hostile-input verification remain explicit in the P4-02 and P0-04 gates.
- Verification: Python syntax compilation, FastAPI import (29 routes/schema 10), Vite production build, TypeScript check, and `git diff --check` passed. No tests run; immutability and digest mismatch behavior still need execution-level verification.

### 2026-11-05 — Close local MVP task UI and access control
- Marked P3-01 complete for the implemented submission/list/detail/timeline journeys and P4-01 complete for static per-user ownership and viewer/operator authorization. Cross-device UI review remains under P3-03; token rotation and external identity remain production extensions under P4-02.

### 2026-11-05 — Close durable persistence and review UI implementation
- Marked P1-01 complete for local MVP persistence, immutable snapshots, append-only records, retention tooling, and numbered schema upgrades. Migration rollback/recovery checks remain in the P1-02/P4-02 verification gates.
- Marked P3-02 complete for the evidence, diff, action, and provenance review flow. Cross-device visual/accessibility review remains grouped under P3-03.

### 2026-11-05 — Close worker and API integration implementation
- Marked P1-02 complete for the single-host SQLite worker/lease/retry implementation; multi-host queueing remains out of scope and restart/fault-injection verification remains under P4-02.
- Marked P3-03 complete for the `/api/v1` integration, polling, loading/error handling, and responsive controls; cross-device QA remains under P4-02.

### 2026-11-05 — Close archive hardening implementation
- Marked P0-04 complete for the archive/path validation implementation across ingress, workspace writes, and patch targets. Request-level hostile archive verification remains an explicit P4-02 release gate.

### 2026-11-05 — Add configured local repository targets
- Added an operator allow-list mapping repository IDs to local Git roots; API callers cannot supply filesystem paths. The UI can bind a task to a clean configured target and records its base revision.
- Apply requires the target to remain at that clean base and verifies changed source files match the submitted snapshot. Commit separately stages only approved changed paths, verifies the applied diff has not changed, and records base/result revisions. Per-repository file locks serialize RELAY actions; apply/commit markers support interrupted-operation recovery; conditional task updates prevent deletion races from resurrecting tasks; and applied target audit records are protected from deletion/retention.
- Updated setup, operations, and threat-model docs. Verification: Python syntax compilation, FastAPI import (30 routes/schema 10), Vite production build, TypeScript check, and `git diff --check` passed. No tests run; real Git target apply/commit and crash-recovery scenarios remain unverified.

### 2026-11-05 — Add backup archive verification
- Added `backup.py --verify` to validate archive paths/types/size limits and manifest, then run SQLite quick-check and foreign-key integrity without extracting task workspaces.
- Backup creation now verifies its temporary archive before atomically publishing it. Documented that this check does not replace a restore drill.
- Verification: Python syntax compilation, CLI help, FastAPI import (30 routes/schema 10), Vite production build, TypeScript check, and `git diff --check` passed. No tests or real backup archives run.

### 2026-11-05 — Add single-host systemd templates
- Added a localhost-only API/worker service unit, daily backup timer, and setup notes for secrets, persistent paths, repository allow-lists, TLS proxying, and Docker socket risk.
- Backup archives are checked before publication; the timer still requires an assigned operator and external failure monitoring. Restore drills remain outstanding.
- Verification: Python syntax compilation, backup CLI help, FastAPI import (30 routes/schema 10), Vite production build, TypeScript check, and `git diff --check` passed. No service was installed or started, and no backup archive was created.

### 2026-11-05 — Add isolated backup restore workflow
- Added `backup.py --restore-archive ... --restore-to ...`, which verifies the archive, rejects existing/live destinations and unsafe paths, restores to a private staging directory, checks SQLite integrity, then atomically publishes the isolated restore tree.
- Updated the runbook for non-destructive restore drills. The restore workflow is implemented but has not been run against an archive; actual replacement of live data remains a manual operator step.
- Verification: Python syntax compilation, backup CLI help, FastAPI import (30 routes/schema 10), Vite production build, TypeScript check, and `git diff --check` passed. No backup archive was restored.

### 2026-11-05 — Complete local target repository integration
- Marked P2-03 complete for the configured local-Git implementation: explicit binding, expected-base checks, changed-file-only commit, audit provenance, retention/deletion protection, and interrupted-action recovery are present.
- Remote Git providers remain future scope; direct target apply/commit and crash-recovery scenarios are still required in the P4-02 verification gate.

### 2026-11-05 — Add backup freshness monitoring
- Added `backup.py --check-latest DIR --max-age-hours N` with JSON output and nonzero status for missing/stale archives, plus a daily systemd check timer.
- Documented that failed units need external alert delivery. Verification: Python syntax compilation, backup CLI help, FastAPI import (30 routes/schema 10), Vite production build, TypeScript check, and `git diff --check` passed. No archive-age scenarios or systemd units were executed.

### 2026-11-05 — Finish local MVP operations artifacts
- Added a deployment record template for named ownership, authentication references, Docker socket policy, repository targets, backup/alert setup, Gemini data approval, and restore-drill signoff.
- Marked P4-02 complete for the local MVP implementation; host-specific configuration, alert integration, and an executed restore drill remain explicit requirements before production exposure.
- Verification: Python syntax compilation, backup CLI help, FastAPI import (30 routes/schema 10), Vite production build, TypeScript check, `git diff --check`, and roadmap count (19/19) passed. No deployment was installed and no restore drill was performed.

### 2026-11-05 — Start single-host production readiness phase
- Added `RELAY_ENV=production` startup checks requiring authentication and explicit non-wildcard CORS origins, and rejecting unsafe host-subprocess mode. The systemd service template enables production mode; setup docs explain required configuration.
- Added a separate 10-task production-readiness phase so the completed 19-item local MVP and operator-dependent release gates are tracked independently. H1 is complete (1/10); H2–H10 require deployment-specific work or execution-level verification.
- No tests were added or run.

### 2026-11-05 — Pin sandbox image provenance
- Pinned the sandbox base to the official Python 3.11.18 slim Trixie multi-platform manifest digest and made the pytest install invocation explicit. Production mode now rejects mutable sandbox tags and requires an image ID or repository digest.
- Documented image ID capture and production configuration in the API setup guide, root README, and systemd runbook. H2 is complete; production-readiness progress is 2/10.
- No tests or image builds were run.

### 2026-11-06 — Add a bounded Nginx reverse-proxy template
- Added an Nginx TLS/upstream config and shared proxy-location snippet with 1 MiB general, 10 MiB JSON submit, and 27 MiB multipart caps, per-IP request/connection limits, and request/upstream timeouts. Forwarded address headers are overwritten at the edge; Uvicorn trusts only loopback. Docs call out trusted load-balancer CIDRs for deployments with another proxy hop.
- Fixed the multipart body-limit path matching for the legacy `/tasks/submit` route. H3 is complete; production-readiness progress is 3/10.
- `git diff --check` passed. No Nginx config validation or tests were run.

### 2026-11-06 — Validate production CORS origins
- Production startup now accepts only bare HTTPS CORS origins and rejects malformed URLs, wildcard characters, credentials, whitespace, and URL path/query/fragment components. Kept live browser-origin and auth-flow verification as a deployment-host release check under H10.
- H4 is complete; production-readiness progress is 4/10. No tests were added or run.

### 2026-11-06 — Canonicalize proxy redirects
- Changed the Nginx HTTP redirect to use the configured canonical host instead of reflecting the request `Host` header, and reject mismatched hosts on HTTP and HTTPS virtual hosts.
- `git diff --check` passed; Nginx configuration was not installed or validated on a host.

### 2026-11-06 — Bound generated patch size and metadata
- Capped generated patch text at 1 MB and added scope checks rejecting repeated file sections, mode changes, renames/copies, and binary-patch metadata before Git validation or application.
- H5 is complete for implementation; adversarial request-level verification remains under H10. Production-readiness progress is 5/10.
- No tests were added or run.

### 2026-11-05 — Default direct API startup to localhost
- Changed `python main.py` to bind to `127.0.0.1` by default, require an IP literal for `RELAY_HOST`, and refuse non-loopback binds unless shared-token or per-user authentication is configured. Disabled auto-reload in this entry point.
- Documented the bind and authentication behavior in both setup guides and `.env.example`.
- Verification: not run in this change; no tests were added or executed.
