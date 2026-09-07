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
workgate worker --help
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
workgate server --remote-enabled true
```

Every `WORKGATE_*` application setting has a matching CLI flag using lowercase dashed form. For example:

```text
WORKGATE_REMOTE_ENABLED -> --remote-enabled true
```

## Executor commands

New machines use the final executor pairing flow:

```text
workgate executor connect CONTROL_URL [--name NAME]
workgate executor run
```

`connect` starts device-code pairing when the saved executor profile is absent or no longer authenticates. It prints the owner verification URL and short user code, then waits for approval. After approval, Workgate atomically saves the issued `control_url`, stable `executor_id`, and bearer credential before the first authenticated hello. If the existing profile still authenticates, `connect` reports that it is already paired and leaves it unchanged.

`run` uses only the saved executor profile. Normal network or control outages reconnect with the same long-lived credential; they do not require a fresh owner approval. Revocation or credential replacement requires owner action before that profile can authenticate again.

The old remote invite/join enrollment flow is no longer a public tool, API, or browser workflow. The legacy `workgate worker` runtime remains an internal migration implementation for already-enrolled workers until machine execution is fully moved behind the executor boundary; do not use it to provision new machines.
