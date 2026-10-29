# Executors

Executors are the machines that own working directories, files, shells, jobs,
PTYs, and machine-local integrations. They connect outbound to one control
endpoint; no inbound Workgate port is required on the executor.

## Install an Executor on a fresh machine

Follow the [published source + uv installation guide](../getting-started/install-options.md)
and choose the **Executor-only** role. This supports Linux and native Windows
with the selected role, installs the matching native per-user service,
and asks the Control owner to approve pairing. It requires Git but does not
require Docker or a separate Workgate executable archive.

If the Executor was previously installed using a frozen executable, do not
replace its running service with the first-install command. Preserve its
paired profile and follow the
[explicit migration and rollback guide](migrate-frozen.md).
The migration guide documents tested Linux x86-64 and Windows x64 a11
cutovers, rollback, and the remaining macOS boundary. Follow
[issue #457](https://github.com/rijuyuezhu/workgate/issues/457)
for additional platform acceptance.

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

A managed versioned **source + uv** Executor can follow the Control's
required release while retaining its paired identity and durable state.
It prepares a separate pinned source checkout and validates it before
switching the owned native service; failure keeps the previous runtime for
recovery. A user-owned development checkout is never silently mutated.
See [versioned source setup and cutover](https://github.com/rijuyuezhu/workgate/blob/main/deploy/source/README.md)
for explicit upgrade and rollback.

Linux uses a systemd user service. It is enabled for future user-manager
sessions; remaining active without a logged-in user depends on the host's
systemd linger policy, which Workgate does not change. macOS uses a LaunchAgent
which starts after that user logs in, rather than before login.

Native Windows source installations use a Task Scheduler task for the current
logged-in user (interactive, non-elevated). The task runs `pythonw.exe` directly
so stopping it stops its owning process; Colocate also uses its existing
Windows Job Object to terminate Control and Executor children. Services start
when that user logs in, not as machine-wide Windows services before login.
`workgate executor` supports the ordinary `install-service`, `start`, `stop`,
`restart`, `status`, `logs`, and `uninstall-service` operations; Colocate offers
`install-service`, `start-service`, `stop-service`, `restart-service`,
`status-service`, and `uninstall-service`. Installation requires a paired
executor and durable role configuration.

Windows source Control, Executor and Colocate tasks append their parent/child
diagnostics to private `control.log`, `executor.log` or `colocate.log` files
under the corresponding role's state `service` directory. On launcher startup,
an existing log larger than 4 MiB is compacted in place to its most recent
approximately 2 MiB, before a Child inherits its stream. This preserves recent
failures across restarts without replacing open handles, but **does not limit
growth during one uninterrupted service run**. Linux uses journald instead.

Windows GUI automation also requires a logged-in, unlocked foreground
desktop. It uses native UI Automation and isolated per-window screenshots;
Windows service processes with no accessible input desktop still expose
non-GUI Executor operations.

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

This keyboard option does **not** enable native pointer input. The separate
Niri pointer option below is also opt-in and never uses `/dev/uinput`.
Other desktop environments retain their existing input behavior.

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

Niri 26.04 / `wdotool` 0.5.3 was tested with normal, 90°/180°/270°,
flipped, and flipped-rotation outputs, including a 1.25× virtual display.
The mapping uses compositor-reported logical window geometry and the selected
output's physical mode; `wdotool` reports integer Wayland scale even when
Niri's logical scale is fractional. Missing/inconsistent mode or output data
fails closed. The window must be focused and visible, with a usable
compositor-reported `tile_pos_in_workspace_view`; windows without this
position are still rejected rather than guessing from AT-SPI bounds.

If an AT-SPI element
has no semantic click action, a fresh `element_id` may be clicked using its
validated element-center bounds; explicit window-relative coordinates remain
supported. Elements without usable in-window bounds are rejected. Move,
click, double-click, right-click, scroll and drag are supported. Drag requires
both endpoints to validate against one focused window and output, and sends a
bounded `mouseup` on success, failure or cancellation. Abrupt process loss or
another same-user pointer client can still interfere with global button state.
Pre-input validation prevents invalid targets from receiving button events.
These checks cannot prevent a physical mouse or another same-user client from
moving the global cursor
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
