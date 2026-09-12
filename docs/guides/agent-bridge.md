# Agent capability bridge

The Agent Bridge makes reusable Skills and selected upstream MCP servers available through `workgate`. Use it when several clients should share the same server-managed capabilities.

## Configuration directory

Declarative Agent Bridge configuration lives in the Workgate config namespace. On Linux the default is:

```text
${XDG_CONFIG_HOME:-~/.config}/workgate/agent/
  config.json
  skills/
    debugging/
      SKILL.md
```

macOS and Windows use their native Workgate config locations. Credentials managed by the CLI remain in private durable state rather than this directory. Legacy literal `env` and `headers` values are still accepted in manifests for compatibility, so treat the configuration directory as potentially sensitive and preserve restrictive file permissions. Use managed secret references for new credentials rather than placing tokens or passwords directly in `config.json`.

The bridge also discovers project Skills from `<workdir>/.agents/skills` and user Skills from `$XDG_CONFIG_HOME/agents/skills` when `XDG_CONFIG_HOME` is an absolute path. If `XDG_CONFIG_HOME` is unset or invalid, the user Skill directory falls back to `~/.config/agents/skills`. A project Skill with the same directory name takes priority for that session.

## Minimal configuration

A minimal `config.json` is:

```json
{
  "version": 1
}
```

Example with one upstream MCP server and managed Skills:

```json
{
  "version": 1,
  "mcpServers": {
    "docs": {
      "integrationId": "docs",
      "type": "http",
      "url": "https://docs.example.com/mcp",
      "auth": {
        "mode": "oauth",
        "scopes": ["tools.read"]
      },
      "enabled": true
    }
  },
  "skills": {
    "enabled": true,
    "directory": "skills"
  },
  "dynamicTools": {
    "mcp": true,
    "skills": true
  }
}
```

Supported upstream types are `stdio`, `http`, and `sse`. Review every command, URL, tool description, and requested scope before enabling a server.

Credential-bearing servers use `integrationId` as their stable private identity. Set it when using OAuth, structured secret references, or credential-like literal headers/environment values. It must be unique within the manifest and must stay unchanged if you later rename the `mcpServers` key. For an existing configuration, using the current server key as the initial `integrationId` preserves the natural credential-store identity. Servers with only ordinary non-sensitive literals do not require one.

Enabled `stdio` servers are kept alive and reused by the Agent Bridge instead of being restarted for every probe or tool call. This is required for upstreams that hold process-local state or accept a secondary long-lived connection, such as browser-extension MCP servers. Changing, disabling, or removing the configured server tears down the retained process.

## Add a Skill

Create a directory containing `SKILL.md`:

```text
<Workgate config>/agent/skills/debugging/SKILL.md
```

```markdown
# Debugging

Reproduce the failure first, inspect the smallest relevant code path, and verify a minimal fix with focused tests.
```

Use `list_agent_skills` to see discovered names and sources, then `activate_agent_skill` to load one. Related files returned by a Skill can be read with `read_agent_skill_file`.

When dynamic Skill tools are enabled, selected Skills also appear directly in the MCP tool list.

## Store a static secret

Secret references are valid only when the same server uses `auth.mode="secret"`. For example, this complete entry configures a server named `github` with a managed API-key header:

```json
{
  "version": 1,
  "mcpServers": {
    "github": {
      "integrationId": "github",
      "type": "http",
      "url": "https://github.example.com/mcp",
      "headers": {
        "X-API-Key": {"secret": "github_token"}
      },
      "auth": {"mode": "secret"},
      "enabled": true
    }
  }
}
```

Set the referenced value through standard input so it does not appear in the command arguments. For a configured integration, use its `mcpServers` display name:

```bash
printf '%s\n' "$GITHUB_TOKEN" | workgate mcp secret set github github_token --stdin
workgate mcp secret list github
workgate mcp secret delete github github_token
```

The CLI resolves that display name to the entry's stable `integrationId` before reading or changing private credentials. Therefore a later rename from `github` to another `mcpServers` key does not reset credential or redaction history as long as `integrationId` remains `github`. Keeping that explicit `integrationId` also preserves retired-value redaction history if the integration is later reconfigured without credentials.

`secret set` always requires a currently configured server. `secret list` and `secret delete` also support cleanup after an integration has been removed: `workgate mcp secret list` shows stored integration identities, and a detached `integrationId` can be supplied in the normal server position to inspect or delete its stored secret names. If a live display name is present, it is resolved to its stable identity unless that exact argument already names a stored secret bucket.

Literal `env` and `headers` are still accepted for compatibility. Credential-like literal keys such as `Authorization`, `Cookie`, `TOKEN`, `PASSWORD`, or API-key fields participate in durable retired-value redaction and therefore also require `integrationId`. Ordinary literal values such as `MODE=production`, `LOG_LEVEL=info`, or `X-Mode: 1` are redacted only for the current server snapshot and are not persisted as a global substring filter. Prefer structured secret references for credentials, especially when the application uses an unusual key name.

## Authorize an OAuth server

```bash
workgate mcp auth docs
workgate mcp auth docs --status
workgate mcp auth docs --logout
```

Use `--no-open` when the CLI should print the authorization URL instead of opening a browser.

`--logout` is also the local cleanup path for retired OAuth identities. If the argument names stored OAuth state that no currently configured OAuth server owns, Workgate clears that local state without attempting network revocation and reports `remote_revocation: "unavailable"`. If a renamed live OAuth server still owns the same stable `integrationId`, supplying that identity resolves back to the live server so remote revocation can still be attempted. An exact detached OAuth identity takes precedence over a reused display name, preventing stale cleanup from touching a different live integration.

## Use bridge capabilities

Start by asking the client to show what is available:

```text
Use workgate to check Agent Bridge status, list available Skills and upstream MCP servers, and summarize the extra capabilities.
```

Before calling an upstream tool, list that server's tools and review the selected tool name and description.

Dynamic tools are convenient, but they make the exposed tool list change with configuration. Disable them when clients should use only the fixed bridge tools.

## Security notes

- Stdio servers run in the `workgate` server environment.
- HTTP and SSE servers use the server's network access, not the MCP client's.
- An upstream server can use any secret or OAuth scope granted to it. Configure only trusted servers and use the narrowest credentials possible.
- Keep the entire service state directory private and back it up only through a secure process.

See [Configuration](../reference/configuration.md) for settings, [Tool reference](../reference/tools.md) for exact bridge tools, and [Development](../development.md) for implementation guidance.
