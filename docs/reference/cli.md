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

## Control administration

Control provides **init-config** and native service management commands. For an existing development checkout, see [Control service](../getting-started/control-service.md).

## Configuration and tools

Use [Configuration](configuration.md) to inspect defaults and supported settings. For MCP tools and their current argument schemas, use the generated [Tools reference](tools.md).
