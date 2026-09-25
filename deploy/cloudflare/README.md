# Workgate on Cloudflare Workers

This directory contains the maintained single-user Cloudflare hosted adapter for
Workgate. It runs one logical Workgate control actor in one SQLite-backed
Durable Object and keeps all machine execution on paired executors.

The hosted adapter is intentionally narrower than the VPS product. It supports
executor pairing, executor v1 transport, explicit sessions, and the machine MCP
tools listed by the hosted tool manifest. Features that still depend on
control-local files or provider-specific streaming are not advertised.

## Install and stage

```bash
cd deploy/cloudflare
npm ci
uv sync --locked
python prepare.py
```

`prepare.py` copies only the dependency-light Workgate hosted closure into
`src/workgate/`. That directory is generated and ignored by Git. Re-running
`prepare.py` updates files in place, so it is safe while `pywrangler dev`
is watching the tree.

For local development:

```bash
python prepare.py
uv run pywrangler dev
```

After changing provider-neutral code under `src/workgate/hosted/` or another
module in the staged closure, rerun `python prepare.py`; pywrangler will reload
only the changed staged files.

## Owner token

Generate a high-entropy token and retain it privately:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Configure it as a Cloudflare Worker secret:

```bash
uv run pywrangler secret put WORKGATE_OWNER_TOKEN
```

Do not put this value in `wrangler.jsonc`, Git, logs, or executor profiles.

## Deploy

```bash
python prepare.py
uv run pywrangler deploy
```

The Worker routes requests to the Durable Object named `personal`, so one
deployment represents one personal Workgate control actor.

If the Worker is reachable through more than one hostname (for example a
`workers.dev` hostname plus a custom domain), configure the canonical public
origin explicitly in `wrangler.jsonc`:

```jsonc
{
  // ...the existing Workgate configuration...
  "vars": {
    "WORKGATE_BASE_URL": "https://workgate.example.com"
  }
}
```

That value is not secret. It is used for the pairing verification URL. When it
is omitted, Workgate infers the origin from the first request that constructs
the Durable Object actor.

## Pair an executor

```bash
workgate executor connect https://<your-worker-host> \
  --name laptop \
  --workspace-root /path/to/workspace
```

Open the printed `/pair` URL, enter the owner token and short user code, and
approve the request. Then run the executor normally with `workgate executor run`.

## MCP

Use `https://<your-worker-host>/mcp` with:

```text
Authorization: Bearer <WORKGATE_OWNER_TOKEN>
```

The endpoint implements stateless MCP Streamable HTTP POST requests. A normal
client can discover/list tools, call `session_start`, and invoke the advertised
session-bound machine tools. The hosted wire adapter implements the
`2026-07-28` stateless discovery/tool-call subset needed by this surface,
including per-request protocol/routing metadata, while retaining the
handshake-era MCP revisions supported by Workgate's current MCP v1 dependency.
It does not claim unrelated 2026 protocol features such as subscriptions or
multi-round-trip tool flows. The first hosted adapter uses a static owner bearer
rather than Workgate's VPS browser OAuth flow.

Owner-authenticated routes are rejected at the stateless Worker edge before
they wake the Durable Object; the Durable Object repeats the bearer check before
dispatch. Executor routes continue to authenticate with executor credentials
inside the control actor.

## Other routes

- `GET /healthz` — public liveness check.
- `GET /pair` — pairing page. Review, approval/denial, and explicit existing
  credential replacement require the owner bearer.
- `GET /status` — owner-authenticated executor/session summary.
- `POST /executor/v1/...` — normal Workgate executor protocol.

## Explicitly unavailable

The hosted adapter currently does not advertise or emulate downloads/public file
links, `session_copy`, canonical file-backed audit history, managed control jobs,
control-owned todos, Human UI, browser terminal/WebSocket streaming, or
control-owned HTTP/OAuth Agent Bridge integrations. Background `bash`/Python job
mode (`async_=true` without `pty=true`) fails closed because the hosted job
companion is not present. PTY mode remains executor-owned and supported.
