# Workgate

Workgate lets ChatGPT and other MCP clients work in a controlled
project workspace. It can inspect and edit files, run commands and tests, use
Git, manage persistent terminals, and delegate work to registered remote
machines.

Workgate originated from
[`fwerkor/local-shell-mcp`](https://github.com/fwerkor/local-shell-mcp) and is now
independently named and maintained. Its tool contracts, lifecycle model,
capabilities, and release line have diverged substantially from the predecessor.
See [Comparison with upstream](comparison.md) for the major functional differences.

## Start here

- Follow the [Quickstart](getting-started/quickstart.md) for a local service exposed through HTTPS.
- Add the service to [ChatGPT](getting-started/chatgpt-connector.md).
- Review [Security](security.md) before exposing the service outside localhost.

## What users can do

After connecting an MCP client, start an explicit workspace session and ask it to:

- inspect a repository and its instruction files;
- search, read, edit, and patch project files;
- run tests, build commands, and Git workflows;
- keep long-running jobs or persistent terminals;
- copy data between sessions on the same or different executors;
- review Todos and Audit history;
- use configured Skills or upstream MCP servers.

See [Common workflows](guides/common-workflows.md) for practical examples and the generated [Tool reference](reference/tools.md) for exact tool contracts.

## Human and remote access

The HTTP server includes a browser interface at `/ui`. An optional terminal client provides the same main management areas. See [Human interface](guides/human-interface.md).

To work on another machine while keeping one public MCP endpoint, enroll a [remote worker](guides/remote-workers.md).

!!! warning
    Give the service access only to workspaces you are prepared for an AI coding agent to modify. Keep OAuth enabled for public deployments, use narrow scopes, and leave full-control mode disabled unless the environment is disposable.
