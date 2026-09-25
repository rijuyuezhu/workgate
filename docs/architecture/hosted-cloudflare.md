# Hosted / Cloudflare

This page records the hosted-control architecture and the constraints measured
against Cloudflare Workers. The maintained `deploy/cloudflare/` adapter provides
a deployable single-user subset; hosted support remains an adapter to the same
control/executor architecture.

For deployment and pairing instructions, see
[Cloudflare hosted deployment](../getting-started/cloudflare-hosted.md).

The evaluated platform is Cloudflare Workers + one SQLite-backed Durable Object
per personal Workgate deployment. The observations below were checked against
Cloudflare's public documentation in September 2026.

## Feasible core

Cloudflare Python Workers support FastAPI/ASGI, and Python Durable Objects can
use the synchronous SQLite API exposed as `ctx.storage.sql`. That is enough to
reuse Workgate's existing synchronous `StateStore` contract without converting
the VPS/standalone core to async KV, Redis, D1, or a durable command queue.
Python Workers do not provide functional `threading`/`multiprocessing`; the
hosted composition therefore uses Durable Object single-threaded execution for
its synchronous control-state critical sections and does not construct Python
thread locks.

Workgate therefore provides two small provider-facing seams:

- `DurableObjectSqlStateStore` stores logical JSON state in the SQLite database
  owned by one stateful actor;
- `HostedControlActorCore` composes the durable control facts with the normal
  executor transport, pairing service, session coordinator, and ephemeral
  stream rendezvous state.

A provider adapter can construct them roughly as follows:

```python
from workgate.hosted import (
    DurableObjectSqlStateStore,
    build_hosted_control_actor_core,
)

state_store = DurableObjectSqlStateStore(ctx.storage.sql)
actor = build_hosted_control_actor_core(settings, state_store=state_store)
actor.start()
```

The provider-specific Worker/Durable-Object routing layer lives under
`deploy/cloudflare/`, while provider-neutral HTTP/MCP routing remains under
`workgate.hosted`. Importing Workgate does not require `workers-py` or a
Cloudflare account. The deployment stages a reduced source closure rather than
installing the published Workgate wheel directly into Python Workers.
Workgate's normal package metadata still includes full VPS/desktop dependencies
such as ordinary `httpx`, `uvicorn[standard]`, and `websockets`, while Python
Workers only support packages available to Pyodide/PyEmscripten. The maintained
deployment therefore stages a deliberately smaller source/dependency closure
instead of reshaping the default VPS/standalone dependency graph.

The hosted MCP wire adapter is likewise dependency-light rather than importing
the full FastMCP server into Python Workers. It implements the MCP
`2026-07-28` stateless discovery/tool-call subset used by this hosted product,
including per-request protocol metadata and routing headers, and retains the
handshake-era revisions generated from Workgate's current MCP v1 dependency.
It does not claim unrelated modern protocol surfaces such as subscriptions or
multi-round-trip tool flows. Tool schemas themselves remain generated from the
canonical Workgate tool catalog so the hosted surface cannot silently drift.

## Reconstruction semantics

Durable Objects may be evicted or restarted, which discards Python in-memory
state. That matches the intended Workgate failure boundary rather than fighting
it:

| State | Across actor reconstruction |
| --- | --- |
| paired executor ID + credential verifier | survives |
| control-side session lifecycle facts | survives |
| ordinary executor command queue/Futures | discarded |
| executor online/presence/inventory | discarded; rebuilt by hello |
| unfinished pairing attempts/plaintext delivery | discarded; executor may retry pairing |
| terminal rendezvous/browser tokens | discarded |

The hosted reconstruction tests exercise this boundary with a fresh actor over
the same SQLite database. No ordinary command is replayed after reconstruction.

## What remains unavailable in the hosted adapter

Some current control features still intentionally own local filesystem state.
The first hosted adapter does not paper over those differences:

