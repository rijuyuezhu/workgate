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
workgate executor --help
```

## Control modes

| Mode | Purpose |
|---|---|
| `mcp` | Serve MCP over HTTP at `/mcp`. This is the default public connector mode. |
| `stdio` | Run a stdio MCP control process for local MCP clients. |
| `http` | Start the WebUI and an HTTP debug wrapper of the registered MCP tools. |

The control CLI intentionally does **not** expose executor machine
settings such as `--default-workdir`, shell executables, or executor tool binary
paths. Those settings belong to the executor process.

## Development examples

Run MCP-over-HTTP locally without OAuth:

```bash
WORKGATE_AUTH_MODE=none uv run workgate control --mode mcp
```

Run the debug HTTP wrapper (the same MCP tools and result contract; not a second public tool protocol):

```bash
WORKGATE_AUTH_MODE=none uv run workgate control --mode http
```

The debug endpoints are `GET /tools` (MCP tool metadata) and
`POST /tools/{tool_name}` with the MCP argument object as JSON. Calls return
MCP `CallToolResult` JSON (`content`, `structuredContent`, `isError`), including
MCP tool validation and scope failures; they do not use the former individual
REST routes or HTTP status/error envelopes. An omitted argument body is `{}`;
protocol-level MCP errors return the SDK error data (`code`, `message`, optional
`data`) with HTTP 400 rather than pretending to be a tool result. For example:

```bash
curl http://127.0.0.1:8765/tools
curl -X POST http://127.0.0.1:8765/tools/version -H 'Content-Type: application/json' -d '{}'
```

Keep this interface for local development and debugging, not as a replacement
for an MCP client connection to `/mcp`. Running MCP HTTP and debug HTTP in the
same process is not currently an advertised mode.


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

`connect` accepts only pairing/profile location options (`--config`, `--state-dir`, and `--name`); runtime settings such as `--default-workdir` belong to `run` or `install-service`. Service status/start/stop/restart/uninstall/log commands accept `--config` and `--state-dir`, while still reading the complete executor configuration for correct staleness detection. Agent Bridge `mcp` credential commands expose only `--config`, `--state-dir`, and `--agent-mcp-call-timeout-s`. The private `bounded-runner` and `job-runner` entrypoints remain callable internally but are omitted from root help.

`run` uses only the saved executor profile. Normal network or control outages reconnect with the same long-lived credential; they do not require a fresh owner approval. Revocation or credential replacement requires owner action before that profile can authenticate again.

`install-service` is local-only host administration. It requires an existing paired profile, snapshots the current executor runtime settings into private Workgate state, installs the native per-user service for Linux or macOS, and starts it immediately. Reinstalling refreshes the service definition without changing executor identity. Persistent bootstrap installs are control-managed and may replace their verified standalone runtime to match control; package-manager, Python-package, and source installs are never self-mutated. `status` reports runtime ownership, local update state, and stale runtime/definition mismatches when detectable; `logs` returns a bounded, redacted recent view. These lifecycle commands are not MCP tools.
