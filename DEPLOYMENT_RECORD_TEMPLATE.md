# RELAY AI Deployment Record

Copy this file into the deployment's protected operations folder and fill it in without recording secret values. Keep only secret-manager references here.

## Service and people

- Environment and hostname:
- Deployment/repository revision:
- Primary service owner (name and contact):
- Backup service owner (name and contact):
- Security/escalation contact:
- Change window and rollback decision owner:

## Runtime and network

- Service account:
- API listen address:
- TLS reverse proxy and public URL:
- Request/body and connection limits at the proxy:
- Configured CORS origins:
- Authentication mode and secret-manager reference:
- Docker sandbox image digest:
- Docker socket access policy and reviewer:
- `RELAY_REPOSITORIES_JSON` target IDs and approved roots (no credentials):

## Data and recovery

- `RELAY_DB_PATH`:
- `RELAY_APPLIED_ROOT`:
- Backup destination and encryption control:
- Backup schedule and maximum age:
- Backup freshness alert destination:
- Backup alert owner:
- Last isolated restore drill date, archive, result, and reviewer:
- Recovery point/time objectives accepted by:

## Data handling and operations

- Gemini source-data approval/reference:
- Task/source retention policy and purge operator:
- Log/backup access group:
- `/health` and `/ready` probe owner:
- Incident response contact and escalation path:
- Known exceptions and expiry date:
