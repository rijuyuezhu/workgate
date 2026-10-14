# Security

Workgate exposes shell, filesystem, browser, and optional native desktop
operations through MCP clients. Treat a connected client as a powerful operator,
not as an untrusted read-only viewer.

## Deploy control safely

- Keep control on loopback for local use. For remote access, use HTTPS through a
  trusted proxy or private network, set `base_url` to the external origin, and
  keep `auth_mode: oauth`. Do not publish the debug HTTP interface as a public
  MCP substitute.
- Keep OAuth admin approval credentials private and use a long random PIN.
  Grant only the scopes a client needs; review or revoke access when no longer
  required. Local loopback authentication bypasses are not a security boundary
  against other processes on the same machine.
- Protect `state_dir`, `data_dir`, configuration, and backups. Do not expose
  them through a web server or mount them into unrelated services. Consult
  [Deployment](getting-started/deployment.md),
  [Configuration](reference/configuration.md), and the
  [ChatGPT connector guide](getting-started/chatgpt-connector.md).

## Control and executor trust

Control authenticates MCP clients and manages tasks, sessions, OAuth, and
executor pairing. Executors perform machine operations under their own OS
accounts; a session binds an executor and workdir. Pair only machines and
control servers you trust. Protect saved pairing profiles and credentials;
revoke an executor that is lost or compromised and re-pair it deliberately.

**A workdir is not a sandbox.** An executor can access any file its OS account
can access, including absolute paths outside the session workdir. Use a dedicated
unprivileged account, filesystem permissions, and a VM or other OS isolation
when strict confinement is required. Do not run an executor as root or give it
sensitive host mounts unless those privileges are intentionally available to
the connected model.

Bounded commands attempt to clean up descendant processes and fail closed when
required containment cannot be verified. This is process-lifecycle handling,
not a substitute for account or machine isolation. Treat browser scripting,
persistent terminals, and unrestricted shell commands as powerful code execution.

## Secrets and upstream integrations

Keep credentials out of prompts, command arguments, committed files, and
screenshots. Agent Bridge can store credentials for additional MCP services:
only configure trusted upstream servers, use narrowly scoped revocable
credentials, and understand that the upstream receives the credentials used to
contact it. Local stdio upstreams can receive secrets in their environment;
HTTP upstreams can receive them in request headers. See
[Agent capability bridge](guides/agent-bridge.md).

## WebUI, browser, and desktop permissions

The WebUI and browser automation act with the permissions of their authenticated
client and bound executor. Limit account access, review requests before
approving broad operations, and keep interactive sessions private. Native GUI
automation needs `gui:use` and the host OS's applicable permissions; it can
interact with the executor's actual desktop rather than an isolated test page.
Do not enable GUI access on a sensitive desktop without accepting that authority.
On Niri, the optional `gui_niri_native_keyboard_enabled` setting uses the
same-user Wayland socket through `wtype` with no per-application Portal consent
prompt. It is disabled by default and grants keyboard authority over the live
desktop when enabled; restrict who can connect to the executor. Typed text is
passed to `wtype` via stdin, rather than command-line arguments.
The separate opt-in `gui_niri_native_pointer_enabled` similarly permits
native mouse events through `wdotool` with a forced `wlr-protocols` backend.
It does not require root or `/dev/uinput`, but also has no per-window Portal
consent. Only trusted executor sessions should enable it; current output
rotation/scaling restrictions are described in the executor guide.
Compositor focus and geometry checks cannot prove where the pointer remains
when another local client is simultaneously moving it. Run native pointer
automation only when the desktop is not also being controlled by a person.

## File links and transfers

`create_file_link` creates a snapshot and returns a bearer URL. **Anyone holding
that URL can download the snapshot** until its expiry, revocation, or download
limit, even without OAuth. Use short TTLs, `max_downloads=1` for one-time
handoff, and `revoke_file_link` when access should end. A link does not track
later changes to the original file. Avoid exposing secrets or active HTML that
could be mishandled after download. See
[File sharing](guides/common-workflows.md#share-a-file) for normal usage.

Cross-executor copies are routed through Workgate and may use configured object
storage; treat third-party storage and its access controls as part of your
security boundary. Keep transfer credentials and private state protected.

## Audit records are sensitive

Audit events can retain unredacted tool inputs, outputs, project content, and
credentials. Limit `audit:read` and especially `audit:full`; restrict filesystem
access to Audit archives and retained payloads. Configure retention for your
privacy and disk budget. Before sharing logs, redact them manually rather than
assuming the UI or export has removed secrets. See [Audit log](guides/audit-log.md).

## When something goes wrong

If an access token, executor profile, secret, or bearer link leaks, revoke the
relevant access, rotate affected credentials, review Audit history, and inspect
any workspaces the affected executor could reach. Isolate compromised hosts
before reconnecting them. Do not post credentials, raw Audit data, or exploit
details in public issues. Report suspected vulnerabilities privately using the
[Security policy](https://github.com/rijuyuezhu/workgate/security/policy), including the affected
version, deployment, impact, and a redacted reproduction.
