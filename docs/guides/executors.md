# Executors

Executors are the machines that own Workgate workspaces, files, shells, jobs,
PTYs, and machine-local integrations. They connect outbound to one control
endpoint; no inbound Workgate port is required on the executor.

## Pair an executor

Install Workgate on the machine, configure its local machine policy, then run:

```bash
workgate executor connect https://control.example --name gpu1
```

Open the verification URL shown by the command, sign in as the owner, and
approve the request from **Executors**.

The saved executor profile contains a long-lived credential. On Linux the
default state root is `~/.local/state/workgate/executor-runtime`; keep it
private. Executor machine policy, including `workspace_root`, command/path
restrictions, and local integration credentials, is configured on that machine.

## Run and reconnect

Start a paired executor with:

```bash
workgate executor run
```

Temporary network or control outages reconnect with the same profile. Normal
reboots or long periods offline do not require pairing again. If the executor
was revoked or its credential was replaced, run `executor connect` again and
complete the owner-approved flow.

For unattended Linux startup, see the systemd example in
[VPS deployment](../getting-started/vps.md).

## Start work on an executor

When several executors are online, choose the intended one when starting a
session. After that, normal file, search, shell, job, Todo, and Audit operations
follow the session's executor binding.

Use `session_copy` to copy data between two existing sessions, including
sessions on different executors.

## Revoke or replace trust

Use **Executors** in the Human UI to revoke a machine. To deliberately replace
its credential, run `workgate executor connect CONTROL_URL` on that machine
and approve the replacement.

For protocol and trust details, see
[Control/executor architecture](../architecture/control-executor.md) and
[Security](../security.md).

## Troubleshooting

- **Pairing request never appears:** confirm the control URL is reachable and the pairing code has not expired.
- **Paired machine restarted:** run `workgate executor run`; downtime alone does not require pairing again.
- **Executor is revoked or unauthorized:** run `executor connect` and approve replacement if the machine should still be trusted.
- **A machine operation is unavailable:** verify the executor is online, the session is bound to it, and the required executable/capability exists.

For exact CLI syntax, see [CLI reference](../reference/cli.md).
