# Prepare pinned uv source runtime, then configure one native Workgate service.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('control', 'executor', 'colocate')]
    [string]$Role,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [string]$InstallRoot,
    [string]$ControlUrl,
    [string]$Name,
    [string]$ControlConfig,
    [string]$ExecutorConfig,
    [string]$HostAddress,
    [int]$Port,
    [string]$BaseUrl
)
$ErrorActionPreference = 'Stop'
foreach ($tool in @('git', 'uv')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is required before Workgate source installation"
    }
}
$arguments = @(
    'run', '--no-project', '--python', '3.14', 'python',
    (Join-Path $PSScriptRoot 'install.py'),
    '--role', $Role, '--tag', $Tag
)
if ($InstallRoot) { $arguments += @('--install-root', $InstallRoot) }
if ($ControlUrl) { $arguments += @('--control-url', $ControlUrl) }
if ($Name) { $arguments += @('--name', $Name) }
if ($ControlConfig) { $arguments += @('--control-config', $ControlConfig) }
if ($ExecutorConfig) { $arguments += @('--executor-config', $ExecutorConfig) }
if ($HostAddress) { $arguments += @('--host', $HostAddress) }
if ($PSBoundParameters.ContainsKey('Port')) { $arguments += @('--port', [string]$Port) }
if ($BaseUrl) { $arguments += @('--base-url', $BaseUrl) }
& uv @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Workgate $Role installation failed (exit $LASTEXITCODE)"
}
