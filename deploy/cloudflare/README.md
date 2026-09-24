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
`src/workgate/`. That directory is generated and ignored by Git.

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
client can initialize, list tools, call `session_start`, and invoke the advertised
session-bound machine tools. The first hosted adapter uses a static owner bearer
rather than Workgate's VPS browser OAuth flow.

## Other routes

- `GET /healthz` — public liveness check.
- `GET /pair` — pairing page; approval POSTs require the owner bearer.
- `GET /status` — owner-authenticated executor/session summary.
- `POST /executor/v1/...` — normal Workgate executor protocol.

## Explicitly unavailable

The hosted adapter currently does not advertise or emulate downloads/public file
links, `session_copy`, canonical file-backed audit history, managed control jobs,
control-owned todos, Human UI, browser terminal/WebSocket streaming, or
control-owned HTTP/OAuth Agent Bridge integrations. Background `bash`/Python job
mode (`async_=true` without `pty=true`) fails closed because the hosted job
companion is not present. PTY mode remains executor-owned and supported.
