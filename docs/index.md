# Workgate

Workgate lets ChatGPT and other MCP clients work in controlled project
workspaces. One **control** endpoint handles clients, authentication, UI, and
orchestration; paired **executors** perform machine-facing work such as files,
shells, jobs, and terminals.

## Start here

- Follow the [Quickstart](getting-started/quickstart.md) for the simplest local setup.
- Use [Choose a deployment](getting-started/deployment.md) for public HTTPS, VPS, or multi-machine setups.
- Pair another machine with [Executors](guides/executors.md).
- Connect the service to [ChatGPT](getting-started/chatgpt-connector.md).
- Read [Security](security.md) before exposing Workgate outside localhost.

## What you can do

After connecting an MCP client, start an execution session and ask it to:

- inspect, search, edit, and patch repositories;
- run tests, builds, Git commands, and bounded shell tasks;
- keep long-running jobs or persistent terminals;
- copy data between sessions on the same or different executors;
- keep durable task objectives, progress reports, findings, blockers, and structured plans across control/executor restarts;
- review task/plan state and Audit history in the Human UI;
- use configured Skills or upstream MCP servers.

See [Common workflows](guides/common-workflows.md) for examples and the
generated [Tool reference](reference/tools.md) for exact tool contracts.

The browser interface is available at `/ui`; an optional terminal client
provides the same main management areas. See
[Human interface](guides/human-interface.md).

!!! warning
    An executor can access any filesystem location permitted to its OS account.
    Run it as a suitably restricted user, and keep OAuth enabled for public
    deployments.
