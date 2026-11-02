# Skills and MCP integrations

Workgate can expose reusable **Skills** and tools from other **MCP servers** to connected clients.

## Add integrations

Open the Workgate WebUI and find the MCP and Skill management options for Control or a selected Executor. Add an upstream server, enable it and complete authorization if requested.

A Control-owned integration uses Control's network. An Executor-owned integration uses that Executor's environment and session workspace.

## Add a Skill

A Skill is a directory containing a **SKILL.md** file. Project Skills can live under:

~~~text
project/.agents/skills/my-skill/SKILL.md
~~~

Workgate also supports user-level and managed Skill directories. See the [Configuration reference](../reference/configuration.md) for options.

## Authorize from CLI

For a configured upstream OAuth server, use:

~~~sh
workgate mcp auth docs
workgate mcp auth docs --status
~~~

Replace **docs** with its configured name. The WebUI also provides authorization.

## Use an integration

Try asking your client:

> Use Workgate to show my available Skills and MCP integrations, then find a tool for searching our docs.

The generated [Tools reference](../reference/tools.md) describes the available tool contracts.

## Credentials

Store API keys and OAuth tokens in managed credentials rather than in project files. Review each upstream server and its access before enabling it.

For advanced manifests, inspect the [Agent Bridge source](https://github.com/rijuyuezhu/workgate/tree/main/src/workgate/agent_bridge).
