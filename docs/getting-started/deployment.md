# Choose a deployment

Workgate has two runtime roles:

- **control** exposes MCP, OAuth, the WebUI, pairing, and orchestration;
- **executor** owns working directories, files, shells, processes, PTYs, and machine-local integrations.

Choose where those roles run. The protocol and trust model stay the same.

| Deployment | Use it when | Start here |
| --- | --- | --- |
| Standalone local | one machine, offline/local use, simplest setup | [Quickstart](quickstart.md) |
| Separate local processes | you want to run and debug each role yourself | [Run the roles separately](#run-the-roles-separately) |
| VPS control + executors | you want one stable public control endpoint | [VPS deployment](vps.md) |
| Self-hosted control through Cloudflare Tunnel | control stays on your machine/VPS but needs public HTTPS | [Cloudflare Tunnel](cloudflare-tunnel.md) |

## Configuration

YAML is the normal durable configuration surface. On Linux the default files are:

```text
~/.config/workgate/config.yaml           # control/general config
~/.config/workgate/executor/config.yaml  # executor config
```

Environment variables and CLI flags are overrides. Control and executor also
use separate state defaults even when they run as the same OS user. See
[Configuration](../reference/configuration.md) for precedence, platform paths,
and role ownership.

## Run the roles separately

For a same-host setup without the standalone supervisor, start control first:

```bash
workgate control
```

Pair the executor once against that control, approve it in `/ui`, then run it:

```bash
workgate executor connect http://127.0.0.1:8765 --name local-executor
workgate executor run
```

Use [Standalone local mode](standalone.md) when you want one command to manage
both processes. For another machine, follow [Executors](../guides/executors.md).
