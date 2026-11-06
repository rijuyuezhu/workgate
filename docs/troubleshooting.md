# Troubleshooting

Start with the symptom, then check the component closest to it.

## Can't connect to Workgate

1. Open Control's local WebUI at **http://127.0.0.1:8765/ui** on the Control machine.
2. Check **http://127.0.0.1:8765/healthz** or the Control service status.
3. If local access works but the public address fails, check the HTTPS proxy and public hostname.
4. Check the MCP URL ends in **/mcp**. ChatGPT needs a reachable HTTPS endpoint.

See [Connect ChatGPT](getting-started/chatgpt-connector.md) or [VPS deployment](getting-started/vps.md).

## OAuth or approval fails

Check the Control **base_url** matches its public HTTPS origin and that the correct owner account is approving access. If you changed the hostname, check the running Control configuration and its configured OAuth settings.

## Executor is offline

On the Executor computer:

~~~sh
workgate executor status
workgate executor logs
~~~

Check that the Control URL is reachable and the saved Executor pairing is still valid. An Executor normally reconnects after temporary outages. If it was revoked, use the [Executor pairing guide](guides/executors.md) to pair again.

## A tool times out or fails

Check the session's selected Executor and whether the requested tool is available there. For long-running commands, use a Workgate Job or persistent shell.

For a **413** result-upload error behind a reverse proxy, inspect the proxy's request-body size setting. See [VPS deployment](getting-started/vps.md).

If the command finishes but its result is delayed while Control is unreachable, the Executor saves pending results privately and retries them after reconnecting. Check **workgate executor logs** for result delivery errors. A result for an expired Control request cannot be reattached to a new caller; retry that operation only after confirming whether it already took effect.

## Browser or desktop tools are unavailable

Check Chromium and the relevant desktop environment on the **Executor machine**. GUI automation requires a logged-in accessible graphical session and OS permissions.

See [Executors](guides/executors.md).

## Missing commands or dependencies

The managed Workgate runtime installs its Python dependencies. Tools invoked by your project—such as Git, compilers and tmux—must exist on the Executor host.

## Audit entries seem missing

Check the selected Control, session/task, time range and permissions. See [Audit](guides/audit-log.md).

For command options and settings, use [CLI help](reference/cli.md) and [Configuration](reference/configuration.md).
