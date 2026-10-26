# Versioned source runtime and role setup (#456)

## Local one-command install (requires an existing Git checkout)

Once a release contains the source-role commands, specify its **actual
published tag** and choose exactly one role. The examples below use
`vRELEASE` as a placeholder; **do not use an older tag that lacks `setup`**.
Git is required. The public bootstraps install missing uv with Astral's
official user-local installer; local checkout scripts still require uv on PATH.
Source installation never requests administrator rights by default.

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
Relative YAML paths (and `~`) are resolved from the invoking shell before
running role setup inside the versioned checkout.

All role options are checked before touching the versioned runtime. The
new release is prepared and verified first, but the stable user CLI switches
**only after the role setup completes successfully**; on failure the previous
stable command remains unchanged. The native service launchers point directly
at the verified source interpreter rather than invoking `uv run` again.
Any existing user's config, state, or paired identity remains under the
individual role's own existing safety rules. Before role setup the installer
checks the current native service status and **refuses an implicit reinstall
or upgrade** if the target role (or a conflicting Colocate service) is already
installed, including stopped services. When a managed launcher already exists,
its service status is checked before cloning or syncing a new checkout. Older
launchers without status support fall back to the check using the prepared CLI.
New installs also write a small private `install.json` receipt in the
managed installation root (only role, release tag and a hash of install flags).
Repeating the **identical** install command is a no-op without `uv sync`,
pairing or service restart when the pinned checkout, stable command and native
running service still agree. Changes to role, tag or installation arguments,
stale/stopped services, missing/malformed receipts and pre-existing installs
from earlier releases are **not** silently accepted as a no-op. In those
cases the existing native-service refusal stays intact; use an explicit
staged upgrade/cutover when appropriate. The receipt contains no credentials
and is never sufficient on its own to claim a healthy native service.
No-op behavior does not claim that the Executor is currently connected.

The existing service and stable CLI stay untouched on refusal. Use the
existing role service commands for explicit changes; new-version migration,
rolling upgrade and rollback remain separate #456 acceptance work.

## Stage a release without switching the running installation

For a planned upgrade or rollback, first prepare the requested released tag
and verify its full-extras runtime **without activating the stable CLI**:

```sh
sh deploy/source/prepare.sh --role control --tag vRELEASE --no-activate-launcher
```

```powershell
& .\deploy\source\prepare.ps1 -Role control -Tag vRELEASE -NoActivateLauncher
```

Select the actual role and published tag. The new checkout is reported under
`versions/<commit>`, but the existing `bin/workgate` launcher, service and
pairing state remain unchanged. This stages a version **only**: it is not an
automatic service upgrade or rollback. Use the role's native service commands
for an explicitly coordinated cutover; do not rerun `uv sync` against a
checkout currently used by an active service.

## Explicit staged Linux Control / Colocate / Executor cutover

Once a newer version has been prepared with `--no-activate-launcher`, switch
a **managed Linux Control or Colocate** service explicitly, without another `uv sync`:

```sh
sh deploy/source/prepare.sh --role control --tag vRELEASE --no-activate-launcher
sh deploy/source/switch.sh --role control --tag vRELEASE \
  --install-root "$HOME/.local/opt/workgate" \
  --control-config "$HOME/.config/workgate/config.yaml"
```

Or, for a previously paired Colocate (which starts both Control and Executor
in the **same** native service):

```sh
sh deploy/source/prepare.sh --role colocate --tag vRELEASE --no-activate-launcher
sh deploy/source/switch.sh --role colocate --tag vRELEASE \
  --install-root "$HOME/.local/opt/workgate" \
  --control-config "$HOME/.config/workgate/config.yaml" \
  --executor-config "$HOME/.config/workgate/executor/config.yaml"
```


For an existing **standalone, paired Linux Executor**, using its current durable
Executor YAML and retained identity (no new pairing):

