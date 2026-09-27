# cloudflared lifecycle

This directory contains the supported Linux service recipe for exposing a
self-hosted Workgate control through a remotely managed Cloudflare Tunnel.

Workgate and cloudflared have independent lifecycles:

- Workgate reads its normal `config.yaml` and listens on loopback.
- cloudflared owns the tunnel credential and forwards the configured public
  hostname to that loopback origin.
- Stopping or restarting either process never changes Workgate executor trust or
  state.

The service template uses `--token-file`, which requires cloudflared 2025.4.0
or newer.

## Prepare the token

Ensure a dedicated service account exists, then create a private token file:

```bash
getent passwd cloudflared >/dev/null || \
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin cloudflared
sudo install -d -o root -g cloudflared -m 0750 /etc/cloudflared
sudo install -o root -g cloudflared -m 0640 /dev/null /etc/cloudflared/workgate.token
sudoedit /etc/cloudflared/workgate.token
```

Paste only the remotely managed tunnel token into that file. The token is a
cloudflared credential, not a Workgate setting.

If cloudflared is not installed as `/usr/bin/cloudflared`, adjust `ExecStart`
in the unit before installing it.

## Install the service

```bash
sudo install -m 0644 deploy/cloudflared/workgate-cloudflared.service \
  /etc/systemd/system/workgate-cloudflared.service
sudo systemctl daemon-reload
sudo systemctl enable --now workgate-cloudflared.service
```

The Cloudflare dashboard should publish the Workgate hostname to
`http://127.0.0.1:8765` (or the loopback host/port configured for control).

## Operate it

```bash
sudo systemctl status workgate-cloudflared.service
sudo systemctl restart workgate-cloudflared.service
sudo systemctl stop workgate-cloudflared.service
journalctl -u workgate-cloudflared.service
```

Replacing the token file requires a service restart. A cloudflared failure makes
the public hostname unavailable but leaves the local Workgate control running.
A Workgate control outage leaves the tunnel process running while the origin is
unavailable. Both services use their own restart policy and logs.

The unit disables cloudflared self-updates. Update the cloudflared package or
binary separately, then restart the service.

For the complete Workgate-side configuration and verification flow, see
`docs/getting-started/cloudflare-tunnel.md`.
