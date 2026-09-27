# Cloudflare Tunnel

Use Cloudflare Tunnel when the Workgate **control** runs on your own machine or
VPS but needs a public HTTPS origin. The tunnel is an edge adapter: it does not
change control/executor trust or move machine execution into Cloudflare.

For a server with a normal public reverse proxy, use [VPS deployment](vps.md)
instead.

## 1. Configure Workgate

Keep Workgate's durable configuration in its normal YAML file. For example,
`~/.config/workgate/config.yaml`:

```yaml
mode: mcp
host: 127.0.0.1
port: 8765
base_url: https://mcp.example.com
auth_mode: oauth
oauth_admin_pin: replace-with-a-long-random-secret
```

`base_url` is the public origin only; do not append `/mcp`.

Start the control normally:

```bash
workgate control
```

Verify the loopback service before adding the tunnel:

```bash
curl --fail http://127.0.0.1:8765/healthz
```

## 2. Create the tunnel

Follow Cloudflare's
[remote tunnel guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)
to create a tunnel and publish `mcp.example.com` to:

```text
http://127.0.0.1:8765
```

The tunnel token belongs to **cloudflared**, not to Workgate configuration.
Keep it in the secret mechanism used to launch cloudflared rather than adding it
to `config.yaml`.

For an interactive smoke test, use the connector command Cloudflare provides,
for example:

```bash
cloudflared tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN"
```

The repository still contains `scripts/run-with-cloudflare-tunnel.sh` as a
development helper, but it is not the canonical production lifecycle. A
first-class managed cloudflared lifecycle is tracked separately.

## 3. Verify the public origin

From another network:

```bash
curl --fail https://mcp.example.com/healthz
```

The public MCP endpoint is:

```text
https://mcp.example.com/mcp
```

Pair executors against `https://mcp.example.com`; executor traffic, MCP, OAuth,
and the Human UI all use the same control origin.

## Common mistakes

- putting `/mcp` in `base_url`;
- publishing the tunnel to the wrong local port;
- changing the public hostname without updating `base_url`;
- exposing a public origin with `auth_mode: none`;
- treating the cloudflared token as a Workgate secret/config field.

Continue with [ChatGPT connector](chatgpt-connector.md) or
[Troubleshooting](../troubleshooting.md).
