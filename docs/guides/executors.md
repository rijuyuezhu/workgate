# Executors

Executors are the machines that own working directories, files, shells, jobs,
PTYs, and machine-local integrations. They connect outbound to one control
endpoint; no inbound Workgate port is required on the executor.

## Bootstrap a fresh machine

For a supported Linux or macOS machine connecting to a self-hosted Workgate
control, run the public bootstrap script:

```bash
curl -fsSL https://control.example/executor/v1/bootstrap | bash -s -- --name gpu1
```

The script downloads the matching official Workgate release, verifies its
SHA-256 and version, extracts only the expected `workgate` executable, then
runs the normal device-code pairing flow. Nothing is pre-authorized: open the
verification URL printed on the executor and approve the request as usual.

Add `--persist` to install and start the existing native per-user service:

```bash
curl -fsSL https://control.example/executor/v1/bootstrap | bash -s -- --name gpu1 --persist
```

Use `--default-workdir /path` only when the executor should start somewhere
other than its normal default. Re-running bootstrap reuses the existing paired
identity when it is still valid.

## Pair an already-installed executor

Install Workgate on the machine, configure its local machine settings, then run:

```bash
workgate executor connect https://control.example --name gpu1
```

Open the verification URL shown by the command, sign in as the owner, and
approve the request from **Executors**.

The saved executor profile contains a long-lived credential. On Linux the
default state root is `~/.local/state/workgate/executor-runtime`; keep it
private. Executor machine configuration, including `default_workdir`, process limits,
and local integration credentials, is configured on that machine. Filesystem
authority comes from the executor OS account.

## Run and reconnect

Start a paired executor in the foreground with:

```bash
workgate executor run
```

For a persistent per-user executor, install the native service instead:

```bash
workgate executor install-service
workgate executor status
```

The same CLI provides `start`, `stop`, `restart`, `logs`, and
`uninstall-service`. Reinstalling refreshes the service without changing the
paired executor identity.

A service installed by the persistent bootstrap owns its self-contained Workgate
runtime. When control requires a different Workgate version, that executor
stops accepting new work, finishes already-offered commands, downloads and
validates the matching control release, then atomically replaces the stable
runtime. Validation failure leaves the previous runtime untouched. The service
reconnects with the same executor identity after a successful replacement; a
failed update stays quiescent instead of retrying in a loop, and
`workgate executor status` shows the local update state.

Package-manager, Python-package, and source-checkout installations remain
externally owned. Workgate never rewrites them to satisfy control; update them
with their owning installation method, then restart or refresh the executor
service as needed.

Linux uses a systemd user service. It is enabled for future user-manager
sessions; remaining active without a logged-in user depends on the host's
systemd linger policy, which Workgate does not change. macOS uses a LaunchAgent
which starts after that user logs in, rather than before login.

Temporary network or control outages reconnect with the same profile. Normal
reboots or long periods offline do not require pairing again. If the executor
was revoked or its credential was replaced, run `executor connect` again and
complete the owner-approved flow.

## Optional Niri native keyboard

On Linux/Niri, the RemoteDesktop Portal may not offer keyboard input. To use
Niri's compositor-supported virtual-keyboard protocol, install the `wtype`
client on the executor host, then explicitly enable the executor-local setting:

```yaml
# ~/.config/workgate/executor/config.yaml
gui_niri_native_keyboard_enabled: true
```

Restart that executor to apply the configuration. The default is `false`.
This uses the **current user's Wayland socket** with no Portal permission
prompt, and can send keyboard input to the actual desktop. Enable it only
for a trusted executor account/session. Workgate checks the exact Niri window
before and after input; external focus changes during typing cannot be
prevented completely. Niri native text entry is limited to 512 characters
per action and is passed to `wtype` through stdin, not process arguments.
Never assume a session workdir isolates desktop access.

Mouse/pointer input remains Portal-only; this option does **not** enable
virtual pointer injection or use `/dev/uinput`. Other desktop environments
retain their existing input behavior.

## Optional Niri native pointer (experimental)

