# VPS deployment

Run one Workgate **control** on a Linux VPS and let executors connect outbound
to it. This guide uses systemd plus Caddy or nginx; no external database or
message broker is required.

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

Create `/var/lib/workgate/.config/workgate/config.yaml` with owner
`workgate:workgate` and mode `0600`:

```yaml
base_url: https://control.example.com
auth_mode: oauth
oauth_admin_pin: replace-with-a-long-random-secret
```

Control binds `127.0.0.1:8765` by default. Keep it on loopback and let the
reverse proxy own public TLS.

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

The same public origin serves MCP/OAuth, the Human UI, executor traffic,
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

After TLS is live:

```bash
curl --fail https://control.example.com/healthz
```

The MCP endpoint is `https://control.example.com/mcp`; the Human UI is
`https://control.example.com/ui`.

## 4. Pair an executor

Run the executor as the OS user that should own its files and processes. For user `alice`,
create `~/.config/workgate/executor/config.yaml`:

```yaml
default_workdir: /srv/workspaces/alice
```

Prepare the workspace and config directory:

```bash
sudo install -d -o alice -g alice -m 700 \
  /srv/workspaces/alice \
  /home/alice/.config/workgate/executor
sudo chown alice:alice /home/alice/.config/workgate/executor/config.yaml
sudo chmod 600 /home/alice/.config/workgate/executor/config.yaml
```

Pair once and approve the request in the owner UI:

```bash
sudo -u alice HOME=/home/alice \
  /opt/workgate/bin/workgate executor connect \
  https://control.example.com \
  --name alice-vps-executor
```

Then run it under systemd:

```ini
[Unit]
Description=Workgate Executor (alice)
After=network.target

[Service]
Type=simple
User=alice
Group=alice
Environment=HOME=/home/alice
WorkingDirectory=/
UMask=0077
ExecStart=/opt/workgate/bin/workgate executor run
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

The executor discovers `~/.config/workgate/executor/config.yaml` and uses its
private default state root under `~/.local/state/workgate/executor-runtime`.
`default_workdir` is the fallback anchor for relative session workdirs and
sessionless executor paths. Filesystem access is governed by the executor OS account.

Temporary network or control outages reconnect with the saved executor profile;
ordinary downtime does not require pairing again.

## 5. Upgrade and back up

For an upgrade, stop control, install the new version, start it again, and check
`/readyz`. Executors reconnect with their existing credentials. An in-flight
ordinary call may be interrupted.

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
