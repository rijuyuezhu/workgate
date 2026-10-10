# Audit log

`workgate` records bounded server and tool activity for the owner to review. **Audit does not mask content**: commands, script source, URLs, page text, form inputs, stdout/stderr, tool results, errors, and credentials can appear in retained records. Treat the entire audit directory and exports as sensitive.

## Review activity

The browser **Audit** panel are the easiest way to filter recent activity and inspect an entry.

From an MCP client, use `audit_tail(task_id=...)` for task-wide history, `audit_tail(session_id=...)` for one concrete execution session, or pass both to request their intersection. Neither identity selects or rebinds execution. The default response is a bounded recent list; use filters to narrow it rather than requesting a large history.

To retrieve a retained full value, request one specific entry with `include_full_payloads=true`. This requires the additional `audit:full` scope. Regular list/detail views remain bounded; size-based truncation and omission are explicit.

## Storage location

By default, local audit state is under:

```text
${XDG_STATE_HOME:-~/.local/state}/workgate/audit_log/
```

This is the control-side Linux default; macOS and Windows use their native Workgate state location. An explicit `state_dir` changes the root. Executors keep their own audit state under their executor-private state root, while the shared UI/tool flow selects the relevant session/executor history.

Do not publish this directory or attach it to public bug reports. It may contain credential values and third-party data.

## Retention and limits

Audit retention, per-entry limits, and optional retained payloads are configurable. Use the generated [Configuration reference](../reference/configuration.md) instead of copying default values into deployment notes.

Hot Audit events are kept in the private `audit.jsonl` file. When its budget or the live payload quota requires eviction, complete tool-call units are first copied to compressed files in `audit_log/archives/`. These cold files participate in the **same** `audit_tail` and WebUI queries and entry details, including session/task filters and ended sessions. There is no separate archive search command or secondary index: the validated archive directory is the recoverable manifest. Damaged archives are ignored during reads and reconciled on later retention sweeps.

`max_audit_archive_bytes` limits **both** compressed archive disk usage and decoded history to be scanned; the oldest segments are pruned first. Set it to `0` to disable cold retention. If one eviction unit exceeds the entire archive budget, it cannot be retained there. Archives keep references to large Audit payloads in the existing private payload store, rather than duplicating them. Historical records remain queryable when those objects expire or are pruned, but full value recovery can then report an unavailable reference. Full values are only materialized for a selected entry with `audit:full`.

Reduce retention or disable retained payloads when the ability to inspect full values is not worth the storage and privacy cost.

## Permissions

Listing audit summaries requires `audit:read`. Full retained payloads require `audit:full`, and details of protected operations may also require the scopes used by those operations. A valid session that lacks a scope receives a forbidden response rather than being signed out.

## Troubleshooting

- **No entries appear:** confirm auditing is enabled, select the correct task or session, and widen the time or event filters.
- **A detail is unavailable:** the payload may not have been retained, may have expired, or the current OAuth session may lack permission.
- **The audit directory is growing too large:** lower retention and payload limits in configuration.
- **You need to report a bug:** export only the smallest relevant entries and review them manually for secrets and project content.

For security guarantees and limitations, see [Security](../security.md). Contributors working on audit storage or event contracts should use [Development](../development.md) and the source tests as the canonical implementation reference.
