# Quickstart

This gets Workgate running locally with the smallest supported topology:
**standalone mode**. Standalone starts separate control and executor processes,
uses the real executor protocol over loopback, and does not require Internet
access after installation.

For a public endpoint or a multi-machine deployment, first finish this local
smoke test, then choose a deployment from [Choose a deployment](deployment.md).

## 1. Install

```bash
git clone https://github.com/rijuyuezhu/workgate.git
cd workgate
uv sync
```

You also need the machine tools you intend the executor to use, such as Git,
`ripgrep`, compilers, package managers, or `tmux`.

## 2. Create the YAML config

Create the normal Workgate config directory:

```bash
mkdir -p ~/.config/workgate
```

Then create `~/.config/workgate/config.yaml`:

```yaml
default_workdir: /absolute/path/to/your/project
```

Use an absolute default working directory.

The generated [Configuration reference](../reference/configuration.md) lists all
settings. Environment variables and CLI flags remain available as overrides,
but YAML is the normal durable setup.

## 3. Start Workgate

```bash
uv run workgate standalone
```

The local control endpoint is `http://127.0.0.1:8765`; the MCP endpoint is:

```text
http://127.0.0.1:8765/mcp
```

In another terminal, check the service:

```bash
curl --fail http://127.0.0.1:8765/healthz
curl --fail http://127.0.0.1:8765/readyz
```

Standalone keeps OAuth enabled. On first launch it also establishes the local
executor identity and, when needed, creates a private OAuth approval PIN. The
command prints the path to that PIN rather than the secret itself.

## 4. Try a first task

Connect a local MCP client to the endpoint above, then try:

```text
Start a session in my configured default workdir, inspect the repository and its
instruction files, then summarize the environment and Git status. Do not change
files yet.
```

For the browser interface, open `/ui` on the same control origin.

## 5. Go beyond one machine

- Need a stable public control on a Linux server? Use [VPS deployment](vps.md).
- Keeping control local but need public HTTPS? Use [Cloudflare Tunnel](cloudflare-tunnel.md).
- Connecting ChatGPT? After you have a public MCP URL, follow [ChatGPT connector](chatgpt-connector.md).

For standalone trust, state ownership, restart behavior, and recovery details,
see [Standalone local mode](standalone.md).
