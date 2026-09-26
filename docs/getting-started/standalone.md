# Standalone local mode

Standalone mode runs Workgate locally without changing the control/executor
architecture. One small supervisor starts two separate operating-system
processes:

```text
workgate standalone
        |
        +-- workgate control
        |      127.0.0.1:<port>
        |
        +-- workgate executor run
               authenticated executor protocol
```

The supervisor owns lifecycle only. It does not execute shell, filesystem,
Python, job, PTY, or other machine operations. Those operations still cross the
loopback HTTP/JSON executor protocol and run inside the executor process.

Standalone requires no Internet connection after Workgate and the machine-local
tools you need are installed.

## Start standalone

Choose the workspace the local executor is allowed to use:

```bash
uv run workgate standalone --workspace-root ~/Projects/my-project
```

The default control endpoint is:

```text
http://127.0.0.1:8765
```

Use another port when needed:

```bash
uv run workgate standalone \
  --workspace-root ~/Projects/my-project \
  --port 9876
```

`workgate standalone --help` lists the user-facing settings that can be supplied
on the command line. A normal Workgate YAML file is also accepted:

```yaml
# standalone.yaml
port: 8765
workspace_root: /home/you/Projects/my-project
allow_full_control: false
command_denylist:
  - shutdown
  - reboot
```

Start it with:

```bash
uv run workgate standalone --config ./standalone.yaml
```

Standalone deliberately fixes several topology and authentication settings:
the control uses MCP-over-HTTP on `127.0.0.1`, OAuth remains enabled,
localhost authentication bypass is disabled, and public `base_url`,
`oauth_issuer`, and `oauth_resource` overrides are ignored. These settings are
not accepted as standalone CLI flags.

The local MCP endpoint is therefore:

```text
http://127.0.0.1:8765/mcp
```

An unauthenticated request to that endpoint is rejected. Standalone does not
switch the control to `auth_mode=none` merely because the executor is colocated.

## First-run trust bootstrap

The first launch establishes the colocated executor through a protected
same-user launcher channel. The end state is exactly the same long-lived
executor trust used by a separately paired machine:

1. the control child creates a normal random `executor_id` and long-lived
   executor credential;
2. the control persists only the credential verifier in control-owned state;
3. the plaintext credential is placed in a private, short-lived bootstrap file;
4. the executor child imports that payload into its normal private
   `ExecutorProfile`;
5. the executor deletes the bootstrap file and authenticates to control with
   the ordinary executor protocol.

Future launches load the existing executor profile. They do not pair again and
do not mint a new credential merely because the processes restarted.

If the standalone port changes, the existing executor identity and credential
are retained while the profile's loopback control URL is updated.

Standalone refuses to silently create a replacement identity when durable
control trust exists but the matching executor profile/bootstrap material has
been lost. Restore the matching standalone state or intentionally reset that
state instead of allowing ambiguous trust replacement.

The inverse mismatch also fails closed. If the executor profile survives but
the matching control trust is lost, revoked, or otherwise requires owner
action, the executor exits with an owner-action status. The supervisor leaves
that executor offline instead of entering a restart loop; control-only features
remain available. Repair/restore the matching trust boundary, then restart
standalone.

## Local OAuth approval PIN

If `oauth_admin_pin` is already configured, standalone uses it. Otherwise the
control child generates one random PIN on first launch and stores it in its
private control state rather than printing the secret itself to stdout.

For a custom state root such as:

```bash
uv run workgate standalone \
  --state-dir ~/.local/state/workgate \
  --workspace-root ~/Projects/my-project
```

the generated PIN is stored at:

```text
~/.local/state/workgate/standalone/control/oauth-admin-pin
```

Standalone prints the private file path, not the PIN value. Read the file only
when an OAuth client needs owner approval.

## Separate child configuration authority

Standalone may accept one convenient user-facing config, but the two children
never share one ambient Python `Settings` object.

Before launch, the supervisor resolves that input into two private child YAML
files:

- the control child receives only control-owned server, auth, UI, persistence,
  admission, and control-integration settings;
- the executor child receives workspace, command/path policy, machine limits,
  executable paths, and other executor-owned settings.

