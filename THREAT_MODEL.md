# RELAY AI Threat Model

**Scope:** current single-host FastAPI/React deployment using SQLite, Gemini, and Docker-based test execution. This describes the implementation as it exists, not a production security certification. Review this document whenever the trust boundaries, repository workflow, authentication model, or sandbox changes.

## Security objectives and assets

- Protect uploaded source code, bug reports, generated patches, task history, and Git workspaces from unauthorized access or disclosure.
- Keep Gemini credentials, bearer tokens, host credentials, and unrelated host files out of repository execution.
- Prevent untrusted archives, model output, and test code from escaping task path and patch limits or silently changing a user's original repository.
- Preserve task ownership, human approval, append-only event/action history, and durable recovery state.
- Keep service availability and resource use within the limits of the configured single-host deployment.

## Trust boundaries

1. **Browser to API:** browser inputs and bearer tokens cross an HTTP boundary. CORS limits browser origins; CORS is not authentication. The API supports static per-user bearer-token mappings with optional viewer/operator roles, a shared token, or unauthenticated local-operator mode.
2. **Upload to API and database:** report text, file paths, file contents, ZIP metadata, and compressed data are untrusted. Validated task source and results are stored in SQLite and task workspaces.
3. **API/worker to Gemini:** report and submitted source context are sent to the configured Gemini provider. Provider output is untrusted and validated against schemas and patch-scope rules. The provider's retention and account controls are outside this repository.
4. **Worker to sandbox:** the worker stages a source snapshot and patch, then starts the configured prebuilt Docker image. The Docker daemon is a privileged host boundary; access to the Docker CLI/daemon must be restricted to the service operator.
5. **Service storage to backup destination:** database snapshots and task workspaces contain user source and generated artifacts. Backup files must be treated as sensitive data.
6. **Task workspace to configured repository:** optional operator-configured local Git targets cross from task storage into a real working tree. Target IDs resolve only through server-side configuration; raw request paths are not accepted. Apply requires explicit approval, a clean recorded base revision, and matching changed-file source; commit is separately authorized and stages only the changed files. Remote repository adapters and per-target user ACLs are not implemented.

## Threat scenarios and current controls

| Threat | Current controls | Residual risk / required deployment action |
|---|---|---|
| Unauthenticated user reads or changes task data | Owner-scoped task reads/actions; optional unique per-user bearer map; optional viewer/operator roles; shared-token mode; per-client rate limits | Authentication is optional and static-token based. Local mode grants access to all local tasks. The role map is optional and unset maps preserve operator permissions for compatibility. Require unique tokens, explicit roles, TLS, and network restrictions before remote use; add an identity provider before broad multi-user deployment. |
| Cross-user task ID guessing or excess operator privilege | Ownership checks on detail, event, action, patch, delete, and list endpoints; viewer role is restricted to safe HTTP methods; unauthorized/missing records return not-found behavior | Tokens map directly to configured names and have no built-in expiry, identity provider, or self-service rotation. Protect configuration and rotate tokens operationally. |
| Malicious ZIP traversal, unsafe special entry, duplicate-path, or expansion bomb | Upload and expanded-size caps, file-count/member limits, portable NFC path validation, case-folded duplicate and file/directory collision rejection, regular-file/directory-only ZIP entry types, bounded per-member reads, blocked `.git` paths | API ingress body buffering and proxy limits must be configured at the deployment edge too. Keep application and reverse-proxy limits aligned; hostile ZIP fixtures still need request-level verification. |
| Oversized request exhausts API memory before validation | ASGI receive middleware caps JSON bodies at 10 MiB, multipart uploads at 26 MiB, and other write bodies at 1 MiB before FastAPI parses them; schema limits further constrain file count, per-file bytes, total UTF-8 bytes, path length/depth, and model context | The ASGI server and any upstream proxy may buffer data before middleware sees it. Configure matching or lower ingress limits and connection limits at the reverse proxy; the limits are fixed constants rather than operator-configurable settings. |
| Prompt injection or malformed model output changes files outside scope | Structured Pydantic output, patch header/scope checks, existing-source-file allowlist, changed-file cap, human review, approval gate requires passing tests and approving reviewer | Model judgment is not a security boundary; continue to treat source, prompts, diffs, and verdicts as untrusted. Keep the human approval gate mandatory. |
| Repository tests access host secrets or network | Docker container uses `--network none`, read-only root/source mount, non-root user, dropped capabilities, no-new-privileges, PID/CPU/memory/tmpfs limits, bounded execution time/output; image is prebuilt and never pulled at runtime; default is fail-closed | Docker daemon access is highly privileged and Docker hardening depends on host configuration. Confirm the image is trusted and pinned; verify isolation on the actual deployment host. `RELAY_ALLOW_UNSAFE_SUBPROCESS=true` executes trusted code on the host and must never be enabled for untrusted uploads. |
| Stale worker overwrites retry progress | SQLite attempt-number fencing for progress writes, lease renewal, and job completion; append-only events and job-attempt records | Multi-process fault-injection and restart-recovery behavior is not yet verified. SQLite remains a single-host store, not a multi-host queue guarantee. |
| Unauthorized or accidental repository mutation | Apply requires explicit approval and validated patch; default remains a task-owned copy; optional local targets are operator allow-listed, require a clean recorded base and changed-file snapshot match, and commit separately stages only approved changed files; task audit is retained after target apply | Tests run against the submitted snapshot; only changed files are compared with the target base, so operators must submit the matching repository snapshot. Remote repository adapters and per-target authorization remain absent. |
| Source or credentials leak through logs/errors | Best-effort redaction removes configured secret environment values, configured per-user bearer tokens, common Google/GitHub/OpenAI/Slack/AWS credential patterns, and private-key blocks from structured logs, task errors, and sandbox output; API keys are not deliberately copied into the sandbox environment; backups omit deployment secrets | Pattern redaction cannot recognize every credential format and may miss transformed, encoded, or split secrets. Inspect logs and API errors before sharing. Gemini receives submitted source; obtain the required data-use approval for the configured account. |
| Backup theft or retention beyond policy | Backup archive mode is restricted; owner deletion cascades task data; operator-managed terminal-task purge is dry-run-first; no automatic expiry by default; runbook documents encryption/access control | Archive contents are not encrypted by the tool. Store them in an encrypted location, define retention, schedule the purge/backup tools, and perform restore drills. There is no automatic purge or backup scheduler. |
| Denial of service through submissions/actions | Fixed-window rate limits, bounded accepted task context, worker attempt limits, sandbox time/output/resource caps | Limits are per client address and local SQLite only; no global quota, concurrent-job cap, upload-edge limit, or load-tested capacity target is established. |

