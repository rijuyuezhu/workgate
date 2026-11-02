# Connect an MCP client

Workgate exposes a **Streamable HTTP** MCP endpoint.

For an MCP client running on the **same computer as Control**, enter this server URL in the client's MCP settings:

~~~text
http://127.0.0.1:8765/mcp
~~~

For remote or cloud-hosted clients, use Control's reachable HTTPS URL ending in /mcp, for example https://control.example.com/mcp. Sign in and approve access when the client prompts you.

After connecting, try:

> Use Workgate to start a session in my project and inspect the Git status.

A connected Executor provides file, shell, and session tools. [Install and pair an Executor](../guides/executors.md) if you haven't added one yet.

## Local stdio clients

For a client that requires a local stdio subprocess, Workgate also supports:

~~~json
{
  "mcpServers": {
    "workgate": {
      "command": "workgate",
      "args": ["control", "--mode", "stdio", "--auth-mode", "none"]
    }
  }
}
~~~

Stdio runs a Control-only interface. Use the HTTP endpoint for workspace operations on Executors. Client configuration fields vary; consult your client's documentation.

For ChatGPT-specific steps, see [Connect ChatGPT](chatgpt-connector.md). For connection errors, see [Troubleshooting](../troubleshooting.md).
