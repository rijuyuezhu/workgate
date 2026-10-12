# Quickstart

Workgate runs a **Control** process serving MCP, OAuth and the WebUI and an
**Executor** process owning files, shells and sessions. They use the same
protocol whether on one host or two.

## 1. Install

```bash
git clone https://github.com/rijuyuezhu/workgate.git
cd workgate
uv sync
```

Install the host tools you intend to use (such as Git, `ripgrep`, compilers
and `tmux`).

## 2. Configure and pair once

Use the normal role-specific files on Linux:

```text
~/.config/workgate/config.yaml           # Control
~/.config/workgate/executor/config.yaml  # Executor
```

Set a strong `oauth_admin_pin` in the Control configuration for approval.
Set `default_workdir: /absolute/path/to/project` in the Executor
configuration for your preferred working directory.

Start `uv run workgate control`, then in another terminal:

```bash
uv run workgate executor connect http://127.0.0.1:8765 --name local
```

Approve the device pairing in the [WebUI](http://127.0.0.1:8765/ui)
using your Control admin PIN. Stop the Control process.

## 3. Run locally

```bash
uv run workgate colocate
```

This is a small launcher for the **ordinary** Control and Executor child
processes, using their normal configuration and state directories. It does
not perform automatic pairing or introduce a special local state layout.

MCP: `http://127.0.0.1:8765/mcp`
WebUI: `http://127.0.0.1:8765/ui`

Connect a local MCP client and try:

> Start a session in my configured default workdir, inspect Git status,
> and do not change any files.

For how the two processes share a host, see
[Colocated local mode](colocate.md). For other hosts or public endpoints,
see [Choose a deployment](deployment.md) and [VPS deployment](vps.md).
