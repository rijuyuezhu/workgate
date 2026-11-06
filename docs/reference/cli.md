# CLI reference

Use the installed **workgate** command for administration, or **uv run workgate** from a development checkout.

CLI help reflects the installed version:

~~~sh
workgate --help
workgate control --help
workgate executor --help
workgate colocate --help
~~~

## Run services

| Role | Command | Purpose |
| --- | --- | --- |
| Control | workgate control | Serve MCP, OAuth and WebUI |
| Executor | workgate executor run | Connect a paired Executor |
| Colocate | workgate colocate | Run both roles on one computer |

For the normal first-time setup, use [Install Workgate](../getting-started/install-options.md).

## Pair and manage Executors

~~~sh
workgate executor status
workgate executor logs
~~~

Pairing and remote installation are covered in [Executors](../guides/executors.md). The Executor CLI also provides service install, start, stop, restart and uninstall operations. Use its **--help** output for arguments and platform options.

## Call MCP tools from a script

With an HTTP Control already running on the same computer, call its registered tools directly:

~~~sh
workgate call session_start --json '{"workdir":"."}'
workgate call read --session SESSION_ID --json '{"path":"README.md"}'
printf '%s' '{"session_id":"SESSION_ID","path":"README.md"}' | workgate call read
~~~

The command returns JSON to stdout and a nonzero exit code if the tool or request fails. The default Control URL uses the configured host and port; use `--url http://127.0.0.1:PORT/mcp` when needed. Only loopback URLs are accepted, and this command never executes tools outside the running Control.

If Control requires OAuth, provide an existing access token through an owner-private file (`workgate call read ... --token-file ~/.config/workgate/call-token`, mode `0600` on Unix) or the `WORKGATE_CALL_TOKEN` environment variable. Tokens are supplied as normal bearer credentials and remain subject to the same Control authorization policies; this command does not create or bypass OAuth credentials. An unauthenticated local Control needs no token. Run `workgate call --help` for the full CLI contract.

## Control administration

Control provides **init-config** and native service management commands. For an existing development checkout, see [Control service](../getting-started/control-service.md).

## Configuration and tools

Use [Configuration](configuration.md) to inspect defaults and supported settings. For MCP tools and their current argument schemas, use the generated [Tools reference](tools.md).