- download snapshots and public file-link payloads use local `PayloadStore`;
- session-copy staging/payload retention uses local files;
- canonical audit JSONL/full-payload storage is file-backed;
- managed-job leases/recovery still contain host-process/file assumptions;
- the hosted MCP endpoint currently uses a static owner bearer rather than the
  VPS OAuth browser flow; Human UI routing is not present;
- terminal WebSocket routes are not yet adapted to the Durable Object
  hibernation event/attachment API.

If hosted support is taken beyond feasibility, large immutable payloads may get
an optional R2-style adapter. That must remain feature-owned storage; it must
not become a generic control database. Unsupported features should stay
explicitly unavailable until their storage/lifecycle semantics are correct.

## Long-poll feasibility and cost shape

The executor protocol currently uses a 25-second delivery poll and a 15-second
heartbeat by default. Cloudflare currently documents no hard wall-time limit for
Durable Object HTTP/RPC calls while the caller remains connected, so the poll
duration itself is viable.

The cost shape matters more than the duration limit. A continuously connected
executor produces roughly:

- 3,456 delivery polls/day at one 25-second poll after another;
- 5,760 heartbeats/day at a 15-second interval;
- about 9,216 baseline actor requests/day before results, reconnects, browser
  traffic, and owner operations.

An in-flight long poll keeps the Durable Object active, so a continuously
polling executor leaves little opportunity to hibernate. At Cloudflare's current
128 MB duration accounting, one actor active for a full day is approximately
11,059 GB-s/day; 30 days is approximately 331,776 GB-s. As of September 2026,
Cloudflare documents 13,000 GB-s/day on the Free plan and 400,000 GB-s/month in
the Paid-plan included allocation. A continuously active single actor is
therefore within the current Free-plan duration allocation but leaves limited
headroom. These are estimates from Cloudflare's documented limits and billing
model, not a Workgate pricing guarantee; Cloudflare limits/pricing can change
and other traffic adds usage.

This means the current HTTP long-poll protocol is technically feasible for a
personal hosted actor, but it is not hibernation-efficient. The core executor
transport is not replaced with a provider-specific durable queue merely to
optimize one hosting provider.

## Terminal streams

Cloudflare Durable Objects can accept hibernatable inbound WebSockets and keep
those sockets connected while actor memory is discarded. Both Workgate terminal
participants connect *to* control, so the platform primitive is promising.

However, Workgate's current terminal stream rendezvous is intentionally
process-local. Correct hibernation would require a provider adapter to bind each
accepted socket to durable/serialized attachment metadata and reconstruct the
relay relationship after wake-up. The generic ASGI routes do not currently do
that, so terminal streaming is explicitly unsupported by the feasibility
adapter rather than silently pinning or durably replaying stream state.

## Storage constraints

`DurableObjectSqlStateStore` uses one table with logical Workgate paths as keys
and deterministic JSON values. It intentionally implements the existing
synchronous `StateStore` API. Transaction scopes contain no `await`; the
Durable Object storage contract documents that synchronous storage reads and
writes without an intervening `await` are automatically atomic and concurrent
events do not interleave that sequence. This is the hosted equivalent of the
file implementation's transaction lock.

Cloudflare's current limits page lists a 2 MB maximum SQL row/string size, up to
10 GB per SQLite-backed Durable Object, and a 5 GB account-wide SQLite Durable
Object storage cap on Workers Free. A separate Cloudflare FAQ still describes a
1 GB Free per-object ceiling, so the precise Free-plan ceiling should be
rechecked before deployment. The hosted state adapter is intended for small
restart-critical registries either way, not arbitrary payload blobs.

## Platform references

- [Python Workers](https://developers.cloudflare.com/workers/languages/python/)
- [FastAPI on Python Workers](https://developers.cloudflare.com/workers/languages/python/packages/fastapi/)
- [SQLite-backed Durable Object storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)
- [Durable Object lifecycle](https://developers.cloudflare.com/durable-objects/concepts/durable-object-lifecycle/)
- [Durable Object limits](https://developers.cloudflare.com/durable-objects/platform/limits/)
- [Durable Object pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)
- [WebSocket hibernation](https://developers.cloudflare.com/durable-objects/best-practices/websockets/)
