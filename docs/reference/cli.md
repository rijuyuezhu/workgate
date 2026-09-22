# CLI reference

Every runtime mode is selected by an explicit argparse subcommand.

```text
workgate server [--config PATH] [--mode MODE] [--host HOST] [--port PORT] [--workspace-root PATH] [...]
```

Use the built-in help for exact parser output:

```bash
workgate --help
workgate server --help
workgate tui --help
workgate executor --help
```

## Server modes

| Mode | Purpose |
|---|---|
| `mcp` | Serve MCP over HTTP at `/mcp`. This is the default public ChatGPT connector mode. |
| `stdio` | Run a stdio MCP server for local MCP clients. |
| `http` | Start the REST debug API only. |
| `both` | Reserved and exits with an error. Run separate processes if you need MCP and REST together. |

## Development examples

Run a local MCP server without OAuth:

```bash
WORKGATE_AUTH_MODE=none uv run workgate server --mode mcp
```

Run the REST debug API:

```bash
WORKGATE_AUTH_MODE=none uv run workgate server --mode http
```

Run with a specific workspace root:

```bash
WORKGATE_WORKSPACE_ROOT=/path/to/project uv run workgate server --mode mcp
```

## Boolean arguments

Boolean CLI values are explicit:

```bash
workgate server --allow-full-control false
workgate server --agent-bridge-enabled true
```

Every `WORKGATE_*` application setting has a matching CLI flag using lowercase dashed form. For example:

```text
WORKGATE_AGENT_BRIDGE_ENABLED -> --agent-bridge-enabled true
```

## Executor commands

New machines use the final executor pairing flow, and paired machines can optionally install a local per-user service:

```text
workgate executor connect CONTROL_URL [--name NAME]
workgate executor run
workgate executor install-service
workgate executor status
workgate executor start
workgate executor stop
workgate executor restart
workgate executor logs [--lines N]
workgate executor uninstall-service
```

`connect` starts device-code pairing when the saved executor profile is absent or no longer authenticates. It prints the owner verification URL and short user code, then waits for approval. After approval, Workgate atomically saves the issued `control_url`, stable `executor_id`, and bearer credential before an auth-only credential validation. That validation does not mark the executor online or publish resource inventory. If the existing profile still authenticates, `connect` reports that it is already paired and leaves it unchanged.

`run` uses only the saved executor profile. Normal network or control outages reconnect with the same long-lived credential; they do not require a fresh owner approval. Revocation or credential replacement requires owner action before that profile can authenticate again.

`install-service` is local-only host administration. It requires an existing paired profile, snapshots executor runtime settings to private Workgate state, installs the native per-user service for Linux systemd, macOS launchd, or Windows Task Scheduler, and starts it immediately. Re-running the command refreshes the service definition without changing the executor profile. Python environments retain their own interpreter path instead of resolving through a virtualenv symlink; Windows frozen releases use a private PowerShell logging launcher while runtime freshness still tracks the release executable. `status` reports the portable service state, stale runtime definitions, and native-definition mismatches where detectable; `logs` returns a bounded recent view with secret redaction. The lifecycle commands do not have MCP equivalents.

The old remote invite/join and `workgate worker` surfaces are removed. New and existing machines use the executor profile and pairing flow above.
