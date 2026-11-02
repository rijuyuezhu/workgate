# Install Workgate

Choose a role: **Colocate** (Control and Executor together), **Control** (MCP endpoint and WebUI), or **Executor** (workspace machine).

## Choose a channel

| Channel | What you get | Recommended for |
| --- | --- | --- |
| **Latest** | Newest published GitHub Release, including Alpha/Beta/RC | Trying current Workgate features |
| **Stable** | Newest non-prerelease GitHub Release | Production, once a compatible source+uv stable release exists |
| **Dev** | Current `main` source checkout | Development and local experimentation |

Install Git first. **Latest** and **Stable** use the same source installer: only release selection differs. It resolves the chosen Git tag to a full commit SHA and verifies the pinned checkout before installing.

**Current compatibility:** Stable currently resolves to v4.3.1, which predates the source+uv installer. The source bootstrap will reject it; use Latest for a source+uv installation until a compatible stable release is published. Dev does not install a managed service.

## Linux

### Colocate on one computer

Latest (includes prereleases):

~~~sh
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/main/deploy/source/bootstrap.sh | sh -s -- --latest --role colocate --name workstation
~~~

Stable (only non-prereleases):

~~~sh
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/main/deploy/source/bootstrap.sh | sh -s -- --stable --role colocate --name workstation
~~~

### Other roles

Replace the options after `--latest` (or `--stable`) with:

- **Control:** `--role control --base-url https://control.example.com`
- **Executor:** `--role executor --control-url https://control.example.com --name workstation`

For remote Control, configure its public HTTPS endpoint as described in [VPS deployment](vps.md). Approve a new Executor's pairing request in the Control WebUI.

## Windows PowerShell

Latest (includes prereleases):

~~~powershell
& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/rijuyuezhu/workgate/main/deploy/source/bootstrap.ps1'))) -Role colocate -Latest -Name workstation
~~~

Stable (only non-prereleases):

~~~powershell
& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/rijuyuezhu/workgate/main/deploy/source/bootstrap.ps1'))) -Role colocate -Stable -Name workstation
~~~

For Control, use `-Role control -Latest -BaseUrl 'https://control.example.com'`. For Executor, use `-Role executor -Latest -ControlUrl 'https://control.example.com' -Name workstation`. Substitute `-Stable` to select the stable channel.

Windows ARM64 uses the native dependency wheel attached to the selected source+uv release.

## Dev: run from current main

Dev is a **development checkout**, not a tagged release or managed service installation. It uses the checked-out source directly, without replacing an existing Workgate service or pairing.

On Linux or macOS:

~~~sh
git clone https://github.com/rijuyuezhu/workgate.git
cd workgate
uv sync --locked
uv run workgate control init-config
uv run workgate colocate
~~~

On Windows PowerShell:

~~~powershell
git clone https://github.com/rijuyuezhu/workgate.git
Set-Location workgate
uv sync --locked
uv run workgate control init-config
uv run workgate colocate
~~~

To develop a different role or contribute code, see [Development](../development.md). Run Dev on a separate port/configuration if an existing Workgate service already uses the defaults.

## Pin a specific published release

Latest and Stable change as new releases appear. For a repeatable installation, pin a specific tag **and full commit SHA**. On Linux:

~~~sh
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/FULL_COMMIT_SHA/deploy/source/bootstrap.sh | sh -s -- vRELEASE FULL_COMMIT_SHA --role colocate --name workstation
~~~

On Windows, download `bootstrap.ps1` from that same commit and invoke it with `-Role colocate -Tag vRELEASE -Commit FULL_COMMIT_SHA`.

Select a matching tag and SHA from [GitHub Releases](https://github.com/rijuyuezhu/workgate/releases). All published-release installers use a versioned source checkout, never an unpinned `main` runtime.

## After installation

Open the WebUI at `http://127.0.0.1:8765/ui` for a local Control and connect the MCP client to `http://127.0.0.1:8765/mcp`. Remote clients must use your public HTTPS origin followed by `/mcp`.

See [Quickstart](quickstart.md) and [Connect ChatGPT](chatgpt-connector.md) for the next steps.

**Already running Workgate?** Use the [upgrade and rollback guide](https://github.com/rijuyuezhu/workgate/blob/main/deploy/source/README.md) rather than rerunning a first-time installer. For old frozen installations, see [Migration](../guides/migrate-frozen.md).
