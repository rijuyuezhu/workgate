# Prepare a pinned Workgate source runtime; services and pairing follow separately.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('control', 'executor', 'colocate')]
    [string]$Role,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [string]$InstallRoot,
    [switch]$NoActivateLauncher,
    [string]$Arm64Wheel,
    [string]$Arm64WheelSha256
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
if ($NoActivateLauncher) { $arguments += '--no-activate-launcher' }
if ($Arm64Wheel) { $arguments += @('--arm64-wheel', $Arm64Wheel) }
if ($Arm64WheelSha256) { $arguments += @('--arm64-wheel-sha256', $Arm64WheelSha256) }
& uv @arguments
if ($LASTEXITCODE -ne 0) { throw "Workgate source preparation failed (exit $LASTEXITCODE)" }
