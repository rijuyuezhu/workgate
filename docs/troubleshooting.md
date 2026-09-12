# Troubleshooting

## Connector shows only `search` and `fetch`

Likely causes:

- ChatGPT Developer Mode is not enabled.
- The client is a regular connector or Deep Research-style client that only supports read-only tools.
- The connector tool list was not refreshed after server changes.

Enable Developer Mode for the full coding-agent surface, then refresh the connector.

## OAuth approval fails

Check:

```env
WORKGATE_BASE_URL=https://your-public-host.example.com
WORKGATE_AUTH_MODE=oauth
WORKGATE_OAUTH_ADMIN_PIN=...
```

Make sure the connector URL is exactly:

```text
https://your-public-host.example.com/mcp
```

The public base URL should be the origin only, without `/mcp`.

## Public URL does not work

Check the local health endpoint first:

```bash
curl -i http://127.0.0.1:8765/healthz
```

Then check the tunnel or reverse proxy:

- It must forward to the server port, usually `8765`.
- It must preserve HTTPS externally for ChatGPT.
- The public hostname must match `WORKGATE_BASE_URL`.

## Tool call times out

Public tool calls are bounded by strict watchdogs and shell timeouts. Check these settings:

```env
WORKGATE_TOOL_TIMEOUT_S=60
WORKGATE_RUN_SHELL_DEFAULT_TIMEOUT_S=10
WORKGATE_RUN_SHELL_MAX_TIMEOUT_S=120
```

Use persistent shells for long-running dev servers, REPLs, or interactive commands.

## Executor does not connect

Check:

- The control URL is reachable from the executor over outbound HTTPS, or loopback HTTP for a same-machine deployment.
- `workgate executor connect CONTROL_URL` completed owner-approved pairing and persisted the executor profile.
- The executor has not been revoked or had its credential replaced.
- `workgate executor run` is running from the same private state/config context that contains the paired profile.

The executor reconnects after temporary network or control outages using its saved long-lived credential; ordinary downtime does not require a new pairing. If authentication is rejected because trust was revoked or replaced, run `workgate executor connect CONTROL_URL` and explicitly approve the replacement from the **Executors** page.

If the executor process exits or fails before connecting, run `workgate executor run` in the foreground and inspect its local output. Workgate no longer provides the legacy `workgate worker` status/log/service commands, so unattended lifecycle diagnostics belong to the service manager used to launch `executor run`.

## Audit log is missing expected calls

Every routed MCP or REST debug tool call should produce a `tool_call_start` and `tool_call_end` pair. Check:

- The server is the instance your connector is actually using.
- The audit log path points to the location you are tailing.
- The state directory is writable.
- The call is not being rejected before routing.

## Release binary lacks system tools

The standalone binary includes the Python server and default OAuth
dependencies. On POSIX systems, persistent shells require a host tmux executable;
the default is `tmux` from `PATH`, and `WORKGATE_TMUX_BIN` may point to a
different executable. Git, shells, compilers, LibreOffice, and other host tools
are not bundled.
