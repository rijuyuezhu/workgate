# Install Workgate

Choose a role for each computer:

- **Colocate** — start with one computer running both Control and Executor.
- **Control** — host the MCP endpoint and WebUI, typically on a server.
- **Executor** — let Control access another computer's workspaces.

Use the published **v5.0.0a13** source installer. Install Git first, and run the command as the user who will operate Workgate.

## Linux

### One computer: Colocate

Follow the [Quickstart](quickstart.md): it has the published Linux Colocate one-line command and first-use instructions.

### Control on a server

~~~sh
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/1424b9e9d84d5ae1405d28c2ea9a7d3d94e193be/deploy/source/bootstrap.sh | sh -s -- v5.0.0a13 1424b9e9d84d5ae1405d28c2ea9a7d3d94e193be --role control --base-url https://control.example.com
~~~

Replace control.example.com with your own public HTTPS hostname and follow [VPS deployment](vps.md) to publish it.

### Executor on another computer

Replace the Control URL before running:

~~~sh
curl -fsSL https://raw.githubusercontent.com/rijuyuezhu/workgate/1424b9e9d84d5ae1405d28c2ea9a7d3d94e193be/deploy/source/bootstrap.sh | sh -s -- v5.0.0a13 1424b9e9d84d5ae1405d28c2ea9a7d3d94e193be --role executor --control-url https://control.example.com --name workstation
~~~

Approve the pairing request in Control's WebUI.

## Windows PowerShell

Run this block in PowerShell to install **Colocate**:

~~~powershell
$tag = 'v5.0.0a13'
$sha = '1424b9e9d84d5ae1405d28c2ea9a7d3d94e193be'
$script = Join-Path $env:TEMP ("workgate-bootstrap-" + [guid]::NewGuid().ToString('N') + '.ps1')
try {
    Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/rijuyuezhu/workgate/$sha/deploy/source/bootstrap.ps1" -OutFile $script
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Role colocate -Tag $tag -Commit $sha -Name workstation
} finally {
    Remove-Item -LiteralPath $script -ErrorAction SilentlyContinue
}
~~~

For a different role, change the PowerShell invocation inside the block:

- **Control:** use \`-Role control -Tag $tag -Commit $sha\`.
- **Executor:** use \`-Role executor -Tag $tag -Commit $sha -ControlUrl 'https://control.example.com' -Name workstation\`.

Windows ARM64 uses the release's native dependency package automatically.

## After installation

Open the WebUI at \`http://127.0.0.1:8765/ui\` for a local Control. Connect your MCP client to:

~~~text
http://127.0.0.1:8765/mcp
~~~

For a remote Control, use the public HTTPS origin followed by \`/mcp\`. Continue with [Quickstart](quickstart.md) or [Connect ChatGPT](chatgpt-connector.md).

## Already running Workgate?

Use the [upgrade and rollback guide](https://github.com/rijuyuezhu/workgate/blob/main/deploy/source/README.md) for an existing managed source installation, or [migrate an older installation](../guides/migrate-frozen.md). First-time install commands are intended for a new role installation.

For an alternative release, choose the matching tag and commit from [GitHub Releases](https://github.com/rijuyuezhu/workgate/releases). The examples above already contain the published release identity. You can inspect the [source installer](https://github.com/rijuyuezhu/workgate/tree/main/deploy/source) if needed.