```sh
sh deploy/source/prepare.sh --role executor --tag vRELEASE --no-activate-launcher
sh deploy/source/switch.sh --role executor --tag vRELEASE \
  --install-root "$HOME/.local/opt/workgate" \
  --executor-config "$HOME/.config/workgate/executor/config.yaml"
```

Executor cutover checks the old active native systemd service, generated
launcher, saved source runtime status, and unchanged profile credentials and
state directory. It uses only `executor install-service` to replace the native
service, not `setup` or `connect`. Failed activation triggers an attempt to
restore the previous version; it reports an error if paired identity changes.

Supply the actual published tag and your existing install/config paths. The
switch requires an already-running, correctly bound managed service
and a prepared checkout from the official repository. It reuses the native role's `install-service` and leaves the durable YAML
configs, pairing state, and prior checkout in place.
If native activation fails, it attempts to restore the previous service and
stable CLI, reporting an error if recovery also fails. To roll back explicitly,
run the same `switch.sh` command using the previously prepared release tag.
A same-release switch is a no-op, not a restart. The target must support
that role's native service installer; `v5.0.0a5` lacks `control install-service`
and cannot be used as a managed Control rollback target.

This cutover supports **Linux Control, Colocate, and standalone Executor**.
It checks native systemd active state, not HTTP/WebUI health or Executor
connectivity. Verify the application after switching. Windows and full
real-release rollback acceptance remain under #456. Existing unmanaged
installations are not adopted automatically.

## Explicit staged Windows Control / Colocate cutover

On Windows x64 or native ARM64, prepare a published source release **without
activating it**, then explicitly switch an existing owned Task Scheduler service:

```powershell
.\deploy\source\prepare.ps1 -Role control -Tag vRELEASE -NoActivateLauncher
.\deploy\source\switch.ps1 -Role control -Tag vRELEASE -ControlConfig "C:\path\to\control.yaml"
```

For a paired Colocate, use `-Role colocate` and also pass
`-ExecutorConfig "C:\path\to\executor.yaml"` to `switch.ps1`. Both
config paths must already exist and match the existing service launcher.
Replace the example paths with your durable YAML files.

The switch checks the previously managed `workgate.cmd`, the staged pinned
release, the Task Scheduler task's owner, action and running state, and the
actual `pythonw.exe`/launcher binding. It stops the old native task **before**
installing and starting the new one, avoiding Windows' IgnoreNew behavior.
After verifying the new task, it switches the stable command. Failure triggers
an attempt to stop and restore the old task and command; an unchanged release
is a no-op. Use the previous prepared tag for explicit rollback.

For a **standalone paired Executor** (not Colocate), use
`-Role executor -ExecutorConfig "C:\path\to\executor.yaml"` and omit
`-ControlConfig`. This path verifies the existing owned Task Scheduler action,
source runtime metadata, stable state directory and pairing profile, then uses
only `executor stop` and `executor install-service`. It never
runs pairing or setup, and requires the old profile to remain unchanged.

Windows Control, Colocate and Executor are supported. Live connection health
and complete real-published-release acceptance remain under #456. No `uv sync`
or dependency changes occur during cutover; do not run the normal switch
against unmanaged or older source tasks whose native action binding cannot
be verified.

### First adoption of a paired, unversioned Windows Executor

An existing standalone Executor installed from an older, **unversioned**
source virtualenv may still have a valid, owned Task Scheduler service and
pairing profile. The ordinary switch intentionally rejects it. Once the
**target release containing this adoption option** is published, prepare it
without activating the command, then explicitly adopt the old service:

```powershell
& .\deploy\source\prepare.ps1 -Role executor -Tag vRELEASE -NoActivateLauncher
& .\deploy\source\switch.ps1 -Role executor -Tag vRELEASE `
    -ExecutorConfig 'C:\path\to\existing\executor\service\config.json' `
    -AdoptLegacyExecutor
```

