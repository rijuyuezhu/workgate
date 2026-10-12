# VPS deployment

Run only Workgate **control** on a Linux VPS; pair executors on the machines
that should own files, shells, and jobs. The VPS needs no local executor.
Use systemd with Caddy, nginx, or an independent Cloudflare Tunnel; no
database or message broker is required.

## 1. Prepare control

Create an unprivileged service account with a private home:

```bash
sudo useradd --system --create-home \
  --home-dir /var/lib/workgate \
  --shell /usr/sbin/nologin workgate
sudo chmod 700 /var/lib/workgate
sudo install -d -o workgate -g workgate -m 700 \
  /var/lib/workgate/.config/workgate
```

Install Workgate somewhere the service account can execute it. The examples
below use `/opt/workgate/bin/workgate`.

Create `/var/lib/workgate/.config/workgate/config.yaml` (the service
account's `~/.config/workgate/config.yaml`) with owner `workgate:workgate`
and mode `0600`:

```yaml
mode: mcp
host: 127.0.0.1
port: 8765
base_url: https://control.example.com
auth_mode: oauth
oauth_admin_pin: replace-with-a-long-random-secret
file_download_max_file_bytes: 67108864  # 64 MiB snapshot limit
```

Replace the sample admin PIN with a long random secret before starting control.
The host/port settings are also the defaults. Keep control on loopback and
let the proxy own public TLS. `base_url` must be the public HTTPS origin,
without `/mcp`; do not put tunnel credentials in Workgate's YAML.
The file-link size limit is recommended for a small VPS (default: unlimited).

## 2. Run control with systemd

Create `/etc/systemd/system/workgate-control.service`:

```ini
[Unit]
Description=Workgate Control
After=network.target

[Service]
Type=simple
User=workgate
Group=workgate
Environment=HOME=/var/lib/workgate
UMask=0077
ExecStart=/opt/workgate/bin/workgate control
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

Enable it and verify the loopback endpoint:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now workgate-control
sudo systemctl status workgate-control
curl --fail http://127.0.0.1:8765/readyz
```

Use `journalctl -u workgate-control` for service logs.

## 3. Add public HTTPS

The same public origin serves MCP/OAuth, the WebUI, executor traffic,
downloads, and terminal WebSockets.

### Caddy

```caddyfile
control.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

### nginx

Put this `map` in nginx's `http` context:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

Then proxy the TLS virtual host:

```nginx
server {
    listen 443 ssl;
    server_name control.example.com;

    # Configure ssl_certificate / ssl_certificate_key normally.

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
```

Avoid short proxy read timeouts: executor delivery uses long polling and
terminal attachments use WebSockets.

### Cloudflare Tunnel

Instead of Caddy or nginx, a separately managed `cloudflared` service can
publish `https://control.example.com` to `http://127.0.0.1:8765`. Keep
its token in `cloudflared`'s private credential file, not in Workgate YAML.
Use the [cloudflared systemd recipe](https://github.com/rijuyuezhu/workgate/blob/main/deploy/cloudflared/README.md).
Ensure the public hostname allows MCP/OAuth, WebUI, executor long polling,
downloads, and terminal WebSockets to reach the same control origin.
This remains **self-hosted control**, not Cloudflare Workers hosted control
(the deferred #252 proposal).

After public HTTPS is live:

```bash
curl --fail https://control.example.com/healthz
```

The MCP endpoint is `https://control.example.com/mcp`; the WebUI is
`https://control.example.com/ui`.

## 4. Pair an outbound executor

On **each executor machine** (for example, your workstation or a GPU host),
install the matching Workgate version and create its own
`~/.config/workgate/executor/config.yaml`:

```yaml
default_workdir: /home/alice/projects
```

Use an existing absolute work directory and keep the config file private.
Run as the OS user who should own machine operations:

```bash
workgate executor connect https://control.example.com --name workstation
workgate executor run
```

Approve the device-code pairing request in the public WebUI. `run` remains
in the foreground; for a persistent user service, use
`workgate executor install-service` instead (see
[Executors](../guides/executors.md)). Executors connect outbound and reconnect
after ordinary network or control outages without pairing again.

## 5. Control storage and diagnostics

`create_file_link` fetches a regular file from its session's executor and
persists an **immutable snapshot on control**. Its bearer download URL
continues working while the executor is offline, until expiry or revocation.
The sample `file_download_max_file_bytes` limits each snapshot to 64 MiB;
allow enough private space under control's `data_dir` and use short link TTLs.
Cross-executor `session_copy` may also use control storage for its durable
relay; budget VPS disk and bandwidth accordingly. An optional S3 route can
avoid routing those bytes through control for large transfers.

Read control logs with `journalctl -u workgate-control`, and tunnel logs with
`journalctl -u workgate-cloudflared` if applicable. Executor failures are
logged **on the executor machine**, via foreground output or
`workgate executor logs` for managed services. A control-only VPS has no
executor machine/terminal logs.

## 6. Upgrade and back up

For an upgrade, stop control, install the new version, start it again, and check
`/readyz`. **Control and executor runtime versions must match** for commands
to run. Update externally installed executors with their own package/source
tooling; supported control-managed standalone executors use the verified update
flow described in [Executors](../guides/executors.md). Executors reconnect
with existing credentials. In-flight operations may be interrupted.

For the default service account layout, back up:

- `/var/lib/workgate/.config/workgate/config.yaml`;
- `/var/lib/workgate/.local/state/workgate`;
- `/var/lib/workgate/.local/share/workgate`.

Take a consistent backup with control stopped. Protect it like credential
material: control state contains the trust needed to authenticate paired
executors.

If you also need to preserve an executor's identity, back up that executor's
private state separately. Restore matching control/executor state together;
otherwise pair the executor again.

For path ownership and migration details, see
[Configuration](../reference/configuration.md). For executor pairing and
revocation, see [Executors](../guides/executors.md).
