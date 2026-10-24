# Versioned source runtime and role setup (#456)

## Local one-command install (requires an existing Git checkout)

Once a release contains the source-role commands, specify its **actual
published tag** and choose exactly one role. The examples below use
`vRELEASE` as a placeholder; **do not use an older tag that lacks `setup`**.
Git and uv are required; source installation never requests administrator
rights by default.

Linux:

```sh
sh deploy/source/install.sh --role control --tag vRELEASE
sh deploy/source/install.sh --role executor --tag vRELEASE --control-url https://control.example.com --name workstation
sh deploy/source/install.sh --role colocate --tag vRELEASE --name workstation
```

Windows PowerShell (choose one):

```powershell
& .\deploy\source\install.ps1 -Role control -Tag vRELEASE
& .\deploy\source\install.ps1 -Role executor -Tag vRELEASE -ControlUrl https://control.example.com -Name workstation
& .\deploy\source\install.ps1 -Role colocate -Tag vRELEASE -Name workstation
```

The installer composes the pinned full-extras source preparation below with
the role's existing setup command. Control creates a durable private config,
then installs/starts its native user service. Executor waits for Control owner
approval before installing its native service. Colocate runs a temporary
loopback Control for approval and then installs **one** native Colocate
service. It does not install separate persistent Control or Executor services.
No pairing PIN is read from installer arguments or echoed to the terminal.
Additional options include `--install-root`, `--control-config`,
`--executor-config` and Control-only `--host`, `--port`, `--base-url`.

All role options are checked before touching the versioned runtime. The
new release is prepared and verified first, but the stable user CLI switches
**only after the role setup completes successfully**; on failure the previous
stable command remains unchanged. The native service launchers point directly
at the verified source interpreter rather than invoking `uv run` again.
Any existing user's config, state, or paired identity remains under the
individual role's own existing safety rules. This first-run helper does not
yet perform full migration, rolling update/rollback, or no-op reinstallation
of an already installed Colocate service.

The **public downloadable** signed/pinned bootstrap and short external
`curl | sh` / PowerShell download entrypoints are still a later #456
deliverable: these checked-out scripts alone do **not** establish a trusted
remote download endpoint.

## Runtime preparation without role setup

This prepares **only** the reproducible source runtime and a stable CLI.
It does not configure roles, pair executors, install/start services or switch
existing service runtimes. Those are separate rollout steps to prevent replacing
a working deployment before its new runtime is proven.

From a checked-out copy of this repository, with Git and uv installed:

**Linux:**

```sh
sh deploy/source/prepare.sh --role control --tag v5.0.0a5
```

**Windows PowerShell:**

```powershell
& .\deploy\source\prepare.ps1 -Role executor -Tag v5.0.0a5
```

The role is mandatory, never silently selected. Replace the tag with the
actual version to install. `main` and floating branch names are rejected.
The script clones that specific Git tag, verifies it resolves to its checkout
commit, installs Python 3.14, and runs:

```text
uv sync --locked --no-dev --extra gui --extra s3 --python 3.14
```

It then provisions Playwright Chromium and verifies the Workgate executable.
Missing host tools such as tmux or ripgrep are reported for follow-up. On Linux
the default root is `~/.local/opt/workgate`, on Windows
`%LOCALAPPDATA%\Workgate\source`. Each commit has a distinct checkout under
`versions/<full commit SHA>`; the user-facing `bin/workgate` (or
`bin/workgate.cmd`) switches **only after** all preparation checks pass.
It refuses to replace existing unrelated launchers, symbolic links or
modified/untracked source files in an already managed release checkout.
Place that `bin` directory on PATH if desired. Old checkouts and running
services remain unchanged so an upgrade can be rolled back. The tag/commit
check establishes consistency with the Git repository, not a separately
verified publisher signature; trusted public bootstrap delivery is a later
stage of #456.

## Executor-only: pairing and native service

After runtime preparation, use the stable source CLI with the intended
Control URL (HTTPS, except loopback):

```sh
~/.local/opt/workgate/bin/workgate executor setup https://control.example.com --name workstation
```

On Windows PowerShell, use:

```powershell
& "$env:LOCALAPPDATA\Workgate\source\bin\workgate.cmd" executor setup https://control.example.com --name workstation
```

The command prints Control's existing device-approval URL/code and
waits for the owner to approve; **only after approval** does it call the
existing native Executor `install-service` lifecycle. Pairing failure leaves
the service untouched. On re-run a still-valid profile is reused, not
overwritten. `--config` or `--state-dir` select the same durable settings for
pairing and service installation; avoid temporary environment overrides.
This is the Executor-only step: Control and colocated roles require their own
initialization and service checks.

## Colocate: first-time same-host pairing

On a fresh host with no standalone Control or Executor service, run after
preparing the source runtime:

~~~sh
~/.local/opt/workgate/bin/workgate colocate setup --name workstation
~~~

Windows PowerShell:

~~~powershell
& "$env:LOCALAPPDATA\Workgate\source\bin\workgate.cmd" colocate setup --name workstation
~~~

The command creates/validates the private Control OAuth config and starts a
**temporary** loopback Control. Use its WebUI with the admin PIN from the
private YAML to approve local Executor pairing. When approved, setup stops
the temporary Control and installs a **single** native Colocate service.

A rejected pairing or failed startup does not install a service. The temporary
Control process is stopped even on error. Setup refuses an already occupied
Control port or any previously installed standalone Control/Executor/Colocate
service (including stopped ones); it never stops unrelated user services.
When running over SSH, forward the target localhost port to open the WebUI.
Custom YAMLs are selected with --control-config and --executor-config.

This is **not yet** the final external `curl | sh` or `irm` install endpoint.
The next stage must securely publish and validate those entrypoints, initialize
Control configuration/approval, handle Executor and Colocate pairing, and
install/restart each role's native service only after validation. Never
advertise a prepared but unpaired Executor as a completed installation.
