# Quickstart

This guide runs a Workgate **control** locally, exposes it through Cloudflare
Tunnel, pairs a local **executor**, and connects ChatGPT to the public `/mcp`
endpoint.

## Prerequisites

You need:

- a Linux or macOS host with `git`, `uv`, Python 3.14+, and `cloudflared`;
- a project directory that the executor may read and modify—use an existing
  checkout, or create an empty directory such as
  `mkdir -p ~/Projects/my-project`;
- the executor-side tools you want to use, such as `tmux`, `ripgrep`, Git,
  compilers, or package managers;
- a Cloudflare account and a domain managed by Cloudflare—follow Cloudflare's
  [domain onboarding guide](https://developers.cloudflare.com/fundamentals/manage-domains/add-site/)
  when starting with a domain from another registrar; and
- a ChatGPT plan and role that can add a custom MCP app.

!!! note "ChatGPT plan availability"
    Check OpenAI's current availability notes before setup. Product availability
    can change independently of Workgate.

[Step 4](#4-create-and-start-the-tunnel) shows where to create the tunnel and
obtain its token. The helper used here owns only the control process and
Cloudflare connector; the executor remains a separate process.

## 1. Install

```bash
git clone https://github.com/rijuyuezhu/workgate.git
cd workgate
uv sync
cp .env.example .env
```

Keep the checkout in a stable location if you plan to run it as a service.

## 2. Configure the control

Set the control-facing values in `.env`:

```env
WORKGATE_MODE=mcp
WORKGATE_HOST=127.0.0.1
WORKGATE_PORT=8765
WORKGATE_BASE_URL=https://your-public-host.example.com
WORKGATE_AUTH_MODE=oauth
WORKGATE_OAUTH_ADMIN_PIN=replace-with-a-long-random-pin
CLOUDFLARE_TUNNEL_TOKEN=your-cloudflare-tunnel-token
```

`WORKGATE_BASE_URL` is the public origin without `/mcp`. The control binds
to loopback and does not own a workspace root, shell policy, command denylist,
or full-control setting. Workgate state uses the platform-native user state
directory by default; keep it private because it contains credentials and
activity data.

Leave the example `CLOUDFLARE_TUNNEL_TOKEN` value in place for now. You will
replace it with the token copied from Cloudflare in
[Step 4](#4-create-and-start-the-tunnel); the detailed dashboard flow is in
[Cloudflare Tunnel](cloudflare-tunnel.md).

For every setting and precedence rule, see
[Configuration](../reference/configuration.md). For the role-specific CLI
surface, use `workgate control --help`.

## 3. Smoke-test locally

```bash
set -a
. ./.env
set +a
uv run workgate control --mode mcp
```

In another terminal:

```bash
curl -i http://127.0.0.1:8765/healthz
curl -i http://127.0.0.1:8765/readyz
```

Successful health and readiness checks confirm that the local control process
is running. Stop it with Ctrl+C before continuing: the tunnel helper starts its
own control process on the same address.

## 4. Create and start the tunnel

Follow [Cloudflare Tunnel](cloudflare-tunnel.md) to:

1. create a remotely managed tunnel in the Cloudflare dashboard;
2. add a published application route from your public hostname to
   `http://127.0.0.1:8765`;
3. copy the tunnel token into `CLOUDFLARE_TUNNEL_TOKEN` in `.env`; and
4. set `WORKGATE_BASE_URL` to the same public HTTPS origin.

Then start the control and tunnel together:

```bash
scripts/run-with-cloudflare-tunnel.sh
```

The public MCP endpoint is:

```text
https://your-public-host.example.com/mcp
```

Keep this terminal running while you pair the executor.

## 5. Pair and run an executor

Create a separate executor config. Machine policy belongs here, not in the
control command:

```yaml
# executor.yaml
workspace_root: /path/to/your/workspace
allow_full_control: false
```

In another terminal, pair this machine with the public control origin:

```bash
uv run workgate executor connect \
  https://your-public-host.example.com \
  --config ./executor.yaml \
  --name local-executor
```

Open the verification URL printed by the command, enter/confirm the short code,
and approve the executor. Pairing stores a private long-lived executor profile.

Then start the executor:

```bash
uv run workgate executor run --config ./executor.yaml
```

The executor connects outbound to the control. Temporary control or network
outages reuse the same saved credential; normal inactivity does not require
re-pairing.

## 6. Keep the control running

For a persistent Linux user service around the source-checkout helper, create
`~/.config/systemd/user/workgate-control-tunnel.service` with the stable
checkout as its working directory:

```ini
[Unit]
Description=Workgate control and Cloudflare Tunnel
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/YOU/Code/workgate
ExecStart=/usr/bin/env bash scripts/run-with-cloudflare-tunnel.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Reload the user manager, enable the service, and inspect its logs:

```bash
systemctl --user daemon-reload
systemctl --user enable --now workgate-control-tunnel.service
journalctl --user -u workgate-control-tunnel.service -f -n 200
```

This unit does not own the executor lifecycle. For a production Linux VPS and a
plain executor systemd example, see [VPS deployment](vps.md). Higher-level
cross-platform executor service management is intentionally separate from this
quickstart.

## 7. Connect ChatGPT

Add a custom MCP connector using the public `/mcp` URL, then complete OAuth
approval with the admin PIN from `.env`. Use a client mode that exposes the
tool surface you need.

See [ChatGPT connector](chatgpt-connector.md) for the exact UI flow.

## 8. Try a first task

```text
Use workgate. Start a session in my project workspace, inspect the repository
and its instruction files, then summarize the environment and Git status. Do
not change files yet.
```

Continue with [Common workflows](../guides/common-workflows.md), or open the
browser interface described in [Human interface](../guides/human-interface.md).
