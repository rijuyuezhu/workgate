# Install options

Install Workgate wherever the **control** runs and on every machine that acts as
an **executor**. They may be the same physical machine, but they remain separate
runtime roles.

## Published release source install (recommended for first-time setup)

The source installer is the normal method for Linux and native Windows;
it installs locked Python 3.14, GUI/S3 extras, browser assets and the selected
native user service. The published **v5.0.0a12 prerelease** has a
commit-verified bootstrap (`ffdb695ba41bdaf3e76dd9c9b20dc228f2c7c775`).

Linux, paste **one** of these commands (exactly one role):

```sh
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/ffdb695ba41bdaf3e76dd9c9b20dc228f2c7c775/deploy/source/bootstrap.sh | sh -s -- v5.0.0a12 ffdb695ba41bdaf3e76dd9c9b20dc228f2c7c775 --role control
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/ffdb695ba41bdaf3e76dd9c9b20dc228f2c7c775/deploy/source/bootstrap.sh | sh -s -- v5.0.0a12 ffdb695ba41bdaf3e76dd9c9b20dc228f2c7c775 --role executor --control-url https://control.example.com --name workstation
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/ffdb695ba41bdaf3e76dd9c9b20dc228f2c7c775/deploy/source/bootstrap.sh | sh -s -- v5.0.0a12 ffdb695ba41bdaf3e76dd9c9b20dc228f2c7c775 --role colocate --name workstation
```

Native Windows PowerShell (choose one role line; the execution-policy
override is only for that PowerShell process):

```powershell
$tag = 'v5.0.0a12'
$sha = 'ffdb695ba41bdaf3e76dd9c9b20dc228f2c7c775'
$script = Join-Path $env:TEMP ("workgate-bootstrap-" + [guid]::NewGuid().ToString('N') + '.ps1')
try {
    Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/rijuyuezhu/workgate/$sha/deploy/source/bootstrap.ps1" -OutFile $script
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Role control -Tag $tag -Commit $sha
    # Or use this line instead, for Executor-only:
    # powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Role executor -Tag $tag -Commit $sha -ControlUrl 'https://control.example.com' -Name 'workstation'
    # Or for Colocate:
    # powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Role colocate -Tag $tag -Commit $sha -Name 'workstation'
} finally {
    Remove-Item -LiteralPath $script -ErrorAction SilentlyContinue
}
```

Replace `control.example.com` with the HTTPS endpoint of your Control.
Executor/Colocate pairing requires owner approval; native Windows ARM64 may
also require the documented cryptography toolchain or verified release Wheel.
Git is required; missing `uv` is installed in user space. No Docker or npm
is needed for these source installations. The commands **do not** replace
existing installed services: use explicit `prepare` and `switch` for that.
For detailed prerequisites, identity-preserving upgrade/rollback instructions
and limits of real-machine acceptance, see the
[source installer guide](https://github.com/rijuyuezhu/workgate/blob/main/deploy/source/README.md).
The prerelease examples are not a claim of a supported stable production release.

## Source checkout (development)

Best for development and early deployments:

```bash
git clone https://github.com/rijuyuezhu/workgate.git
cd workgate
uv sync
```

Then follow the [Quickstart](quickstart.md). For an existing source checkout,
see [Persistent Control service](control-service.md) to install a native
Control-only user service without modifying its state.

For a **versioned, full-extras** runtime without changing the currently running
services, use the repository's [source preparation scripts](https://github.com/rijuyuezhu/workgate/tree/main/deploy/source). They resolve a specific release tag to a
commit-addressed checkout, run locked uv installation and provision Chromium.
The same directory also includes checkout-local `install.sh` and
`install.ps1` for interactive role setup/pairing. The verified GitHub
bootstrap examples above need no existing checkout. Older releases such as
`v5.0.0a5` predate those entrypoints. An installed service is never
implicitly overwritten or restarted; use explicit upgrade/rollback procedures
from the source installer guide.

## Native desktop automation

Native `gui_list`, `gui_state`, and `gui_action` require the dedicated `gui:use`
OAuth scope and run only on the executor that owns the selected Workgate session.
For a source checkout, the installer provisions GUI dependencies by default.
For an existing development checkout, use:

```bash
uv sync --extra gui
```

macOS requires Accessibility permission for native control. Window screenshots
also require Screen Recording permission; without it, use `gui_state` with
`screenshot=false`. Linux requires a graphical desktop session plus the system
PyGObject/AT-SPI bindings (for example, the distro's Python GI and AT-SPI
typelib packages); Wayland uses the desktop portal and X11 uses the session's
X server. Executors without supported bindings or a usable Linux graphical session
omit `gui.v1`. Runtime permission or desktop-session failures are reported
explicitly; GUI tools never fall back to the control machine.

## Native Windows

Use the **published source PowerShell bootstrap** at the top of this page.
Choose one role and approve pairing from the Control WebUI. The managed
Executor starts as a native user-level Task Scheduler task and uses
`pythonw.exe` from the pinned source runtime, not a frozen EXE. No WSL
or administrator permission is required for ordinary user setup.

For a development checkout, use `git clone` and `uv sync` as described
above. Existing native Workgate scheduled tasks are never silently
overwritten by the source bootstrap; use the explicit source
[prepare/switch procedure](https://github.com/rijuyuezhu/workgate/blob/main/deploy/source/README.md)
for a managed source upgrade.

## Existing self-contained installations

Older Workgate releases distributed standalone executables. Those are **not**
the recommended installation path. Keep the existing Executor profile,
configuration and native service until a planned source migration has been
verified; **do not** run the new first-install bootstrap over an existing
task or delete its private state to force a new pairing. Follow the
[explicit migration and rollback guide](../guides/migrate-frozen.md);
real frozen-to-source acceptance remains tracked under
[issue #457](https://github.com/rijuyuezhu/workgate/issues/457).