On Linux/Niri, the RemoteDesktop Portal may not provide mouse input. An
independent executor-local option enables the compositor-supported virtual
pointer protocol through the optional `wdotool` CLI (available as `wdotool`
or `wdotool-bin` on the Arch AUR). This is **disabled by default**, even if
the native keyboard is enabled:

```yaml
# ~/.config/workgate/executor/config.yaml
gui_niri_native_pointer_enabled: true
```

The pointer is scoped by Niri's focused window and compositor-reported
workspace/output position, not by the often-zero AT-SPI window origin. The
implementation forces `wdotool --backend wlr-protocols`: it never falls back
to `/dev/uinput`. There is no Portal consent prompt; the connected executor
has native pointer authority over the current user's entire Wayland desktop.
Only enable this on trusted desktop sessions. Restart the executor to apply
its setting.

Initial restrictions: only **unrotated, scale-1** outputs are eligible until
hardware validation establishes how other transforms map coordinates. The
window must be focused and visible, with a usable compositor-reported
`tile_pos_in_workspace_view`; windows without this position are rejected.
Output geometry must agree between Niri and the client. Explicit
window-relative coordinates are required if an
AT-SPI element cannot perform a semantic click. Move, click, double-click,
right-click and scroll are supported by the implementation; **drag is not**.
If validation fails, no button event is sent. These checks cannot prevent a
physical mouse or another same-user client from moving the global cursor
between the motion and button commands. Do **not** run native pointer actions
while someone else is actively using that desktop.

## Discover and manage executors

Use the `executor` control tool before `session_start` when more than one
executor is available or when no executor is currently eligible. With no
explicit action it returns the paired fleet, including online/trust state,
runtime/update status, capabilities, active sessions, command load, and the
reasons each executor can or cannot admit a new session.

`executor(action="inspect", executor_id=...)` returns one detailed row. The
bounded administrative actions are `rename`, `reset`, `drain`, `resume`, and
`revoke`. Drain blocks new sessions while existing sessions may finish.
Reset cancels only queued control commands, preserves already-offered work, and
does not revoke executor identity.

When no executor can take new work because executor capacity/capability is
unavailable, the result includes the standard persistent bootstrap recipe.
Pairing still requires the human owner to approve the device code. Workgate
does not provision cloud machines itself; an external provider can create a
machine and then run the same bootstrap recipe.

The WebUI **Executors** page exposes the same inventory and owner actions.
The `executor:use` OAuth scope protects executor discovery and administration.

## Start work on an executor

When several executors are online, choose the intended one when starting a
session. After that, normal file, search, shell, job, Todo, and Audit operations
follow the session's executor binding.

Use `session_copy` to copy data between two existing sessions, including
sessions on different executors. Workgate chooses the data route automatically.

### Optional object-store route

For large cross-executor transfers, control can use an S3-compatible bucket so
payload bytes do not traverse control. Install Workgate on control with the `s3`
extra and set `transfer_object_store_bucket`. The optional
`transfer_object_store_prefix`, `transfer_object_store_region`, and
`transfer_object_store_endpoint_url` settings support scoped keys and
S3-compatible services.

The object-store byte route is opportunistic. Network/HTTP transfer failures can
fall back to the durable control relay before destination commit. Configuration,
signing, durable-state, and destination transaction errors are reported directly
instead of being silently bypassed. Only the durable control relay keeps verified
bytes on control for source-offline retries.

## Revoke or replace trust

Use **Executors** in the WebUI or `executor(action="revoke", ...)` to revoke
a machine. To deliberately replace its credential, run
`workgate executor connect CONTROL_URL` on that machine and approve the
replacement.

For executor trust and host permissions, see [Security](../security.md).

## Troubleshooting

- **Pairing request never appears:** confirm the control URL is reachable and the pairing code has not expired.
- **Paired machine restarted:** check `workgate executor status` when using the managed service; otherwise run `workgate executor run`. Downtime alone does not require pairing again.
- **Executor is revoked or unauthorized:** run `executor connect` and approve replacement if the machine should still be trusted.
- **A machine operation is unavailable:** verify the executor is online, the session is bound to it, and the required executable/capability exists.

For exact CLI syntax, see [CLI reference](../reference/cli.md).
