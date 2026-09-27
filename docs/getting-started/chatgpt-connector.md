# ChatGPT connector

Add `workgate` to ChatGPT as a custom MCP connector. Public deployments should use the built-in OAuth flow.

## Check the server

Confirm that:

1. the public HTTPS hostname reaches the server;
2. `WORKGATE_BASE_URL` exactly matches that origin;
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
Use workgate. Start a session in my project workspace, inspect its instructions and Git status, and summarize what you can access. Do not edit files yet.
```

Then try a harmless command:

```text
Use workgate to run pwd in that session and report the output.
```

## Common mistakes

- The connector URL omits `/mcp`.
- The configured base URL includes `/mcp` instead of only the origin.
- The tunnel hostname and configured base URL do not match.
- OAuth is disabled on a public deployment.
- The client cached an older tool list after a server change.

See [Configuration](../reference/configuration.md) for OAuth settings and [Security](../security.md) before granting access to important workspaces.
