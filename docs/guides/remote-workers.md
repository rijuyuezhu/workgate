# Executors and legacy remote workers

Workgate is moving machine execution behind paired executors. Pairing establishes a stable executor identity and long-lived trust with the control service. Already-enrolled legacy remote workers remain usable during the migration, but the old invite/join enrollment path is no longer public.

## Requirements

The control service needs a URL reachable from the executor. Normal MCP and Human UI access should remain protected by the configured owner authentication. The executor machine needs Workgate installed and outbound access to the control URL; it never needs an inbound Workgate port. Machine-local tools such as Git, compilers, CUDA, and package managers still come from that executor host.

## Pair an executor

Run this on the machine you want to pair:

```bash
workgate executor connect https://control.example --name gpu1
```

The command prints an owner verification URL and a short pairing code. Open the verification URL, sign in as the owner, open **Executors**, inspect the reported machine metadata, and approve or deny the request. The browser never receives the executor bearer credential.

After approval, the executor atomically saves its private profile before the first authenticated hello. Re-running `connect` against the same control URL first checks the saved profile; a still-valid profile is reused rather than paired again. The former Remote invite and `/join` enrollment surfaces are intentionally unavailable.

The final executor profile is private durable state under Workgate's normal state root at `executor/profile.json`. It contains the control URL, stable executor ID, and bearer credential, so keep the state directory private and do not copy the profile into logs or support output.

Executor machine policy is resolved locally. In particular, its configured workspace root is executor authority; control-side path settings do not override that machine policy.

## Reconnect after a restart

Start the executor from its saved profile:

```bash
workgate executor run
```

Temporary network or control outages use reconnect/backoff with the same profile. Long inactivity does not itself expire executor trust. If the executor was revoked or its credential was replaced, run `workgate executor connect CONTROL_URL` and complete the owner-approved pairing/replacement flow instead of falling back to a legacy invite.

## Legacy worker service lifecycle

The commands in this section apply only to already-enrolled legacy workers retained during the migration; they are not the provisioning path for new executors.

Linux with `systemd --user` and macOS with launchd can keep one selected profile running as a per-user service:

```bash
workgate worker install-service p_0123456789abcdef
workgate worker status
```

On Linux, installing the `systemd --user` service does not by itself guarantee startup before that user logs in after a reboot. If the worker must be reachable before login, an administrator must enable lingering once for that user:

```bash
sudo loginctl enable-linger USER
```

Without lingering, the enabled worker service starts with the user's systemd manager after login.

Common lifecycle commands are:

```bash
workgate worker start
workgate worker stop
workgate worker restart
workgate worker logs --lines 100
workgate worker logs --follow
workgate worker uninstall-service
```

There is one native worker service per user state directory. Installing another profile rebinds that service; other profiles can still run in the foreground. The native service records both its state root and installed-data root, so startup does not depend on the service manager's CWD. Native Windows service management is not provided, so use the reconnect command from your preferred startup mechanism.

## Start work on an executor

Ask the MCP client to start an explicit session on the intended paired executor. Pass `executor_id` when more than one eligible executor is online:

```text
Start a session in /home/me/project on executor gpu1, inspect the repository and its instructions, then run git status. Do not edit files yet.
```

After `session_start`, the same opaque `session_id` is shared by control and executor. Use it with the normal read, search, edit, shell, job, Todo, and Audit tools. Use `session_change_cwd` to move that session to another allowed directory on the same executor; sessions are never silently rebound.

For long-running non-interactive commands, use asynchronous `bash` and poll the returned job. Use `pty=true` when the executor-side task genuinely needs an interactive terminal; prefer bounded commands otherwise.

## Copy files

Use `session_copy` after starting both source and destination sessions:

```text
Copy results/summary.json from the gpu1 session into reports/gpu1-summary.json in my workstation session, then verify the destination.
```

The control server coordinates the transfer. Same-executor copies involve one executor; cross-executor copies coordinate the two bound executors without turning the control workspace into a hidden filesystem endpoint. Users normally do not need to tune transfer internals. For limits and advanced settings, see [Configuration](../reference/configuration.md).

## Legacy worker update

For an already-enrolled legacy worker, `workgate worker update PROFILE_ID` still refreshes its managed runtime during the migration. Do not use the legacy worker CLI to provision a new machine. Older source-only or pre-Workgate installations are not adopted automatically; stop the old service if needed and pair a final executor instead.

The 5.0-alpha state/data layout change also does not automatically move an older Workgate worker root such as `~/.local/state/workgate-worker`. Existing installations may temporarily keep using an explicitly configured legacy root, but new trust belongs to the final executor profile.

## Revoke an executor

Open **Executors**, select the executor, and choose **Revoke**. Revocation is persisted before the action returns, wakes transport waiters, and prevents the old bearer from authenticating again. Pairing delivery plaintext associated with that executor is cleared as part of the same owner action.

To deliberately replace the credential for the same stable executor ID, run `workgate executor connect CONTROL_URL` from that machine, inspect the pairing request in **Executors**, and explicitly approve replacement.

The **Remotes** page and `remote_admin` remain only for administration of already-enrolled legacy workers; they no longer create enrollment invites.

## Troubleshooting

- **Pairing request never appears:** confirm the control URL is reachable from the executor and that the short pairing code has not expired.
- **Executor was paired before a reboot:** run `workgate executor run`; ordinary downtime does not require pairing again.
- **Executor reports revoked or unauthorized:** run `workgate executor connect CONTROL_URL` and complete the owner-approved replacement flow if that machine should still be trusted.
- **Legacy worker service fails to start:** inspect `workgate worker status` and `workgate worker logs --lines 100` for that already-enrolled worker.
- **A machine command or file action is missing:** verify the machine has the required executable and advertises the needed capability.

For exact CLI syntax, see [CLI reference](../reference/cli.md). For implementation and protocol work, see [Development](../development.md) and [Security](../security.md).
