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

## Release archive

For a self-contained install, download the archive matching your operating
system and architecture from the GitHub release and run the included `workgate`
executable.

The runtime commands and configuration files are the same regardless of install
method. See [Choose a deployment](deployment.md) for the topology and
[Configuration](../reference/configuration.md) for the YAML paths.
