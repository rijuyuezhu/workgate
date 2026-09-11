# Human interface

The HTTP server includes a browser interface for managing the control service and paired executors. An optional OpenTUI client provides the same main areas in a terminal.

## Start the browser interface

Run the HTTP server:

```bash
workgate server --mode http
```

Open:

```text
http://127.0.0.1:8765/ui
```

When the service is exposed through a public hostname, use the corresponding public `/ui` URL and sign in through OAuth. Do not expose an unauthenticated HTTP service to a shared or public network.

The UI path and enablement can be changed through `WORKGATE_UI_PATH` and `WORKGATE_UI_ENABLED`. See [Configuration](../reference/configuration.md).

## Start OpenTUI

Keep the HTTP server running, then start the terminal client in another terminal:

```bash
workgate tui
```

By default it connects to the loopback Human UI API on the configured server port. Use `--api-base` only when the local API is listening at another loopback URL.

OpenTUI availability depends on the installation and platform. If the native
client is unavailable, use the browser interface or install a supported platform
build; see [Install options](../getting-started/install-options.md). Source
contributors should use [Development](../development.md) and
[Native artifact provenance](../maintenance/native-artifact-provenance.md).

## Browser OpenTUI Console

When an OpenTUI runtime is available, the browser UI can also show an
**OpenTUI Console**. It launches a separate terminal UI inside the browser;
closing that console stops only the console client, not the HTTP server or
existing persistent shells.

## Main areas

### Dashboard

View health, resource usage, recent activity, and alerts for the selected executor. Missing metrics are shown as unavailable rather than guessed.

### Executors

Approve pairing requests, inspect or rename executors, see whether they are online, and revoke trust from the **Executors** page. Pairing and reconnect behavior are covered in [Executors](remote-workers.md).

### Terminals

Create, attach to, resize, and close persistent terminals. A persistent shell continues after the browser tab or OpenTUI client disconnects. Closing a client view does not necessarily terminate the underlying shell; use the explicit terminate action when you are finished.

Interactive terminal support depends on the selected machine. When a full interactive stream is unavailable, the UI falls back to a compatible snapshot view.

### Files

Browse the selected workspace, preview supported files, edit bounded UTF-8 text files, create files, and perform the operations offered by the selected machine. The UI refuses unsafe partial edits and reports when a file changed concurrently.

Some executor file operations may be unavailable when the bound executor is offline or lacks the needed capability. The UI shows the available actions instead of emulating missing operations with control-local shell commands. Use `session_copy` through an MCP client for cross-workspace transfers.

### Todos

View and update the Todo list associated with a shared workspace session. If another client changes the same list, reload the latest version instead of overwriting it.

### Audit

Filter recent activity and inspect details allowed by the current OAuth scopes. Audit entries may contain project text and command output; treat them as sensitive. See [Audit log](audit-log.md).

## Authentication and sessions

The browser uses the server's OAuth flow and the configured admin PIN for
approval. Sign out from the UI when using a shared browser. A `401` normally
means the session must authenticate again; a `403` means the current session is
valid but lacks a required scope.

The native OpenTUI client connects only through the trusted loopback Human UI API. Do not place credentials in `--api-base` or expose that private local API as a public endpoint.

## Common problems

- **`/ui` returns 404:** confirm the server is in supported `mcp` or `http` mode and the UI is enabled. The reserved `both` mode does not start a server.
- **OpenTUI cannot start:** use the browser UI, then confirm that your installation contains a platform-native runtime.
- **An executor-backed panel is unavailable:** verify that the executor is online, the session is bound to it, and it supports the requested operation.
- **A terminal looks disconnected:** reattach to the persistent shell or use its snapshot view.
- **An action is forbidden:** sign in again with the scopes required for that operation.

See [Troubleshooting](../troubleshooting.md) for server-wide diagnostics and [Security](../security.md) for the trust model.
