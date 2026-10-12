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

## State and recovery

Control uses its ordinary `state_dir` (on Linux, normally
`~/.local/state/workgate`), while Executor uses its existing
role-specific default `~/.local/state/workgate/executor-runtime`.
These are the **same directories** used if you launch both roles by hand.
The normal Executor profile, Control trust registry, and resource recovery
behavior therefore apply without translation.

For separate machines, see [Choose a deployment](deployment.md).
