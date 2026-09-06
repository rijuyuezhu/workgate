# Control/executor architecture

This document is the canonical architecture contract for the control/executor
refactor tracked by [#123](https://github.com/rijuyuezhu/workgate/issues/123).
The issue remains the detailed design record and implementation checklist; this
page intentionally keeps only contracts that later code must preserve.

## Core invariants

Workgate has two roles:

- **control** owns owner identity, public APIs/UI, executor trust and presence,
  public session identity/binding, orchestration, control-owned integrations,
  payload/share artifacts, audit, and restart-critical product state;
- **executor** owns machine reality: workspace paths, files/search, shell and
  Python execution, PTYs, persistent shells, background jobs, grounding state,
  machine-local integrations, and executor-local policy/secrets.

The boundary is about machine authority, not whether a process may use its own
private filesystem. Control may read/write its own config, state, data, runtime,
payload, audit, and secret storage. It must not gain arbitrary executor workspace,
shell, PTY, or process authority.

Standalone mode still runs two OS processes and uses the real loopback executor
protocol. If the executor dies, machine-facing tools become unavailable; control
must never fall back to direct host execution.

A personal deployment assumes one owner, one control deployment, and usually
one to four executors. Do not introduce active-active coordination, a broker,
generic workflow engine, or distributed exactly-once machinery for this model.

## Packages and dependency direction

The target roots are:

```text
workgate.protocol   dependency-light shared wire/data contracts
workgate.control    public/control composition and authority
workgate.executor   machine-facing composition and authority
```

`workgate.protocol` must not depend on filesystem persistence, HTTP server
frameworks, systemd/deployment code, Cloudflare/provider SDKs, shell/PTY code,
or control/executor implementation modules. Protocol v1 models live there; the
runtime mechanisms that implement them belong to the owning process.

Control and executor have distinct resolved configuration authority. Control
owns public bind/base URL, auth/OAuth/UI, pairing/presence, command admission,
and control persistence policy. Executor owns `workspace_root`, path/command
policy, machine limits, local integrations, and executor state/data/runtime.
Standalone may accept one user-facing config file, but its supervisor resolves
separate child configurations before launch.

Relative session workdirs are always resolved against the executor's fixed
configured `workspace_root`, never against a session's previous cwd.

## Identity, credentials, and pairing

`executor_id`, `session_id`, and `command_id` are opaque URL-safe identifiers.
New identifiers contain at least 128 bits of cryptographic randomness. Executor
names are display labels, never authentication identity.

An executor stores one long-lived high-entropy bearer credential. Durable
control trust state stores only a verifier/hash. Credential validity does not
expire because of `last_seen_at`, suspend, reboot, or long shutdown; it ends only
through explicit revoke/replacement, lost/reset trust state, lost local profile,
or a deliberate incompatible trust migration.

Pairing uses a Workgate-specific device-code-style flow:

```text
executor -> POST /executor/v1/pair/start
         <- high-entropy device_code + short user_code + verification URI
owner    -> authenticated approve/deny
executor -> POST /executor/v1/pair/poll(device_code)
         <- executor_id + long-lived credential
```

Pending unauthenticated pairing attempts and user-code lookups are bounded and
rate-limited. Approval/denial and credential generation are one-shot, but
credential **delivery** is retryable: while the short-lived process-local
pairing attempt remains alive, the same `device_code` receives the same
credential. Control never durably stores that plaintext credential.

The executor atomically persists `control_url`, `executor_id`, and credential in
its private profile **before** its first authenticated hello. A failed profile
write sends no hello, so credential delivery can still be retried. First
successful authenticated hello or pairing expiry clears the transient delivery
copy. If control restarts after the executor persisted the credential, the
executor authenticates normally from its profile. Fresh pairing may be needed
only when credential delivery/profile persistence did not complete before
process-local delivery state was lost; this rare window does not justify durable
plaintext credential storage. A pairing client's unauthenticated
`existing_executor_id` is only a hint; owner approval is authoritative for
whether an existing executor identity is reused with a replacement credential.

Revocation/replacement is persisted before acknowledgement and fences new work:
old-bearer polls are woken/rejected, queued work is interrupted, pending callers
are interrupted, and active streams are closed. Already-offered machine effects
cannot be rolled back, and results submitted under the revoked bearer are
rejected. No credential generation counter is part of the protocol.

## Executor protocol v1

The baseline executor surface is versioned under `/executor/v1`:

```text
/pair/start
/pair/poll
/hello
/heartbeat
/poll
/result
```

After pairing, executor requests authenticate with the long-lived bearer. The
bearer determines `executor_id`; ordinary request bodies do not repeat it merely
for authentication.

### Hello inventory and presence

Hello carries a **complete** thin inventory by contract:

```text
protocol/runtime/capability metadata
session_id + resolved cwd
shell_id + session_id
job_id + session_id + status
```

Product resource caps bound inventory size. Partial/truncated inventory is not a
protocol mode: if a valid inventory cannot fit the normal request limit, fail
explicitly. This keeps absence meaningful. `session.lookup` remains a targeted
read-only reconciliation operation for ambiguous creation or cwd projection,
not pagination for hello.

Optional boot metadata is diagnostics only. It is never identity, trust,
fencing, deduplication, or command-correlation authority.

Presence uses an independent authenticated heartbeat. A saturated executor must
continue heartbeating even when command capacity is full. Blocking machine work
must not monopolize the transport/presence event loop; use the simplest existing
isolation mechanism such as a thread or owned subprocess rather than inventing a
generic worker-pool subsystem.

`last_seen_at` is diagnostic metadata, not trust expiry. It may be coalesced
rather than written to disk for every heartbeat.

### Ordinary commands

The command envelope stays small:

```json
{
  "id": "random-command-id",
  "op": "shell.run",
  "session_id": "optional-session-id",
  "args": {}
}
```

Destination executor identity comes from the authenticated poll. There is no
control epoch, executor generation, per-boot protocol identity, generic progress
field, or generic cancellation field.

Control keeps a bounded per-executor in-memory admission budget and an in-memory
pending map. The only delivery states needed are:

```text
queued -> offered -> result | abandoned/interrupted
```

`offered` is the irreversible application-level handoff boundary. Under the
same per-executor coordination used to fence trust changes, control revalidates
the poll bearer, removes one queued command, and marks it offered. From that
moment the command is possibly delivered forever and is **never requeued** because
of HTTP handler cancellation, socket failure, proxy ambiguity, or control
restart.

Before `offered`, caller cancellation/timeout removes queued work, so no executor
side effect can occur. After `offered`, cancellation means only "stop waiting";
it does not preempt, replay, or transparently retry executor work. An explicit
caller retry is a new command with a new random `command_id`.

Control permits at most one outstanding delivery poll per stable `executor_id`.
Executor also preserves one-process-per-profile local locking. Executor runs only
a bounded set of command tasks and opens another poll only when capacity exists;
long-running work therefore need not serialize unrelated commands, but the
executor never accumulates an unbounded already-offered backlog.

Result upload uses the same `command_id` and current executor bearer. Upload may
be retried after a transient network failure because it does not repeat the
machine side effect. `unknown_command` is terminal for the uploader: drop the
result and local correlation rather than retrying forever. Control restart loses
ordinary queues/Futures and never reconstructs or replays them.

## Session lifecycle

There is one shared `session_id` end-to-end:

```text
control:  session_id -> executor_id + durable product state
executor: same id    -> resolved workdir + machine state
```

There is no second worker/executor session ID and no live migration or silent
rebinding. Durable control states may remain simply:

```text
creating | active | terminating | ended
```

### Creation and ambiguous outcomes

Control persists `creating` before sending `session.create(S)`.

- admission failure or queued cancellation/timeout proves no side effect and
  removes/ends the new `creating` checkpoint;
- an explicit create failure is authoritative only after executor has ensured
  no durable session `S` remains;
- after create is `offered`, an ambiguous/lost result is never resolved by
  replaying create. Reconnect inventory or positive read-only `session.lookup(S)`
  may promote `creating -> active`; negative lookup alone does not guess failure.

Creation-unconfirmed sessions remain visible to owner/admin status. `session_end`
may operate on `creating`, persist `terminating`, and use normal idempotent
"desired absence" cleanup.

Executor must not let a later-received `session.terminate(S)` overtake an earlier
received `session.create(S)`. A tiny process-local per-session FIFO/chain for
existence mutations is sufficient; it is not durable and different sessions
remain concurrent. Negative `session.lookup` is non-authoritative and need not
join that FIFO.

### Admission and termination

Control active-session admission and transition to `terminating` linearize per
session. Admission includes registering/enqueuing the operation under the same
coordination. Multi-session admission, including `session_copy`, uses stable
session-ID order. Once `terminating` is durable, no new non-cleanup operation is
admitted.

Termination is desired absence and may be reissued idempotently. If executor is
offline, the control record remains `terminating`; force release is explicit and
must not claim remote cleanup occurred.

Retention/capacity cleanup follows the same rule: select an eligible idle
session, transition it to `terminating`, confirm executor-side absence, then mark
ended/prune. Sessions with active persistent shells/jobs remain protected. Never
drop an executor-backed binding merely to free a slot.

### Workdir changes

`session_change_cwd` is an executor-authoritative resource mutation. Under its
session/snapshot synchronization, executor:

1. resolves and validates the requested path against fixed `workspace_root`;
2. invalidates/removes old durable and cached grounding/snapshots;
3. atomically replaces the durable session cwd;
4. reports refreshed orientation.

This ordering is crash-safe: a crash before cwd replacement leaves old cwd with
no snapshots; a crash after replacement leaves new cwd with no old snapshots.
If the response is lost after commit, control does not replay the mutation; hello
inventory or explicit lookup repairs its display projection.

`session_copy` retains its existing meaning: copy one file/directory between two
**existing** session IDs. It does not create, migrate, clone, or rebind a session.
Cross-executor copy may use a feature-specific transfer checkpoint; this is not a
reason to build a generic workflow engine.

## Persistence and long-lived resources

Persist only facts the user can still care about after restart. Examples include
executor trust/revoke state, session bindings/lifecycle intent, approved OAuth
clients, persistent shells/jobs, control-owned integration config/secrets,
public-share metadata/payloads, transfer-specific checkpoints, and audit.

Keep process-local state ephemeral: presence, pairing attempts, ordinary command
queues/Futures/offered state, browser stream tokens, terminal bytes/backpressure,
and live task objects. Control restart intentionally interrupts these rather than
turning them into a database-driven command system.

Feature resources own their durability. Persistent jobs/shells and
cross-executor transfer may have resource-specific IDs/checkpoints. Ordinary RPC
does not inherit those recovery semantics.

## Terminal streams

Terminal resources live on executor; browser rendezvous lives on control. The
executor never opens an inbound Workgate terminal port.

```text
browser -> control: request attach(session, shell)
control: validate owner/session; allocate random live stream_id + browser token
control -> executor: terminal.attach(stream_id, shell)
executor -> control: outbound WSS /executor/v1/streams/<stream_id>
                     authenticated by normal executor credential
control: require stream_id live and bearer executor == expected executor
browser -> control: /stream/<stream_id> with short-lived browser token
StreamHub: pair endpoints with bounded backpressure
```

Executor needs no second stream credential: its bearer plus a random live
`stream_id` binding is sufficient for this threat model. Browser attachment is a
separate authority and keeps a short-lived, preferably one-use token.

Raw terminal bytes, resize messages, and backpressure state remain in memory and
do not enter command/checkpoint/audit bodies. Control restart drops the
attachment but not a persistent executor shell. Revoke/replacement closes active
streams for that executor.

## Integrations and secrets

Secret ownership follows integration ownership:

- network/OAuth/SaaS integrations normally run on control and keep their secrets
  there;
- stdio/local process/device/workspace integrations run on executor and may use
  explicitly provisioned executor-local private secrets;
- no automatic control-to-executor secret replication exists;
- no generic control secret-read API or arbitrary URL+credential oracle exists.

Keep abstractions demand-driven. Implement the first concrete integration or
provider directly; extract a seam only after real repeated call sites exist.
Executor operation dispatch may start as a plain `dict[str, handler]`; protocol
v1 does not require a plugin/factory framework or universal operation metadata.

## Deployment and non-goals

The first hosted target is an ordinary Linux VPS behind Caddy/nginx, with
executors making outbound HTTPS/WSS connections only. No Redis, Postgres, broker,
object store, or Kubernetes is required. Standalone is the same architecture on
loopback under a lifecycle-only supervisor.

Optional hosted/Cloudflare work comes only after VPS and standalone are stable
and must adapt to this core rather than reshape it.

Explicit non-goals include active-active control, distributed leader election,
generic durable command queues, transparent ordinary-RPC restart recovery,
distributed exactly-once execution, universal workflow orchestration, live
session/job/PTY migration, periodic executor credential rotation, and a
multi-tenant/SaaS trust model. Add such machinery only for a future concrete
requirement, not pre-emptively.
