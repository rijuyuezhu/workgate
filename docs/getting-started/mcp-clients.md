# Connect another MCP client

Workgate supports MCP clients through **Streamable HTTP** or a local **stdio**
child process. Install and configure Workgate first using the
[Quickstart](quickstart.md) and [Install options](install-options.md).

| Transport | Choose it when | Client setting |
| --- | --- | --- |
| Streamable HTTP | Workgate is already running, or the client runs on another machine | MCP server URL |
| stdio | Your MCP client launches a local Workgate process itself | Command and arguments |

## Connect to a running control (recommended)

Start Workgate with the normal [colocate](colocate.md) or
[control + executor](deployment.md) deployment. For a local colocated server,
enter this **Streamable HTTP** URL in your client's MCP server settings:

```text
http://127.0.0.1:8765/mcp
```

For remote clients, use your control's public **HTTPS** URL ending in `/mcp`,
for example `https://control.example.com/mcp`. Public HTTP MCP uses Workgate's
OAuth authorization flow: complete the client's authorization prompt and any
owner approval. Never disable OAuth on a publicly reachable server. The
`base_url` configuration is the origin *without* `/mcp`.

A control needs at least one connected **executor** for filesystem and shell
operations. Pair the executor once via the normal device-code flow, including for
colocated deployments. See [Executors](../guides/executors.md).

## Let a local client start stdio

A stdio-only Workgate control has **no HTTP executor connection or pairing
endpoint**. Unlike colocated HTTP, it cannot attach an executor for shell,
filesystem, or session operations. Use HTTP above for ordinary workspace tasks.

If your local client needs the control-only MCP interface, install Workgate from
a [GitHub release](install-options.md) or launch the pinned GitHub revision
through `uvx`. The generic JSON shape is:

```json
{
  "mcpServers": {
    "workgate": {
      "command": "uvx",
      "args": [
        "--python", "3.14",
        "--from", "git+https://github.com/rijuyuezhu/workgate.git@v5.0.0a1",
        "workgate", "control", "--mode", "stdio", "--auth-mode", "none"
      ]
    }
  }
}
```

Client configuration keys vary; use its equivalent command, arguments and
environment fields. If installed with `pipx`, use `workgate` as the command and
`["control", "--mode", "stdio", "--auth-mode", "none"]` as the arguments.
Use the appropriate release tag in place of the prerelease example above.

Stdio communicates only with its parent process, not over a public network.
The `--auth-mode none` example is **only for that local stdio process**; do not
copy it into a public HTTP deployment.

## First check

After connecting, ask the client:

> List the Workgate tools and report the Workgate server version. Do not change
> any files.

If you use HTTP with a connected executor, continue with:

> Start a session in my configured project workdir and report the Git status.
> Do not modify anything.

For ChatGPT's connector settings and OAuth approval flow, see the separate
[ChatGPT connector](chatgpt-connector.md) guide.
