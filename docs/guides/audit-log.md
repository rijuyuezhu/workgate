# Audit log

`workgate` records server and tool activity so users can review what happened. Audit data may include project text, command input, command output, errors, and remote activity after best-effort credential redaction.

Treat the entire audit directory as sensitive.

## Review activity

The browser and OpenTUI **Audit** panels are the easiest way to filter recent activity and inspect an entry.

From an MCP client, start an explicit executor-backed session and use `audit_tail(session_id=...)`. The default response is a bounded recent list. Use filters to narrow the result rather than requesting a large history.

To retrieve a retained full sanitized value, request one specific entry with `include_full_payloads=true`. This requires the additional `audit:full` scope. Redacted credentials cannot be recovered.

## Storage location

By default, local audit state is under:

```text
${XDG_STATE_HOME:-~/.local/state}/workgate/audit_log/
```

This is the Linux default; macOS and Windows use their native Workgate state location. The location follows `WORKGATE_STATE_DIR` when that setting is explicitly overridden. Executor-side audit history is selected through the same shared session-oriented UI and tool flow.

Do not publish this directory, attach it to public bug reports, or assume redaction removed every sensitive project value.

## Retention and limits

Audit retention, per-entry limits, and optional retained payloads are configurable. Use the generated [Configuration reference](../reference/configuration.md) instead of copying default values into deployment notes.

Reduce retention or disable retained payloads when the ability to inspect full sanitized values is not worth the storage and privacy cost.

## Permissions

Listing audit summaries requires `audit:read`. Full retained payloads require `audit:full`, and details of protected operations may also require the scopes used by those operations. A valid session that lacks a scope receives a forbidden response rather than being signed out.

## Troubleshooting

- **No entries appear:** confirm auditing is enabled, select the correct local or remote machine, and widen the time or event filters.
- **A detail is unavailable:** the payload may not have been retained, may have expired, or the current OAuth session may lack permission.
- **The audit directory is growing too large:** lower retention and payload limits in configuration.
- **You need to report a bug:** export only the smallest relevant entries and review them manually for secrets and project content.

For security guarantees and limitations, see [Security](../security.md). Contributors working on audit storage or event contracts should use [Development](../development.md) and the source tests as the canonical implementation reference.
