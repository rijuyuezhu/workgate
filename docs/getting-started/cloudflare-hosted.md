# Cloudflare hosted deployment

This is the serverless control option from [Choose a deployment](deployment.md).
It is different from putting a self-hosted control behind
[Cloudflare Tunnel](cloudflare-tunnel.md).

Workgate ships a single-user Cloudflare adapter under `deploy/cloudflare/`.
Machine execution still happens on ordinary paired executors.

## Current feature surface

The hosted adapter supports executor pairing/transport, owner-authenticated
status, stateless MCP, explicit sessions, and the advertised executor-backed
file/search/shell tools.

It does not currently provide the full self-hosted control surface. Human UI,
browser terminal streaming, public file links, `session_copy`, control-managed
jobs, Todos, file-backed Audit, and control-owned Agent Bridge integrations are
not available. Unsupported tools are omitted from discovery rather than emulated.

## Authentication

Hosted control uses one high-entropy owner bearer stored as the Cloudflare
secret `WORKGATE_OWNER_TOKEN`. Executor routes continue to use executor
credentials. Clients that require the self-hosted OAuth flow are not supported
by this adapter.

## Deploy

```bash
cd deploy/cloudflare
npm ci
uv sync --locked
python prepare.py
python -c 'import secrets; print(secrets.token_urlsafe(32))'
uv run pywrangler secret put WORKGATE_OWNER_TOKEN
uv run pywrangler deploy
```

If the Worker is reachable through multiple hostnames, set
`WORKGATE_BASE_URL` to the canonical public origin.

The deployment-specific README under `deploy/cloudflare/` contains the exact
pairing and MCP client configuration. For provider/storage details, see
[Hosted / Cloudflare architecture](../architecture/hosted-cloudflare.md).
