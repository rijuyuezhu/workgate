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
individual role's own existing safety rules. Before role setup the installer
checks the current native service status and **refuses an implicit reinstall
or upgrade** if the target role (or a conflicting Colocate service) is already
installed, including stopped services. Preparation of a new versioned checkout
may already have finished, but the existing service and stable CLI stay
untouched. The caller must explicitly manage an existing deployment using
its normal CLI. Safe no-op reinstallation, migration and rolling upgrade /
rollback remain separate #456 acceptance work.

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
executing it. Git and uv must already be installed on the host; absent tools
are reported clearly. No running Control is needed to fetch the installer.

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
settings for nonstandard layouts. It also recognizes Scoop rustup's
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
