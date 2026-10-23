# Versioned source runtime preparation (stage 2 of #456)

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

This is **not yet** the final external `curl | sh` or `irm` install endpoint.
The next stage must securely publish and validate those entrypoints, initialize
Control configuration/approval, handle Executor and Colocate pairing, and
install/restart each role's native service only after validation. Never
advertise a prepared but unpaired Executor as a completed installation.
