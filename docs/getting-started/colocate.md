# Colocated local mode

`workgate colocate` launches the **normal Control and Executor commands as two
child processes** on the same host. It does not create a separate state store,
pair executors, modify credentials, or implement a different runtime protocol.

## First-time pairing

Configure the normal Control settings in `~/.config/workgate/config.yaml`,
including an `oauth_admin_pin` for authorization, and the Executor settings
in `~/.config/workgate/executor/config.yaml` (for example,
`default_workdir`). Then pair the Executor once using the normal flow:

1. Start `workgate control` in one terminal.
2. Run `workgate executor connect http://127.0.0.1:8765 --name local` in
   another terminal and approve the pairing request in the Control WebUI
   at `http://127.0.0.1:8765/ui`.
3. Stop the Control process.

For later starts, run:

```bash
workgate colocate
```

It starts `workgate control` and `workgate executor run` with their
normal role-specific config files. Optional explicit paths can be passed
without changing the other role:

```bash
workgate colocate --control-config ./control.yaml --executor-config ./executor.yaml
```

When using `--executor-config FILE`, pair using that same configuration:
`workgate executor connect CONTROL_URL --config FILE`. The URL must match
the Control configuration. Both children inherit environment overrides,
so avoid setting a shared `WORKGATE_CONFIG` or `WORKGATE_STATE_DIR` when
the roles need separate configuration or state paths.

The normal Control URL is `http://127.0.0.1:8765`, and the MCP URL is
`http://127.0.0.1:8765/mcp`. The launcher owns the child processes and
stops both when interrupted or when either child exits; it does not restart
them or repair failed pairing.

## Linux systemd user service

After pairing, install and start the ordinary colocate launcher as a persistent
user service:

```bash
workgate colocate install-service
```

For nondefault configuration files, use
`workgate colocate install-service --control-config /absolute/control.yaml --executor-config /absolute/executor.yaml`.
The installer records absolute paths for the selected configuration files,
including existing default YAML files, so the service reads the same files even
if its environment differs from the install shell. File edits take effect after
restarting the service. Installation refuses to overwrite an unrelated unit.
Use durable YAML rather than temporary `WORKGATE_*` environment overrides.

The service is named `workgate-colocate.service`. Operate it using systemd:

```bash
systemctl --user status workgate-colocate.service
journalctl --user -u workgate-colocate.service -f
systemctl --user restart workgate-colocate.service
systemctl --user disable --now workgate-colocate.service
```

The installer does not pair executors, install cloudflared, configure system
boot, or create runtime state. The `systemd --user` manager must be available;
on hosts that must run without an interactive login, configure systemd linger
separately. In WSL, ensure the WSL instance itself starts after Windows boot.

## State and recovery

Control uses its ordinary `state_dir` (on Linux, normally
`~/.local/state/workgate`), while Executor uses its existing
role-specific default `~/.local/state/workgate/executor-runtime`.
These are the **same directories** used if you launch both roles by hand.
The normal Executor profile, Control trust registry, and resource recovery
behavior therefore apply without translation.

For separate machines, see [Choose a deployment](deployment.md).