The existing generated `config.json` is also valid YAML. Use the **actual
existing** config path, not a fresh config. The script discovers the previous
Python from the owner-validated, running `Workgate Executor` task; validates
its action, native service status and paired profile; then reuses the normal
Executor cutover and rollback without calling setup/pairing. On a failed
switch it attempts to restore the old Executor using its original Python and
leaves no newly created stable command. The old virtualenv is retained for
separately approved manual rollback after a successful adoption.

This is a **one-time opt-in** operation. It refuses an existing managed
`workgate.cmd`, stopped/unowned/unrecognized tasks, missing identities and
mismatched runtime state. The option does not apply to Control or Colocate.
The activation checks that Task Scheduler is Running, **not** that the
Executor is connected to Control. Independently verify online health and
command execution after adoption. Do not use this procedure on a live
machine without a planned maintenance window and rollback approval.

## Public GitHub bootstrap, pinned to a release commit

After a **new release contains these bootstrap files**, obtain the full
40-character release commit SHA from the official GitHub release's Git tag.
Use the same SHA in both the raw GitHub URL and the bootstrap arguments.
Replace `FULL_SHA` and `vRELEASE` together; the older `v5.0.0a5`
release **does not contain this bootstrap**. Never replace `FULL_SHA` with
`main`, `latest` or a floating branch.

Linux (choose exactly one role):

```sh
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/FULL_SHA/deploy/source/bootstrap.sh | sh -s -- vRELEASE FULL_SHA --role control
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/FULL_SHA/deploy/source/bootstrap.sh | sh -s -- vRELEASE FULL_SHA --role executor --control-url https://control.example.com --name workstation
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/FULL_SHA/deploy/source/bootstrap.sh | sh -s -- vRELEASE FULL_SHA --role colocate --name workstation
```

Native Windows PowerShell (download first, then execute with explicit role):

```powershell
$sha = 'FULL_SHA'
$tag = 'vRELEASE'
$bootstrap = Join-Path $env:TEMP ("workgate-bootstrap-" + [guid]::NewGuid().ToString('N') + ".ps1")
try {
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/rijuyuezhu/workgate/$sha/deploy/source/bootstrap.ps1" -OutFile $bootstrap
    & $bootstrap -Role executor -Tag $tag -Commit $sha -ControlUrl 'https://control.example.com' -Name 'workstation'
} finally {
    Remove-Item -LiteralPath $bootstrap -ErrorAction SilentlyContinue
}
```

Select `-Role control` or `-Role colocate` instead of `executor` when
appropriate, omitting the Executor-only `-ControlUrl`. Both bootstraps use
the official GitHub repository and a **full, explicitly provided commit**;
they reject tag retargeting before executing checked-out scripts. The source
installer clones the release once more and checks the same SHA before
synchronizing dependencies or touching the managed launcher. The temporary
bootstrap checkout is removed after installation or failure.

**Trust boundary:** the first script is downloaded over HTTPS from a
commit-addressed GitHub raw URL and the clone content is checked against the
commit that the operator pinned. This is *not* a cryptographic publisher
signature or an independent audit of the release contents. Obtain the
tag/commit SHA from a trusted channel and inspect the pinned bootstrap before
executing it. Git must already be installed; the public bootstrap provisions
uv under the user's executable directory if missing, without modifying shell
profiles.
The official uv installer is fetched over HTTPS from Astral, independently
of the pinned WorkGate source; an absent Git/curl or failed installer is
reported. No running Control is needed to fetch the installer.

The public bootstrap still needs release publication and real clean-machine,
upgrade and rollback acceptance before #456 can close.

## Windows ARM64 native cryptography prerequisite

PyCA `cryptography` no longer publishes Windows ARM64 wheels for our current
lockfile version. On **native** Windows ARM64 (including Spark), installing
the package from source against dynamically linked OpenSSL can succeed at
`uv sync` but later fail at Workgate startup with `DLL load failed while
importing _rust`: the Scoop OpenSSL DLL can conflict with Python's own
OpenSSL runtime.

