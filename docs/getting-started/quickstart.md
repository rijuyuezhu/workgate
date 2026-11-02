# Quickstart

Start with Workgate on one computer, then connect your MCP client.

## 1. Install

On **Linux**, install **Colocate** (Control and Executor together) from the latest published release (including prereleases):

~~~sh
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/main/deploy/source/bootstrap.sh | sh -s -- --latest --role colocate --name workstation
~~~

You need Git, curl and an interactive terminal. The installer sets up the runtime and shows you how to approve the local Executor in the WebUI.

On **Windows**, use the [PowerShell installation instructions](install-options.md#windows-powershell). For a different layout, see [Install Workgate](install-options.md).

## 2. Connect

After installation, open your local [WebUI](http://127.0.0.1:8765/ui). For an MCP client running on the **same computer**, use:

~~~text
http://127.0.0.1:8765/mcp
~~~

For **ChatGPT** or an MCP client running on another computer, set up a reachable HTTPS endpoint with [VPS deployment](vps.md), then follow [Connect ChatGPT](chatgpt-connector.md) or [Connect an MCP client](mcp-clients.md). Cloud-hosted clients cannot use your computer's localhost address.

## 3. Try it

Ask your client:

> Use Workgate to start a session in my project directory, show the Git status, and summarize the project.

See [Common workflows](../guides/common-workflows.md) for more examples.

## What's next?

[Add another Executor](../guides/executors.md), [choose a multi-machine deployment](deployment.md), or [explore the WebUI](../guides/webui.md).

Working on Workgate itself? Start with [Development](../development.md).
