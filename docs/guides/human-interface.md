# Human interface

The HTTP control process provides a browser WebUI for managing the control service and paired executors.

## Start the browser interface

Run the HTTP control process:

```bash
workgate control --mode http
```

Open:

```text
http://127.0.0.1:8765/ui
```

When the service is exposed through a public hostname, use the corresponding public `/ui` URL and sign in through OAuth. Do not expose an unauthenticated HTTP service to a shared or public network.

The `ui_path` and `ui_enabled` settings control the browser UI. See [Configuration](../reference/configuration.md).

## Main areas

### Dashboard

View health, resource usage, recent activity, and alerts for the selected executor. Missing metrics are shown as unavailable rather than guessed.
Executor status and names refresh automatically across Dashboard, Files, Terminals, and Sessions. Offline or revoked executors cannot be used for new file or terminal operations; a deep-linked executor stays selected as unavailable instead of silently switching machines.

### Executors

Approve pairing requests, inspect or rename executors, see whether they are online, and revoke trust from the **Executors** page. Pairing and reconnect behavior are covered in [Executors](executors.md).

### Terminals

Create, attach to, resize, and close persistent terminals. A persistent shell continues after the browser tab disconnects. Closing a client view does not necessarily terminate the underlying shell; use the explicit terminate action when you are finished.

Browser terminal traffic uses the executor's outbound stream path. If the bound executor is offline or streaming is unavailable, the UI reports the terminal as unavailable instead of falling back to a second transport.

### Files

Browse the selected executor filesystem, preview supported files, edit bounded UTF-8 text files, create files, and perform the operations offered by the selected machine. The editor checks the file revision before saving; if it changed elsewhere, your draft remains visible. Copy or reconcile your edits before choosing **Reload from disk**, which discards the draft only after confirmation.

Some executor file operations may be unavailable when the bound executor is offline or lacks the needed capability. The UI shows the available actions instead of emulating missing operations with control-local shell commands. Use `session_copy` through an MCP client for cross-workspace transfers.

### Task progress and plan

The Sessions view reads the same control-owned task state used by MCP clients. It shows status, objective, progress, and the canonical plan; the Plan editor and `read_todos`/`write_todos` all use those same plan steps.

`session_end` only ends the selected execution context. Its task remains readable and mutable, including when no execution sessions remain active.

### Audit

Filter recent activity and inspect details allowed by the current OAuth scopes. Audit entries may contain project text and command output; treat them as sensitive. See [Audit log](audit-log.md).

## Authentication and sessions

The browser uses the server's OAuth flow and the configured admin PIN for
approval. Sign out from the UI when using a shared browser. A `401` normally
means the session must authenticate again; a `403` means the current session is
valid but lacks a required scope.

## Common problems

- **`/ui` returns 404:** confirm the server is in supported `mcp` or `http` mode and the UI is enabled. The reserved `both` mode does not start a server.
- **An executor-backed panel is unavailable:** verify that the executor is online, the session is bound to it, and it supports the requested operation.
- **A terminal looks disconnected:** verify the executor connection, then reattach to the persistent shell.
- **An action is forbidden:** sign in again with the scopes required for that operation.

See [Troubleshooting](../troubleshooting.md) for server-wide diagnostics and [Security](../security.md) for the trust model.