The source preparer uses the native Rust/MSVC toolchain and **ARM64 static
OpenSSL libraries**, building `cryptography` from source in a fresh
environment rather than using a cached dynamically linked wheel. An
already-installed, matching package is not unnecessarily reinstalled on
subsequent preparations. A supported **OpenSSL 3.5 LTS or newer** ARM64 SDK providing
`include/openssl/ssl.h`, `libcrypto_static.lib` and
`libssl_static.lib` is required. Scoop's `openssl-lts` installation provides
these under `lib/VC/arm64/MD`, but Scoop's current OpenSSL 3.0 package
is **end-of-life and rejected** for release builds. Supply a maintained
ARM64 OpenSSL SDK instead; the preparer validates its header version before
compilation. The preparer recognizes Scoop's
`~/scoop/apps/openssl-lts/current` SDK (not the executable shim), or accepts
explicit `OPENSSL_DIR`, `OPENSSL_LIB_DIR` and `OPENSSL_INCLUDE_DIR`
settings for nonstandard layouts. Relative SDK paths are resolved before
running `uv sync`. It also recognizes Scoop rustup's
`~/scoop/persist/rustup/.cargo/bin` when Cargo is not on PATH. The
Scoop path alone is not evidence that the SDK is currently supported.

Native MSVC Build Tools must be installed and their ARM64 developer-shell
environment initialized (e.g. `vcvarsall.bat arm64`) before invoking the
source installer; this setup only affects the current shell. Missing tools
or SDK files cause a clear preparation error rather than a silently incomplete
runtime; previous launchers and services are not changed.

No downgrade of `cryptography`, x64 emulation, DLL copying or global
Python modifications are performed. **This source-build toolchain is an
interim/developer fallback**, not the intended end-user installation path.
Issue [#475](https://github.com/rijuyuezhu/workgate/issues/475) tracks a
version-pinned, verified prebuilt native ARM64 wheel so ordinary Windows ARM64
installations will not require Cargo, MSVC or an OpenSSL SDK. The temporary
source build can take several minutes on a fresh checkout. The preparer
checks the actual Python build target rather than only the reported host CPU,
and tests AES-GCM with both cryptography-first and ssl-first imports before
activating a launcher. The final
`workgate --version` verification must succeed using the release's isolated
`.venv`; full native ARM64 Executor/GUI/PTY and managed upgrade acceptance
remains part of #459.

## Windows ARM64 (automatic release Wheel)

For a WorkGate release containing the native `cryptography` Wheel, the normal
source installer works **without extra arguments**:

```powershell
& .\deploy\source\install.ps1 -Role executor -Tag vRELEASE `
  -ControlUrl https://control.example.com -Name workstation
```

On native Windows ARM64, the preparer uses the **specified tag only** and
retrieves the Wheel URL and GitHub-recorded SHA256 from the corresponding
GitHub Release API over HTTPS. It then checks the downloaded SHA256, native
ARM64 extension, lack of dynamic OpenSSL imports, cryptography/OpenSSL versions,
and AES-GCM before enabling the checkout. A missing or invalid asset fails
clearly; it never silently changes to an unverified download or compilation.
**Ordinary users never need to find or enter a SHA256 checksum.** Release
assets remain subject to the GitHub repository's release permissions; the
SHA256 verifies asset integrity, not an independent publisher signature.

The preparer synchronizes the remaining locked GUI/S3 dependencies with
`--no-install-package cryptography` and then installs the verified Wheel.
This sequence is repeated on re-runs because a regular `uv sync --locked`
would reject an out-of-index Windows ARM64 Wheel. Explicit
`-Arm64Wheel`/`-Arm64WheelSha256` overrides remain available for advanced
offline testing only, not normal installation. No frozen executables, new
package manager or alternative update mechanism are introduced.

Previous releases without an ARM64 Wheel asset cannot use this automatic path.
Full native service upgrade/rollback still requires the separate #456/#459
acceptance; source preparation alone never replaces a running service.

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
