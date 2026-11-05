# VPS deployment

Use a VPS for a public **Control** endpoint. Keep Executors on the computers where your projects and tools live.

## 1. Install Control

Create or choose a regular Linux user to own Workgate. Install the **Control** role using [Install Workgate](install-options.md#other-roles).

Use your public HTTPS hostname in the Control install command's **--base-url** setting, for example:

~~~text
https://control.example.com
~~~

The installer stores this origin in Control's configuration and initializes OAuth approval. Keep Control listening on loopback; the reverse proxy provides public HTTPS.

The source installer creates the native user service. For a server expected to stay online after logout, configure its systemd user manager (including lingering when needed) and check that the service is running. Use:

~~~sh
workgate control status-service
curl --fail http://127.0.0.1:8765/readyz
~~~

## 2. Publish HTTPS

Your proxy must forward the same origin to Control's local port (default **8765**). Use a certificate for your hostname.

### Caddy

~~~caddyfile
control.example.com {
    reverse_proxy 127.0.0.1:8765
}
~~~

### nginx

Put this map in nginx's **http** context:

~~~nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
~~~

Configure the HTTPS server:

~~~nginx
server {
    listen 443 ssl;
    server_name control.example.com;
    # Configure your certificate and private key here.
    client_max_body_size 8m;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 1d;
        proxy_send_timeout 1d;
    }
}
~~~

The nginx request-body limit gives file transfers and other tool results room to pass through the proxy. Control Relay uploads use separate bounded HTTP requests (up to 64 MiB), reducing request size after HTTP 413; the proxy no longer needs to accept an entire large file in one request. Presigned Object Store uploads are a separate path and still use a single PUT.

Alternatively, publish Control with a [Cloudflare Tunnel](https://github.com/rijuyuezhu/workgate/tree/main/deploy/cloudflared).

Verify the public endpoint:

~~~sh
curl --fail https://control.example.com/healthz
~~~

Your MCP URL is **https://control.example.com/mcp** and the WebUI is **https://control.example.com/ui**.

## 3. Connect your computers

On each workspace machine, install the **Executor** role using its Control HTTPS URL. Approve the device in the Control WebUI.

Then [connect ChatGPT](chatgpt-connector.md) or [another MCP client](mcp-clients.md) to the public MCP URL.

## 4. Operate and back up

- **Control unavailable:** check the local /readyz endpoint and Control service status before checking the proxy.
- **Executor offline:** check the Executor machine's status and logs, then its Control URL and pairing.
- **Tool result rejected with HTTP 413:** check the reverse proxy's request-body size.
- **Storage usage:** Control stores state, audit data and file snapshots. Size its private data directory for your workload.

Back up Control's private **config**, **state** and **data** directories together, with a consistent snapshot. Back up each Executor's private pairing profile separately. Protect these backups as credentials.

To upgrade a managed source installation, use the [source upgrade guide](https://github.com/rijuyuezhu/workgate/blob/main/deploy/source/README.md). For detailed settings and paths, see [Configuration](../reference/configuration.md).
