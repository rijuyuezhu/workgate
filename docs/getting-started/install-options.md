# Install options

Install Workgate wherever the **control** runs and on every machine that acts as
an **executor**. They may be the same physical machine, but they remain separate
runtime roles.

## Source checkout

Best for development and early deployments:

```bash
git clone https://github.com/rijuyuezhu/workgate.git
cd workgate
uv sync
```

Then follow the [Quickstart](quickstart.md).

## Python installation from GitHub

Workgate is **not published to PyPI**. Choose a tag from
[GitHub Releases](https://github.com/rijuyuezhu/workgate/releases) and install
from that Git revision, not from `pip install workgate` or `uvx workgate`.
The examples below pin `v5.0.0a5` (**prerelease**); choose a non-prerelease
release tag for a stable deployment.

Run without a persistent installation (requires Python 3.14):

```bash
uvx --python 3.14 --from 'git+https://github.com/rijuyuezhu/workgate.git@v5.0.0a5' workgate --help
```

Or install the CLI in an isolated environment:

```bash
pipx install 'git+https://github.com/rijuyuezhu/workgate.git@v5.0.0a5' --python python3.14
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
pipx install 'workgate[gui] @ git+https://github.com/rijuyuezhu/workgate.git@v5.0.0a5' --python python3.14
python3.14 -m pip install 'workgate[gui] @ git+https://github.com/rijuyuezhu/workgate.git@v5.0.0a5'  # existing environment
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
