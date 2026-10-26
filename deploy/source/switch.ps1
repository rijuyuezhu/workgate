# Explicit Windows Control/Colocate switch to an already prepared source tag.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('control', 'colocate')]
    [string]$Role,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [string]$InstallRoot,
    [Parameter(Mandatory = $true)]
    [string]$ControlConfig,
    [string]$ExecutorConfig
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required for Workgate source cutover'
}
$arguments = @('run', '--no-project', '--python', '3.14', 'python',
    (Join-Path $PSScriptRoot 'switch_windows.py'), '--role', $Role,
    '--tag', $Tag, '--control-config', $ControlConfig)
if ($InstallRoot) { $arguments += @('--install-root', $InstallRoot) }
if ($ExecutorConfig) { $arguments += @('--executor-config', $ExecutorConfig) }
& uv @arguments
if ($LASTEXITCODE -ne 0) { throw "Workgate source switch failed (exit $LASTEXITCODE)" }
