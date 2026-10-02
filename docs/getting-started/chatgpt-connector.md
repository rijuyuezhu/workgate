# ChatGPT connector

Add `workgate` to ChatGPT as a custom MCP connector. Public deployments should use the built-in OAuth flow.

## Check the server

Confirm that:

1. the public HTTPS hostname reaches the server;
2. the control YAML `base_url` exactly matches that origin;
3. OAuth is enabled and the admin PIN is a long random value;
4. the health endpoint responds.

```bash
curl -i https://your-public-host.example.com/healthz
```

The connector URL is:

```text
https://your-public-host.example.com/mcp
```

## Add the connector

1. Open ChatGPT settings for connectors or custom MCP servers.
2. Add the public URL ending in `/mcp`.
3. Complete the OAuth approval flow with the configured admin PIN.
4. Refresh the connector's tools after changing the server tool surface.

Use a ChatGPT mode that exposes full MCP tools when you need shell, filesystem, Git, executor, or Agent Bridge operations. A limited connector mode may expose only search-style tools.

## Test it

```text
Use workgate. Create a task for inspecting my project workspace, start an execution session there attached to that task, inspect its instructions and Git status, and summarize what you can access. Do not edit files yet.
```

Then try a harmless command:

```text
Use workgate to run pwd in that session and report the output.
```

## Open a Live Workspace

When the ChatGPT client supports MCP Apps, ask it to open `workspace_open` with the semantic `task_id` returned by `task(action="create")`. You may also pass an attached `session_id` to select one execution context. The app stays bound to the task, shows all attached sessions and task-wide recent activity, and exposes session-specific jobs, persistent shells, and Human UI links when a session is selected. Human controls update the same task document; ending one execution session is a separate confirmed action and does not finish the task.

## Common mistakes

- The connector URL omits `/mcp`.
- The configured base URL includes `/mcp` instead of only the origin.
- The tunnel hostname and configured base URL do not match.
- OAuth is disabled on a public deployment.
- The client cached an older tool list after a server change.

See [Configuration](../reference/configuration.md) for OAuth settings and [Security](../security.md) before granting access to important workspaces.
