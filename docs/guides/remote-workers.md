# Executors

Workgate keeps one public control endpoint while machine-facing work runs on paired executors. Pairing establishes a stable executor identity and long-lived trust with the control service; the removed legacy remote-worker invite/join and `workgate worker` flows are not part of the current runtime.

## Requirements

The control service needs a URL reachable from the executor. Normal MCP and Human UI access should remain protected by the configured owner authentication. The executor machine needs Workgate installed and outbound access to the control URL; it never needs an inbound Workgate port. Machine-local tools such as Git, compilers, CUDA, and package managers come from that executor host.

## Pair an executor

Run this on the machine you want to pair:

```bash
workgate executor connect https://control.example --name gpu1
```

The command prints an owner verification URL and a short pairing code. Open the verification URL, sign in as the owner, open **Executors**, inspect the reported machine metadata, and approve or deny the request. The browser never receives the executor bearer credential.

After approval, the executor atomically saves its private profile before an auth-only credential validation. That proof does not mark the executor online or publish resource inventory; `executor run` publishes the first runtime hello. Re-running `connect` against the same control URL first validates the saved profile; a still-valid profile is reused rather than paired again.

The executor profile is private durable state under Workgate's normal state root at `executor/profile.json`. It contains the control URL, stable executor ID, and bearer credential, so keep the state directory private and do not copy the profile into logs or support output.

Executor machine policy is resolved locally. In particular, its configured workspace root is executor authority; control-side settings do not override executor path, command, shell, or resource policy.

## Reconnect after a restart

For a foreground process, start the executor from its saved profile:

```bash
workgate executor run
```

Temporary network or control outages reconnect with the same profile. Long inactivity does not itself expire executor trust. If the executor was revoked or its credential was replaced, run `workgate executor connect CONTROL_URL` and complete the owner-approved pairing or replacement flow.

For unattended use, install Workgate's local per-user service after pairing:

```bash
workgate executor install-service
workgate executor status
workgate executor logs --lines 100
```

`install-service` snapshots the effective executor runtime settings into a private service config under the Workgate state directory. It does **not** copy the executor bearer credential, pairing code, OAuth material, or control URL into the service definition or argv. The managed process still loads the existing private `executor/profile.json`, so install, restart, reinstall, and uninstall do not create or replace executor identity. The normal executor profile lock remains authoritative: a manual `executor run` and the managed service cannot poll as the same profile at the same time.

The lifecycle is local administration only; there is no MCP tool that installs or rewrites host services. The remaining commands are:

```bash
workgate executor start
workgate executor stop
workgate executor restart
workgate executor uninstall-service
```

Platform startup semantics differ:

- **Linux:** installs `workgate-executor.service` as a `systemd --user` unit, enables it for the user manager, starts it immediately, and restarts it after exits. Normally the user manager starts with the user's login. For a machine that must run before login, enable systemd lingering for that account according to the host's policy (for example `loginctl enable-linger "$USER"` when permitted).
- **macOS:** installs a per-user LaunchAgent under `~/Library/LaunchAgents`. It starts immediately, is kept alive, and starts again when that user logs in. It does not run before the user login.
- **Windows:** registers a current-user Scheduled Task with limited privileges. It starts immediately and again at user logon, permits battery operation, ignores duplicate starts, and retries failed runs. It does not require administrator rights in the normal per-user path and does not run before login.

`status` reports a typed lifecycle state rendered as `running`, `stopped`, `failed`, `installed`, or `not-installed`, plus the native backend and bounded diagnostics. `logs` returns only a bounded recent view and applies Workgate's secret redaction, including executor bearer tokens.

### Upgrades and configuration changes

For Python/pip/pipx installs, the service uses the Python interpreter that installed the service plus a private stable launcher that imports the currently installed Workgate package. An in-place Workgate package upgrade therefore takes effect on the next service restart without changing executor identity. Self-contained release executables are invoked directly and likewise survive replacement at the same path.

If the Python environment or release executable moves to a different path, `status` reports the managed runtime as stale. Run `workgate executor install-service` again from the new installation to refresh the native definition; this rewrites only service-owned files and preserves `executor/profile.json`. Reinstall the service after changing executor settings as well, because the managed service intentionally uses the private settings snapshot rather than inheriting an interactive shell's `WORKGATE_*` environment.

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

## Revoke or replace an executor

Open **Executors**, select the executor, and choose **Revoke**. Revocation is persisted before the action returns, wakes transport waiters, and prevents the old bearer from authenticating again. Pairing-delivery plaintext associated with that executor is cleared as part of the same owner action.

To deliberately replace the credential for the same stable executor ID, run `workgate executor connect CONTROL_URL` from that machine, inspect the pairing request in **Executors**, and explicitly approve replacement.

## Troubleshooting

- **Pairing request never appears:** confirm the control URL is reachable from the executor and that the short pairing code has not expired.
- **Executor was paired before a reboot:** run `workgate executor run`; ordinary downtime does not require pairing again.
- **Executor reports revoked or unauthorized:** run `workgate executor connect CONTROL_URL` and complete the owner-approved replacement flow if that machine should still be trusted.
- **A machine command or file action is unavailable:** verify that the executor is online, the session is bound to it, and the machine has the required executable/capability.
- **A session remains terminating while an executor is offline:** restore the executor so control can confirm machine-side absence, or use the explicit force-release path only when accepting that remote cleanup is unconfirmed.

For exact CLI syntax, see [CLI reference](../reference/cli.md). For implementation and protocol work, see [Development](../development.md), [Control/executor architecture](../architecture/control-executor.md), and [Security](../security.md).