## Deployment requirements before remote exposure

1. Put the API behind TLS and a reverse proxy with request-body, connection, and request-time limits. Keep it off the public network until authentication is mandatory.
   The `python main.py` entry point binds to loopback by default and refuses a non-loopback `RELAY_HOST` unless shared-token or per-user authentication is configured. When launching Uvicorn directly, explicitly bind to loopback for local use and configure authentication before selecting a network interface.
2. Configure unique per-user tokens from a secret manager, explicit production CORS origins, restrictive filesystem permissions, and a dedicated service account. Do not use local-operator or shared-token mode for a multi-user service.
3. Restrict Docker daemon access to the trusted service host/operator. Use a maintained, pinned sandbox image and verify its runtime restrictions on the target host. Keep unsafe subprocess mode disabled.
4. Approve Gemini data handling for submitted source, define source retention, encrypt backups at rest, and restrict log/backup readers.
5. Use one service instance with SQLite only while accepting its single-host limits. Select a server database and queue before multi-host deployment.
6. Complete concurrency/recovery, hostile-input, sandbox isolation, security, accessibility, and load verification before describing the deployment as production-ready.

## Provisional service objectives

These are initial targets for an operator-managed single-host pilot, not measured guarantees. Review them after collecting at least 30 days of production-like traffic and publish the measured SLI separately.

| SLI | Provisional objective | Measurement / caveat |
|---|---|---|
| API availability | At least 99.0% successful health/readiness checks per calendar month, excluding announced maintenance | Probe `/health` and `/ready` separately; readiness intentionally fails when Gemini or the sandbox image is unavailable. No external probe is configured by this repository. |
| Queue wait | 95% of worker attempts claimed within 30 seconds while the worker is healthy | `relay_job_queue_wait_ms` histogram; traffic and single-worker saturation affect the result. |
| Pipeline completion | 90% of mature jobs reach review or a classified terminal failure within 15 minutes | `/metrics` exposes `relay_pipeline_completion_sli_ratio` and rolling 30-day cohort counts from task/job timestamps. The ratio excludes cancellations and jobs still within their first 15 minutes; overdue in-progress jobs count as late. It depends on provider latency/quota and repository size; owner-deleted tasks disappear from the measured cohort, so long-term SLO reporting needs an external durable metrics store. |
| Recovery point | No more than 24 hours of task data loss | Requires an operator-scheduled daily backup; no scheduler is included. |
| Recovery time | Restore core service and verify task/event access within 4 hours | Manual runbook target; restore drill not yet evidenced. |

## Ownership and incident roles

The deployment owner is accountable for token issuance/rotation, TLS and network exposure, Docker host security, Gemini data-use approval, backup encryption/retention, restore drills, and incident response. Assign a named primary and backup operator, alert destination, and escalation path in the deployment record before remote service. This repository does not contain organization-specific owners or an on-call integration.

For incidents: restrict inbound access first; preserve relevant logs and a protected database/workspace backup; rotate exposed provider or bearer credentials; inspect task/action history; rebuild from a trusted image; and resume only after `/health`, `/ready`, task ownership, and recovery state are confirmed.
