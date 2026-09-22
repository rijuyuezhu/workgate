# VPS deployment

This guide is the first production deployment target for the final
control/executor architecture: one small Linux VPS runs **one Workgate control
process**, and one or more executor machines connect outbound to it. It uses
ordinary systemd plus Caddy or nginx. It does not require PostgreSQL, Redis, a
message broker, an external object store, Kubernetes, or a distributed lock.

The control is public only through the reverse proxy. Executors keep machine
policy, workspace paths, shells, credentials, and local service ownership on
the executor machine. The control process does not need a workspace root and
the production `workgate control` entrypoint does not create one.

## 1. Prepare the control service account

Use a dedicated unprivileged account. The example below gives it a private
home under `/var/lib` so the normal Linux XDG layout stays useful and easy to
back up.

```bash
sudo useradd --system --create-home \
  --home-dir /var/lib/workgate \
  --shell /usr/sbin/nologin workgate

sudo chmod 700 /var/lib/workgate
sudo install -d -o workgate -g workgate -m 700 \
  /var/lib/workgate/.config/workgate \
  /var/lib/workgate/.local/state/workgate \
  /var/lib/workgate/.local/share/workgate \
  /var/lib/workgate/.cache
```

Install Workgate in a stable location that the service account can execute.
The units below use `/opt/workgate/bin/workgate`; replace that path with the
real installed executable on your VPS.

Create
`/var/lib/workgate/.config/workgate/control.yaml` as `workgate:workgate`
with mode `0600`:

```yaml
mode: mcp
host: 127.0.0.1
port: 8765

base_url: https://control.example.com
auth_mode: oauth
oauth_admin_pin: replace-with-a-long-random-secret

state_dir: /var/lib/workgate/.local/state/workgate
data_dir: /var/lib/workgate/.local/share/workgate
```

`host: 127.0.0.1` is also the built-in default. Do not bind the control
directly to a public interface for this deployment; terminate TLS at the
reverse proxy instead. Keep the config private because it can contain owner
authentication material.

The relevant Linux lifetime classes are:

| Lifetime | Default location for this service account | Contents |
| --- | --- | --- |
| config | `~/.config/workgate` | declarative configuration |
| state | `~/.local/state/workgate` | control trust, sessions, OAuth state, jobs, downloads, audit, locks |
| data | `~/.local/share/workgate` | durable control-owned payload data |
| cache | `~/.cache/workgate` | regenerable material |
| runtime | `$XDG_RUNTIME_DIR/workgate` | disposable process-lifetime files |

State and data directories are forced to owner-only permissions when the
production control starts. Sensitive state files, including executor
credentials on executor machines, are written owner-only. Do not copy state
files or bearer credentials into tickets, shell transcripts, or logs.

## 2. Run exactly one control with systemd

Create `/etc/systemd/system/workgate-control.service`:

```ini
[Unit]
Description=Workgate Control
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=workgate
Group=workgate
Environment=HOME=/var/lib/workgate
Environment=XDG_CONFIG_HOME=/var/lib/workgate/.config
Environment=XDG_STATE_HOME=/var/lib/workgate/.local/state
Environment=XDG_DATA_HOME=/var/lib/workgate/.local/share
Environment=XDG_CACHE_HOME=/var/lib/workgate/.cache
Environment=XDG_RUNTIME_DIR=/run/workgate
RuntimeDirectory=workgate
RuntimeDirectoryMode=0700
UMask=0077

ExecStart=/opt/workgate/bin/workgate control --config /var/lib/workgate/.config/workgate/control.yaml
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now workgate-control
sudo systemctl status workgate-control
sudo journalctl -u workgate-control -f
```

The production entrypoint takes a non-blocking single-writer lock under the
configured state root. Starting another `workgate control` against the same
state root fails instead of allowing two control processes to mutate the same
durable state.

Smoke-test the loopback listener before adding the proxy:

```bash
curl --fail http://127.0.0.1:8765/healthz
curl --fail http://127.0.0.1:8765/readyz
```

Both endpoints should return an `ok` response. Health/readiness intentionally
do not claim that every executor is online. Executor presence is separate
process-local state; use the Human UI **Executors** view for owner diagnostics,
including online status and the latest authenticated `last_seen_at`.

## 3. Put TLS and the public origin in front

The same public origin carries MCP/OAuth, pairing, executor long-poll traffic,
downloads, Human UI, and terminal WebSockets. Do not put executor endpoints on
a separate trust domain: bearer authentication is the identity boundary, not
the source IP.

### Caddy

Caddy handles WebSocket upgrade automatically. A minimal site is:

```caddyfile
control.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Do not add a short proxy read timeout: executor delivery uses long polling and
terminal streams use WebSockets. If you set request-body or rate limits, keep
them compatible with Workgate's configured upload/transfer limits and avoid
rate-limiting a healthy executor heartbeat or poll loop into permanent
failure.

### nginx

Put the `map` in nginx's `http` context:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

Then use a TLS virtual host like:

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
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        proxy_read_timeout 1d;
        proxy_send_timeout 1d;
    }
}
```

After TLS is live, verify the public health endpoint and open
`https://control.example.com/ui`. The production MCP endpoint is
`https://control.example.com/mcp`. Pairing and executor protocol requests
also use the same origin, including `/pair` and `/executor/v1/...`; terminal
streams include WebSocket routes under `/ui/...`, `/stream/...`, and
`/executor/v1/streams/...`.

## 4. Pair and run an executor

An executor should normally run as the OS user that owns the intended
workspace. Do not run it as root merely to simplify service setup. Give that
user an explicit config; in particular, never let the service manager's
incidental working directory decide machine authority.

For an executor user `alice`, for example:

```yaml
# /home/alice/.config/workgate/executor.yaml
workspace_root: /srv/workspaces/alice
state_dir: /home/alice/.local/state/workgate
data_dir: /home/alice/.local/share/workgate
```

Prepare the executor-owned workspace and private config/state/data roots, then
make the config owner-readable only:

```bash
sudo install -d -o alice -g alice -m 700 \
  /srv/workspaces/alice \
  /home/alice/.config/workgate \
  /home/alice/.local/state/workgate \
  /home/alice/.local/share/workgate \
  /home/alice/.cache
sudo chown alice:alice /home/alice/.config/workgate/executor.yaml
sudo chmod 600 /home/alice/.config/workgate/executor.yaml
```

Then pair once:

```bash
sudo -u alice HOME=/home/alice \
  /opt/workgate/bin/workgate executor connect \
  https://control.example.com \
  --config /home/alice/.config/workgate/executor.yaml \
  --name alice-vps-executor
```

Approve the pairing in the owner UI. The resulting
`state_dir/executor/profile.json` contains the long-lived executor bearer and
must remain private. Ordinary inactivity does not expire this trust.

A plain systemd unit can run the final executor entrypoint:

```ini
[Unit]
Description=Workgate Executor (alice)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=alice
Group=alice
Environment=HOME=/home/alice
Environment=XDG_CONFIG_HOME=/home/alice/.config
Environment=XDG_STATE_HOME=/home/alice/.local/state
Environment=XDG_DATA_HOME=/home/alice/.local/share
Environment=XDG_CACHE_HOME=/home/alice/.cache
Environment=XDG_RUNTIME_DIR=/run/workgate-executor-alice
RuntimeDirectory=workgate-executor-alice
RuntimeDirectoryMode=0700
WorkingDirectory=/
UMask=0077

ExecStart=/opt/workgate/bin/workgate executor run --config /home/alice/.config/workgate/executor.yaml
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

The explicit XDG environment and `RuntimeDirectory` give the service the same
private lifetime separation as an interactive executor. `WorkingDirectory=/`
is deliberate: the executor's resolved `workspace_root` comes from
executor-owned configuration, not service CWD.

The executor process itself owns transient control/network reconnect with
bounded exponential backoff and jitter. A temporary outage therefore keeps
the same executor process and profile alive; systemd's restart policy is for
process failure, not the normal reconnect loop. Revoked or replaced
credentials require explicit owner action and re-pairing rather than an
automatic trust reset.

This raw unit is only the simple-VPS deployment example. Higher-level
cross-platform executor service lifecycle commands are a separate concern and
do not change the control/executor authority split described here.

## 5. Reboot and upgrade behavior

A control restart restores durable product facts such as executor trust,
session records, approved OAuth clients, managed-job metadata, and persisted
payload/checkpoint state. It does **not** restore process-local executor
presence, in-flight ordinary RPC Futures, long polls, or open WebSockets.

That means an upgrade can use the ordinary sequence:

```bash
sudo systemctl stop workgate-control
# Install the new Workgate version.
sudo systemctl start workgate-control
curl --fail http://127.0.0.1:8765/readyz
```

Expect an ordinary call that was in flight during the stop to fail or be
interrupted. Do not infer completion from a disconnected request; inspect the
relevant durable job/session state and retry only when the operation semantics
permit it. Executors reconnect with their existing bearer after the control
returns. A machine that was offline for days does not need to re-pair solely
because of elapsed time.

## 6. Back up and restore

The backup boundary is the control's private **config, state, and durable data**.
Do not back up the executor workspace through the control deployment, and do
not back up cache or `XDG_RUNTIME_DIR` as if they were durable product state.

For the simple VPS milestone, the safest schema-independent backup is a cold
archive of:

- `/var/lib/workgate/.config/workgate/control.yaml`;
- `/var/lib/workgate/.local/state/workgate`;
- `/var/lib/workgate/.local/share/workgate`.

The state root contains restart-critical executor trust/verifiers, session
facts, OAuth signing/client state, download/job metadata and related
checkpoints. The data root contains control-owned payloads that may still be
referenced by durable operations or links. Retained audit history may be kept
for policy/compliance needs; it is not required to recreate live executor
presence. Never treat pending in-memory command Futures, active long polls,
WebSockets, cache, or runtime lock files as backup state.

Take a consistent archive with the control stopped. The control's
`control/run.lock` file only carries process-local lock ownership, so exclude
it from the archive:

```bash
sudo systemctl stop workgate-control
sudo tar --xattrs --acls -C /var/lib/workgate \
  --exclude=.local/state/workgate/control/run.lock \
  -czf /root/workgate-backup.tgz \
  .config/workgate/control.yaml \
  .local/state/workgate \
  .local/share/workgate
sudo systemctl start workgate-control
```

Protect the archive like a credential backup: the control state is sufficient
to authenticate previously paired executors.

To restore, install a compatible Workgate version, stop the control, restore
the three paths with owner `workgate:workgate`, keep directories at `0700`
and sensitive files at `0600`, then start the service and check
`/readyz`. Existing executors whose profiles were also preserved should
reconnect with the same bearer. If control-side trust was lost, or an executor
lost its private profile, pair that executor again instead of manufacturing a
credential by hand.

## Operational checklist

Before calling the VPS deployment healthy, verify:

1. exactly one control process owns the configured state root;
2. the application binds only to `127.0.0.1` and public traffic reaches it
   through TLS at Caddy/nginx;
3. `/healthz` and `/readyz` succeed locally and through the proxy;
4. an executor can pair, become online, survive a control restart, and
   reconnect without re-pairing;
5. a terminal WebSocket remains usable through the proxy;
6. stopping the control interrupts an ordinary in-flight call rather than
   falsely reporting success;
7. restored state preserves executor trust and durable session/product facts;
8. the deployment has no external database, broker, object store, or
   distributed locking dependency.
