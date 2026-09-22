# Install options

Install Workgate on the machine that hosts the control process and on every
machine that should act as an executor. They may be the same physical machine,
but they are separate runtime roles.

## Standalone local mode

Best when control and executor live on the same machine and the installation
must keep working offline:

```bash
workgate standalone --workspace-root /path/to/project
```

This still launches separate control and executor processes and keeps their
configuration/state authority separate. The supervisor performs a protected
first-run local bootstrap into the normal executor profile; future launches
reuse that profile. See [Standalone local mode](standalone.md).

## Local source checkout

Best for development machines and contributors:

```bash
git clone https://github.com/rijuyuezhu/workgate.git
cd workgate
uv sync
cp .env.example .env
uv run workgate control --mode mcp
```

Continue with the [Quickstart](quickstart.md) to configure OAuth, HTTPS, pair an
executor, and connect a client.

## Python package

Best when you want a normal Python installation without a source checkout:

```bash
pipx install workgate
# or
pip install workgate

workgate control --mode mcp
```

Platform-specific wheels include the native OpenTUI client on supported
platforms. A universal wheel may omit the native TUI; the browser interface
remains available.

## Release archive

Best for a self-contained installation without managing a Python environment.
Download the archive for your operating system and architecture from the
GitHub release, then run the control role explicitly:

```bash
./workgate control --mode mcp
```

Workspace authority does not belong to the control process. On the machine
that should execute shell, file, Git, and tool operations, create an executor
config such as:

```yaml
workspace_root: /path/to/project
allow_full_control: false
```

Pair that executor once, then run it:

```bash
./workgate executor connect https://control.example.com \
  --config /path/to/executor.yaml
./workgate executor run --config /path/to/executor.yaml
```

Host commands such as Git, compilers, package managers, shells, `tmux`, and
`ripgrep` need to be installed on the **executor** machine, not on a remote
control-only VPS.
