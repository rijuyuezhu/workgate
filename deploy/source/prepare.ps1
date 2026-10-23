# Prepare a pinned Workgate source runtime; services and pairing follow separately.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('control', 'executor', 'colocate')]
    [string]$Role,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [string]$InstallRoot
)
$ErrorActionPreference = 'Stop'
foreach ($tool in @('git', 'uv')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is required before Workgate source preparation"
    }
}
$arguments = @('run', '--no-project', '--python', '3.14', 'python',
    (Join-Path $PSScriptRoot 'prepare.py'), '--role', $Role, '--tag', $Tag)
if ($InstallRoot) { $arguments += @('--install-root', $InstallRoot) }
& uv @arguments
if ($LASTEXITCODE -ne 0) { throw "Workgate source preparation failed (exit $LASTEXITCODE)" }
