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

## Python package

For a normal Python installation:

```bash
pipx install workgate
# or
pip install workgate
```

Platform-specific wheels include the native OpenTUI client on supported
platforms. A universal wheel may omit the native TUI; the browser interface is
still available.

## Native desktop automation

Native `gui_list`, `gui_state`, and `gui_action` require the dedicated `gui:use`
OAuth scope and run only on the executor that owns the selected Workgate session.
For source or Python-package executors, install the optional GUI bindings:

```bash
uv sync --extra gui                  # source checkout
pipx install 'workgate[gui]'         # isolated CLI install
pip install 'workgate[gui]'          # Python environment
```

Release standalone executables already bundle the corresponding Python GUI bindings.
macOS requires Accessibility permission for native control. Window screenshots
also require Screen Recording permission; without it, use `gui_state` with
`screenshot=false`. Linux requires a graphical desktop session plus the system
PyGObject/AT-SPI bindings (for example, the distro's Python GI and AT-SPI
typelib packages); Wayland uses the desktop portal and X11 uses the session's
X server. Executors without supported bindings or a usable Linux graphical session
omit `gui.v1`. Runtime permission or desktop-session failures are reported
explicitly; GUI tools never fall back to the control machine. Windows native GUI
automation is not supported.

## Release archive

For a self-contained install, download the archive matching your operating
system and architecture from the GitHub release and run the included `workgate`
executable.

The runtime commands and configuration files are the same regardless of install
method. See [Choose a deployment](deployment.md) for the topology and
[Configuration](../reference/configuration.md) for the YAML paths.
