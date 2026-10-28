# Install options

Install Workgate wherever the **control** runs and on every machine that acts as
an **executor**. They may be the same physical machine, but they remain separate
runtime roles.

## Published release source install (recommended for first-time setup)

The source installer is the normal method for Linux and native Windows;
it installs locked Python 3.14, GUI/S3 extras, browser assets and the selected
native user service. The published **v5.0.0a11 prerelease** has a
commit-verified bootstrap (`9aeedc78df7ccab2a5c1ea6d1b89d115abd16845`).

Linux, paste **one** of these commands (exactly one role):

```sh
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/9aeedc78df7ccab2a5c1ea6d1b89d115abd16845/deploy/source/bootstrap.sh | sh -s -- v5.0.0a11 9aeedc78df7ccab2a5c1ea6d1b89d115abd16845 --role control
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/9aeedc78df7ccab2a5c1ea6d1b89d115abd16845/deploy/source/bootstrap.sh | sh -s -- v5.0.0a11 9aeedc78df7ccab2a5c1ea6d1b89d115abd16845 --role executor --control-url https://control.example.com --name workstation
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/9aeedc78df7ccab2a5c1ea6d1b89d115abd16845/deploy/source/bootstrap.sh | sh -s -- v5.0.0a11 9aeedc78df7ccab2a5c1ea6d1b89d115abd16845 --role colocate --name workstation
```

Native Windows PowerShell (choose one role line; the execution-policy
override is only for that PowerShell process):

```powershell
$tag = 'v5.0.0a11'
$sha = '9aeedc78df7ccab2a5c1ea6d1b89d115abd16845'
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
services, use the repository's [source preparation scripts]
(https://github.com/rijuyuezhu/workgate/tree/main/deploy/source). They resolve a specific release tag to a
commit-addressed checkout, run locked uv installation and provision Chromium.
The same directory also includes checkout-local `install.sh` and
`install.ps1` for interactive role setup/pairing. The verified GitHub
bootstrap examples above need no existing checkout. Older releases such as
`v5.0.0a5` predate those entrypoints. An installed service is never
implicitly overwritten or restarted; explicit upgrade/rollback procedures
and remaining clean-host acceptance are tracked in #456.

## Python installation from GitHub

Workgate is **not published to PyPI**. Choose a tag from
[GitHub Releases](https://github.com/rijuyuezhu/workgate/releases) and install
from that Git revision, not from `pip install workgate` or `uvx workgate`.
The examples below pin `v5.0.0a6` (**prerelease**); choose a non-prerelease
release tag for a stable deployment.

Run without a persistent installation (requires Python 3.14):

```bash
uvx --python 3.14 --from 'git+https://github.com/rijuyuezhu/workgate.git@v5.0.0a6' workgate --help
```

Or install the CLI in an isolated environment:

```bash
pipx install 'git+https://github.com/rijuyuezhu/workgate.git@v5.0.0a6' --python python3.14
workgate --help
```

Replace the tag in both commands when choosing a different GitHub release.
Use `@main` or a full commit hash only when deliberately testing development
code, not for a normal installation. The Python distribution builds a universal
wheel and source distribution; use the browser WebUI for interactive management
and persistent terminals.

## Native desktop automation

Native `gui_list`, `gui_state`, and `gui_action` require the dedicated `gui:use`
OAuth scope and run only on the executor that owns the selected Workgate session.
For source or Python-package executors, install the optional GUI bindings:

```bash
uv sync --extra gui  # source checkout
pipx install 'workgate[gui] @ git+https://github.com/rijuyuezhu/workgate.git@v5.0.0a6' --python python3.14
python3.14 -m pip install 'workgate[gui] @ git+https://github.com/rijuyuezhu/workgate.git@v5.0.0a6'  # existing environment
```

Release self-contained executables already bundle the corresponding Python GUI bindings.
macOS requires Accessibility permission for native control. Window screenshots
also require Screen Recording permission; without it, use `gui_state` with
`screenshot=false`. Linux requires a graphical desktop session plus the system
PyGObject/AT-SPI bindings (for example, the distro's Python GI and AT-SPI
typelib packages); Wayland uses the desktop portal and X11 uses the session's
X server. Executors without supported bindings or a usable Linux graphical session
omit `gui.v1`. Runtime permission or desktop-session failures are reported
explicitly; GUI tools never fall back to the control machine.

## Native Windows (PowerShell)

On native Windows x86-64, use a Python 3.14 source checkout or the
release-backed PowerShell bootstrap with a matching Windows archive containing
both `workgate.exe` and `workgatew.exe`. Neither option requires Bash or
WSL. For a source checkout:

```powershell
git clone https://github.com/rijuyuezhu/workgate.git
Set-Location workgate
uv sync
uv run workgate executor connect https://control.example --name windows-laptop
uv run workgate executor run
```

For a Control-provided native binary, replace the example hostname with the
HTTPS Control URL you trust, then run in the logged-in user PowerShell:

```powershell
Invoke-WebRequest -UseBasicParsing 'https://control.example/executor/v1/bootstrap.ps1' -OutFile workgate-bootstrap.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\workgate-bootstrap.ps1 -Persist
```

The script downloads only the matching Windows x86-64 release asset from the
Control's release proxy, verifies the release's SHA-256 checksum, extracts the
console and windowless executables, checks their versions, and prompts for
normal pairing approval. `-Persist` installs the user-level Task Scheduler
Executor; omit it to run in the foreground instead. An unattended service cannot use native GUI
input unless the user's interactive desktop is accessible and unlocked.

Older Windows releases such as `v5.0.0a1` used the former `worker`/TUI model:
the published ZIP contains `workgate.exe` and `workgate-tui.exe`, not
`workgatew.exe`, and its CLI has no `executor` command. **Do not attempt an
in-place managed Executor upgrade from that layout.** Stop the old deployment,
install the current Executor separately, and complete a new owner-approved
pairing; legacy worker credentials are not an Executor profile.

## Release archive

For a self-contained install, download the archive matching your operating
system and architecture from the GitHub release and run the included `workgate`
executable.

The runtime commands and configuration files are the same regardless of install
method. See [Choose a deployment](deployment.md) for the topology and
[Configuration](../reference/configuration.md) for the YAML paths.