All inherited `WORKGATE_*` settings are stripped from the child environment
before the role-specific config is applied. The only launcher-only environment
values re-added are private internal bootstrap/control-origin values required
for the colocated executor.

The generated child configs live in a private runtime namespace keyed by the
standalone state root and are not the durable source of truth. Executor scratch
files used by shell/patch/transfer operations are namespaced under that same
per-standalone runtime root, so two standalone deployments under the same OS
account do not share temporary-file quotas or scratch contents.

Executor-local declarative Agent Bridge configuration is also separated from
the control-side Agent Bridge config. Standalone derives a stable executor-owned
config directory from the standalone state root and prints that path at startup.
For example, stdio MCP configuration and other executor-local integration
declarations belong under that printed directory rather than the control's
normal global Agent Bridge config.

## State ownership

Colocation does not merge state stores. Given a user-facing `state_dir` of
`STATE` and `data_dir` of `DATA`, standalone resolves:

```text
STATE/
  standalone/
    control/          control-owned durable state
    executor/         executor-owned durable state
    run.lock          supervisor single-instance lock

DATA/
  standalone/
    <instance>/
      control/        control-owned durable application data
```

`<instance>` is a stable namespace derived from the standalone state root. This
keeps control data separate when the same OS account runs multiple standalone
deployments with distinct state roots but a shared platform data root.

The executor also has a stable, per-standalone configuration namespace under
the platform Workgate config root for executor-local integrations. That config
namespace and the executor state tree are distinct from the control's config
and state even though both processes run as the same OS account.

The normal executor profile is below the executor state root, while paired
executor trust is below the control state root. A control backup therefore
never becomes executor machine authority merely because both directories are
on the same computer.

Back up both `STATE/standalone/` and the matching
`DATA/standalone/<instance>/` directory if you want a standalone installation
to retain its logical control facts, immutable control data, and machine
identity. If you intentionally restore the same standalone state under a new
state-root path, move the matching control-data directory to the newly derived
instance namespace as part of that restore.

## Restart behavior

The supervisor watches both direct children independently.

If the executor exits:

- the control process stays alive;
- control-only features remain available;
- machine-facing operations have no local fallback;
- for an ordinary crash, the supervisor starts a new executor process and the
  executor reuses its persisted profile and credential;
- for an authentication/protocol condition that explicitly requires owner
  action, the supervisor leaves the executor offline instead of restarting it
  repeatedly.

If the control exits:

- the executor process is not deliberately killed;
- current ordinary RPCs are interrupted rather than replayed;
- persistent executor-owned resources may keep their normal executor semantics;
- the supervisor starts a new control process;
- the executor reconnects using the same credential and reports its current
  resource inventory;
- if the replacement control cannot become ready, the supervisor stops retrying
  that control rather than entering a restart storm, while leaving the executor
  running until the owner repairs the problem and restarts standalone.

Stopping the standalone supervisor stops both children. A graceful control
shutdown may wait for an active long poll to drain before the process exits;
crash recovery does not require that drain. If the control child exits during
initial startup before readiness, standalone fails that startup immediately
instead of repeatedly respawning a deterministically broken child.

## Session convenience

Standalone provisions exactly one normal executor. Once that executor is online
and eligible, the normal control routing rule therefore preserves the familiar
single-machine call:

```text
session_start(workdir="project")
```

You do not need to pass `executor_id` when exactly one eligible executor exists.
This is only routing convenience: the created session is still bound to the
executor, and all subsequent machine operations cross the executor protocol.

If no eligible executor is online, `session_start` fails instead of falling
back to execution inside the control process.

## What standalone is not

Standalone is not:

- a hidden in-process machine-execution mode;
- an in-process control/executor composition;
- an unauthenticated localhost server;
- a second kind of executor identity;
- a service manager for remote executors; or
- a requirement for Redis, PostgreSQL, a message broker, an object store, or
  Internet connectivity.

For an always-on remote control deployment, use
[VPS deployment](vps.md). For explicit executor pairing with a control running
elsewhere, see [Executors](../guides/remote-workers.md).
