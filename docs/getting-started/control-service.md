# Manage a Control service

For a new server, install the **Control** role using [Install Workgate](install-options.md). For public HTTPS, follow [VPS deployment](vps.md).

## Existing development checkout

From a prepared source checkout, initialize Control's config and install its user service:

~~~sh
uv run workgate control init-config
uv run workgate control install-service
uv run workgate control status-service
~~~

Control keeps its configuration and authentication state in its user-owned directories. Use the generated admin approval PIN in your private config when approving users.

To operate an existing service:

~~~sh
uv run workgate control restart-service
uv run workgate control stop-service
uv run workgate control start-service
~~~

For custom config paths and other options, use **workgate control --help**.

## Diagnose a problem

- Service cannot start: check its native systemd user unit, Windows Task Scheduler task, or corresponding log.
- Public URL fails: check local Control health first, then the HTTPS reverse proxy.
- Pairing approval fails: verify Control's public origin and the saved OAuth config.

See [Troubleshooting](../troubleshooting.md) and [Configuration](../reference/configuration.md).
