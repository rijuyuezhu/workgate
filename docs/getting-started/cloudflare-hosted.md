# Cloudflare hosted deployment

Workgate includes a deployable single-user Cloudflare adapter under
`deploy/cloudflare/`. It maps one Workgate deployment to one SQLite-backed
Durable Object and keeps machine execution on ordinary paired executors. The
Worker never becomes a machine executor.

## Supported surface

The current adapter provides executor pairing and v1 transport, a simple owner
pairing page, owner-authenticated status diagnostics, stateless Streamable HTTP
MCP, and explicit sessions plus the advertised executor-backed file/search/shell
tools. Executor trust and session facts are persisted in Durable Object SQLite.

Unsupported control-local features are omitted from MCP discovery instead of
being emulated. Downloads/public links, `session_copy`, control-managed jobs,
todos, canonical file-backed audit, Human UI, browser terminal streaming, and
control-owned Agent Bridge HTTP/OAuth integrations are not currently available.
Background `bash`/Python job mode (`async_=true` without `pty=true`) fails
explicitly; PTY mode remains executor-owned and supported.

## Authentication

The first hosted adapter uses one high-entropy owner bearer stored as the
Cloudflare secret `WORKGATE_OWNER_TOKEN`. That bearer protects `/mcp`, `/status`,
and pairing decisions. Executor routes continue to use their own credentials.
Clients that require OAuth discovery/authorization instead of a configured
bearer are not supported yet.

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

If the same Worker is reachable through multiple hostnames, set the non-secret
`WORKGATE_BASE_URL` Wrangler variable to the canonical public origin (for
example `https://workgate.example.com`). Otherwise the adapter infers the
pairing verification origin from the first request that constructs the Durable
Object actor.

The deployment-specific README under `deploy/cloudflare/` has the exact pairing
and MCP client configuration.

## Architecture notes

`prepare.py` stages only the dependency-light hosted closure under
`deploy/cloudflare/src/workgate/`; that generated tree is ignored by Git. The
normal Workgate wheel and its VPS/desktop dependency graph are not installed
inside the Worker. Durable trust and session facts survive actor reconstruction;
ordinary command queues, Futures, presence, unfinished pairing attempts, and
live stream rendezvous remain ephemeral.

For storage and lifecycle detail, see
[Hosted / Cloudflare architecture](../architecture/hosted-cloudflare.md).
