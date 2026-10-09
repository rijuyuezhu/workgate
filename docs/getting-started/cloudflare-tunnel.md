# Cloudflare Tunnel

Use Cloudflare Tunnel when a self-hosted Workgate **control** needs a public HTTPS
origin without accepting inbound Internet traffic directly. cloudflared is an
optional edge process; it does not own Workgate configuration, executor trust,
or machine execution.

For a VPS with a normal public reverse proxy, use [VPS deployment](vps.md)
instead.

## 1. Configure Workgate

Keep Workgate configuration in its normal YAML file. For example,
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

Start control and verify the loopback origin before involving Cloudflare:

```bash
workgate control
curl --fail http://127.0.0.1:8765/healthz
```

## 2. Create the tunnel

Create a remotely managed tunnel in the Cloudflare dashboard using
[Cloudflare's tunnel setup guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/),
then publish `mcp.example.com` to:

```text
http://127.0.0.1:8765
```

The tunnel token belongs to **cloudflared**, not to Workgate. Do not put it in
`config.yaml`, Workgate state, or `.env`.

## 3. Smoke-test in the foreground

Use cloudflared 2025.4.0 or newer; that release line supports
[`--token-file`](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/)
so the credential can stay in a private file instead of an environment
variable or command-line token:

```bash
cloudflared tunnel --no-autoupdate run --token-file /path/to/private/tunnel.token
```

Stop the foreground process with Ctrl-C. Workgate control keeps running.

## 4. Run cloudflared as a Linux service

Production Linux deployments should run cloudflared independently from Workgate
under systemd. The repository provides the canonical unit and setup notes in
`deploy/cloudflared/`.

The supplied unit:

- runs cloudflared as a dedicated `cloudflared` account;
- reads the tunnel token from `/etc/cloudflared/workgate.token`;
- restarts cloudflared after failures;
- logs through journald;
- does not start, stop, configure, or hold credentials for Workgate.

After installing the unit as described in the repository file
`deploy/cloudflared/README.md`, operate it with:

```bash
sudo systemctl status workgate-cloudflared.service
sudo systemctl restart workgate-cloudflared.service
sudo systemctl stop workgate-cloudflared.service
journalctl -u workgate-cloudflared.service
```

Restart cloudflared after replacing the tunnel token. Restarting Workgate is not
required for token rotation.

## Failure behavior

The two services fail independently:

- if cloudflared is down, the public hostname is unavailable while local Workgate
  control continues running;
- if Workgate control is down, cloudflared can remain connected but the loopback
  origin is unavailable;
- temporary network loss is handled by cloudflared and Workgate executor
  reconnect behavior rather than by one process supervising the other.

## Verify the public origin

From another network:

```bash
curl --fail https://mcp.example.com/healthz
```

The MCP endpoint is:

```text
https://mcp.example.com/mcp
```

Pair executors against `https://mcp.example.com`; executor traffic, MCP, OAuth,
and the WebUI all use the same control origin.

## Common mistakes

- putting `/mcp` in `base_url`;
- publishing the tunnel to the wrong loopback port;
- changing the public hostname without updating `base_url`;
- exposing a public origin with `auth_mode: none`;
- treating the cloudflared tunnel token as a Workgate setting or state file;
- coupling Workgate and cloudflared into one wrapper process.

Continue with [ChatGPT connector](chatgpt-connector.md) or
[Troubleshooting](../troubleshooting.md).
