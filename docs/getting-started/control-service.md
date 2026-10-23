# Persistent Control service (source checkout)

The Control-only role uses the existing native user service managers, like
Executor and Colocate. This does not create an additional Workgate supervisor.

Prepare a **durable** Control YAML file (default:
`~/.config/workgate/config.yaml`). Use HTTP mode and configure OAuth before
exposing it beyond loopback, including a strong non-placeholder
`oauth_admin_pin` (at least 8 characters for public OAuth URLs). The normal
Control startup checks public OAuth security and stores its signing secret in
private state; do not print or embed secrets in a service command.

After installing the locked source checkout and its Python 3.14 environment,
initialize Control's OAuth-enabled HTTP configuration once:

```sh
.venv/bin/workgate control init-config
```

This is noninteractive: it creates a random, non-placeholder admin approval PIN
in a user-private YAML (0600 on Linux), and the existing Workgate persistence
mechanism creates its OAuth signing secret. **The PIN and signing secret are
never printed**; view the PIN locally inside your private configuration when
performing a browser approval. For custom locations, supply `--config PATH`
and optionally `--state-dir PATH`; Control binds to loopback by default.
Non-loopback listeners need an explicit `--base-url` for secure OAuth metadata.
Re-running validates and preserves the existing YAML, PIN, and signing identity
rather than replacing them. If a preexisting file is weak, public or invalid,
the command stops and instructs you to correct it; it will never silently
rewrite credentials. On Windows, custom configuration files inherit the parent
directory's NTFS ACL: use an account-private directory, not a shared/public
folder. The default per-user AppData location is preferred.

Install the Control service afterwards:

```sh
.venv/bin/workgate control install-service
.venv/bin/workgate control status-service
.venv/bin/workgate control restart-service
.venv/bin/workgate control stop-service
.venv/bin/workgate control start-service
.venv/bin/workgate control uninstall-service
```

For a nondefault Control YAML location, pass `--config /absolute/config.yaml`
to `install-service`. Installation pins that absolute file path and the
current `.venv` Python executable; it never invokes `uv run` at service startup
(which might resync the environment without selected runtime extras).

On Linux, this installs `workgate-control.service` under
`systemd --user`. Ensure the user manager is available. For unattended boot,
configure user lingering and the host's own startup separately. On native
Windows, it installs the non-elevated current-user `Workgate Control` Task
Scheduler task using `pythonw.exe`; it runs at user logon and writes its own
private service log under the Workgate state directory. The log is retained when
uninstalling so startup failures remain diagnosable. On Linux, systemd's
imported `WORKGATE_*` overrides for Control are explicitly unset so the pinned
YAML remains authoritative even if the login shell and user service manager
have different environments.

Reinstall updates only this managed service definition, not existing Control
state or OAuth signing identity. The installer refuses to overwrite an
unmanaged user unit/task. `uninstall-service` removes only the service, not its
Control configuration, approval records, or state.

Run these commands from the validated Python 3.14 environment or use a stable
launcher pointing to that environment. Full one-step Linux and PowerShell
source setup is tracked separately under #456.
