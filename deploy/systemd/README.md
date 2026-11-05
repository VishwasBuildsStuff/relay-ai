# Single-host systemd templates

These units provide a localhost-only FastAPI/worker service and a daily backup for a dedicated Linux host. They are templates: replace the example paths and configure the host before enabling them.

1. Install the repository and Python environment under `/opt/relay-ai`; install Node-built static assets behind a TLS reverse proxy or serve them separately.
2. Create `/etc/relay-ai/relay-ai.env` with protected permissions (`root:relay-ai`, mode `0640`). Set at least `GEMINI_API_KEY`, `RELAY_DB_PATH=/var/lib/relay-ai/relay_tasks.sqlite3`, `RELAY_APPLIED_ROOT=/var/lib/relay-ai/applied`, explicit `RELAY_CORS_ORIGINS`, and unique `RELAY_AUTH_USERS_JSON`/`RELAY_AUTH_ROLES_JSON` values. Do not set `RELAY_ENV` in this file; the unit sets production mode, which fails startup if auth or explicit CORS configuration is missing or unsafe subprocess mode is enabled. Add `RELAY_REPOSITORIES_JSON` only for trusted local Git roots.
3. Create `/var/lib/relay-ai` and `/secure/backups/relay-ai` owned by the service account. Add each configured repository root to the service unit's `ReadWritePaths` and grant access only to those roots.
4. Install the units to `/etc/systemd/system`, then run `systemctl daemon-reload`, `systemctl enable --now relay-ai.service`, `systemctl enable --now relay-ai-backup.timer`, and `systemctl enable --now relay-ai-backup-age-check.timer`.
5. Put the API behind TLS and a request-limiting reverse proxy. The unit binds to `127.0.0.1:8000` and must not be exposed directly to an untrusted network.

The sandbox expects Docker. Giving the service account membership in the host `docker` group is root-equivalent; use a narrowly restricted Docker socket proxy or accept that host-level risk deliberately. The unit does not grant Docker access by itself. Configure and test the chosen socket path separately.

Each backup is verified before the tool atomically publishes its final filename. The age-check timer exits nonzero when no archive exists or the newest archive exceeds 26 hours; connect failed systemd units to an external alerting system. Operators must assign an owner and periodically restore into an isolated environment. Do not treat these example units as a production security certification.
