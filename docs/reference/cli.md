# CLI reference

Every runtime role is selected by an explicit argparse subcommand. The public
control entrypoint is:

```text
workgate control [--config PATH] [--mode MODE] [--host HOST] [--port PORT] [...]
```

The former `workgate server` command is removed. Use the built-in help for the
exact parser surface:

```bash
workgate --help
workgate control --help
workgate tui --help
workgate executor --help
```

## Control modes

| Mode | Purpose |
|---|---|
| `mcp` | Serve MCP over HTTP at `/mcp`. This is the default public connector mode. |
| `stdio` | Run a stdio MCP control process for local MCP clients. |
| `http` | Start the REST/debug and Human UI HTTP service. |
| `both` | Reserved and exits with an error. Run separate processes if both transports are needed. |

The control CLI intentionally does **not** expose executor machine-policy
settings such as `--workspace-root`, `--allow-full-control`, command/path
denylists, shell executables, or local tool binary paths. Those settings belong
to the executor process.

## Development examples

Run MCP-over-HTTP locally without OAuth:

```bash
WORKGATE_AUTH_MODE=none uv run workgate control --mode mcp
```

Run the REST/debug API:

```bash
WORKGATE_AUTH_MODE=none uv run workgate control --mode http
```

Boolean CLI values are explicit:

```bash
workgate control --ui-enabled false
workgate control --agent-bridge-enabled true
```

Use role-specific config files for long-running deployments. The generated
[Configuration](configuration.md) reference documents the shared settings
schema and environment variables; a command's `--help` is authoritative for
which settings that role accepts as CLI overrides.

## Executor commands

New machines use the final executor pairing flow:

```text
workgate executor connect CONTROL_URL [--name NAME]
workgate executor run
```

`connect` starts device-code pairing when the saved executor profile is absent or no longer authenticates. It prints the owner verification URL and short user code, then waits for approval. After approval, Workgate atomically saves the issued `control_url`, stable `executor_id`, and bearer credential before an auth-only credential validation. That validation does not mark the executor online or publish resource inventory. If the existing profile still authenticates, `connect` reports that it is already paired and leaves it unchanged.

`run` uses only the saved executor profile. Normal network or control outages reconnect with the same long-lived credential; they do not require a fresh owner approval. Revocation or credential replacement requires owner action before that profile can authenticate again.

The old remote invite/join and `workgate worker` surfaces are removed. New and existing machines use the executor profile and pairing flow above.
