# Cloudflare Tunnel

A remote MCP client needs a public HTTPS endpoint. Cloudflare Tunnel can forward a public hostname to the local `workgate control` service while the control's own OAuth flow protects `/mcp`.

## Create the tunnel and obtain its token

Follow Cloudflare's [Create a tunnel in the dashboard](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/) guide:

1. sign in to Cloudflare and open **Networking → Tunnels**;
2. create a remotely managed tunnel and give it a recognizable name;
3. copy the installation command shown for the connector; the long `eyJ...` value after `--token` is the tunnel token; and
4. add a **Published application** route for a hostname such as `mcp.example.com`.

The token authorizes a connector to run this tunnel, so keep it private. Cloudflare also documents how to [retrieve a tunnel token later](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/remote-tunnel-permissions/#get-the-tunnel-token).

## Choose the service target

For the [local source setup](quickstart.md#3-smoke-test-locally), set the published application's service URL to:

```text
http://127.0.0.1:8765
```

Add the public origin and copied token to `.env`:

```env
CLOUDFLARE_TUNNEL_TOKEN=eyJ...
WORKGATE_BASE_URL=https://mcp.example.com
```

`WORKGATE_BASE_URL` is the origin only. Do not append `/mcp`. Return to [Quickstart](quickstart.md#4-create-and-start-the-tunnel) to start the service and tunnel.

## Verify

From another network, check:

```bash
curl -i https://mcp.example.com/healthz
```

Use this URL in the MCP client:

```text
https://mcp.example.com/mcp
```

## Common mistakes

- The connector URL does not end in `/mcp`.
- `WORKGATE_BASE_URL` incorrectly includes `/mcp`.
- The tunnel target does not point to the local control address and port.
- The public hostname changed but `.env` still contains the old origin.
- OAuth is disabled on a public hostname.

Continue with [ChatGPT connector](chatgpt-connector.md) or see [Troubleshooting](../troubleshooting.md).
