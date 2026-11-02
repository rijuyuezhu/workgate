# Choose a deployment

**Control** serves the MCP endpoint and WebUI. **Executors** provide access to the computers and projects where work happens.

| Your setup | What to install | Guide |
| --- | --- | --- |
| One computer | Colocate | [Quickstart](quickstart.md) |
| Several computers | Control on one host; Executor on each workspace host | [Install Workgate](install-options.md), then [Executors](../guides/executors.md) |
| Public Control on a VPS | Control on the VPS; Executors on your computers | [VPS deployment](vps.md) |

Once Control is running, connect ChatGPT or another client using the Control URL ending in /mcp. Open /ui to manage and approve Executors.

Choose an OS account for each Executor whose file and command permissions match the work you want it to perform. See [Security](../security.md).

For config options, consult the [Configuration reference](../reference/configuration.md). For shell-based development, see [Development](../development.md).
