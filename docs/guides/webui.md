# WebUI

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

Click the terminal for direct interactive keyboard input, or use the command box to submit whole lines. Select terminal text and choose **Copy** (Ctrl+Shift+C), or choose **Paste** (Ctrl+Shift+V) to insert clipboard text using the terminal's bracketed-paste mode when enabled. Use **Reconnect** to reattach the selected persistent shell without creating a new one; scrollback is available through the terminal's scrollbar. Terminal dimensions track container resizing, including narrow browser layouts.

Browser terminal traffic uses the executor's outbound stream path. If the bound executor is offline or streaming is unavailable, the UI reports the terminal as unavailable instead of falling back to a second transport.

### Files

Browse the selected executor filesystem, preview supported files, edit bounded UTF-8 text files, create files, and perform the operations offered by the selected machine. The editor checks the file revision before saving; if it changed elsewhere, your draft remains visible. Copy or reconcile your edits before choosing **Reload from disk**, which discards the draft only after confirmation.

Use **New folder** or **New file** to create entries, **Upload** for up to eight local files (2 MB each, including binary files), and breadcrumbs, filter, and sort to browse. Copy or cut a selected entry, navigate to the destination, then choose **Paste** to name the destination. File operations use in-app dialogs rather than browser destination-path prompts; uploads refuse to overwrite existing files. Upload is a WebUI-only feature, not a model-facing file tool.

Some executor file operations may be unavailable when the bound executor is offline or lacks the needed capability. The UI shows the available actions instead of emulating missing operations with control-local shell commands. Use `session_copy` through an MCP client for cross-workspace transfers.

### Task progress and plan

The Sessions view reads the same control-owned task state used by MCP clients. It shows status, objective, progress, and the canonical plan; the Plan editor and `read_todos`/`write_todos` all use those same plan steps.

`session_end` only ends the selected execution context. Its task remains readable and mutable, including when no execution sessions remain active.

### Native GUI viewer

In **Tasks → Sessions**, select an active execution session and click **Open GUI** to enter a dedicated read-only viewer. Use **Back to session** to return to the session Audit; The window list loads automatically; **Refresh windows** updates that list without losing the current selection if the window remains available. **Capture frame** requests a fresh screenshot from that session's bound executor. Frames are displayed only on demand, are not stored by the control service, and are cleared when the selected session or window changes. The viewer requires the `gui:use` OAuth scope and native GUI capture support on the executor. **Enter manual mode** enables explicit pointer clicks, double/right clicks, drags, focus, scrolling, text typing and key chords on the selected screenshot; **Exit manual mode** disables browser input. While manual mode is armed, wheel/trackpad scrolling over the screenshot scrolls the native window; focus the screenshot (click or Tab) to send navigation keys, Space and supported editing shortcuts directly. Shift+Tab can leave the screenshot; browser shortcuts not explicitly supported are not intercepted. Use the text form for multi-character typing (including ASCII and CJK/IME); there is no keyboard input queue. The existing scroll buttons and key/chord form remain available. Each input consumes one fresh, short-lived observation and then captures a new frame. When a state has expired or the window moved, capture again; failed actions are never automatically retried. Manual mode does not pause model agents or acquire exclusive desktop ownership: inputs use the same executor-side native serialization as model actions.

### Configuration

In **Executors**, open **Control config** or an Executor's **Config** subtab. The **Setting / Config / Default** columns show all available role settings (64 for Control, 47 for Executor). An absent file setting displays **unset**, distinct from an explicit `null`. If the live value differs from Config/Default, a smaller **In use** line shows the current value and its source (`CLI`, `ENV`, etc.). Groups are expanded by default. Select **Edit** and **Set** or **Unset** (remove the file key), then **Save changes** to the selected YAML or service JSON file. Unsaved edits have a yellow outline and appear as `old → new`. **Only changed** filters to staged edits; **Non-default** shows fields where the file value, staged value, or running effective value differs from Default. Enable both to intersect their results. Saves validate the Settings schema, check for concurrent edits and write a private backup. **Saving never changes running settings.** When the saved file differs from the running version and a Parent is available, use **Apply saved file → Confirm restart** to restart only that role. The browser reconnects and shows whether the new config started or was rolled back to the last healthy version. A Parent retains startup CLI/ENV; changing those requires restarting the Parent itself.

Uses existing WebUI read/write permissions; secrets are displayed unmasked. Offline Executors and files that cannot be safely edited remain read-only. Browser responses are not cached. If the file changed in another session, reload before editing again.

### Audit

Filter recent activity and inspect details allowed by the current OAuth scopes. Audit entries may contain project text and command output; treat them as sensitive. See [Audit log](audit-log.md).

## Authentication and sessions

The browser uses the server's OAuth flow and the configured admin PIN for
approval. Sign out from the UI when using a shared browser. A `401` normally
means the session must authenticate again; a `403` means the current session is
valid but lacks a required scope.

## Common problems

- **`/ui` returns 404:** confirm the server is in supported `mcp` or `http` mode and the UI is enabled.
- **An executor-backed panel is unavailable:** verify that the executor is online, the session is bound to it, and it supports the requested operation.
- **A terminal looks disconnected:** verify the executor connection, then reattach to the persistent shell.
- **An action is forbidden:** sign in again with the scopes required for that operation.

See [Troubleshooting](../troubleshooting.md) for server-wide diagnostics and [Security](../security.md) for the trust model.
