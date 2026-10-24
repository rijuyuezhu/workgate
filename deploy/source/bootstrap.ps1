# Public PowerShell bootstrap: verify an explicit Git commit before execution.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('control', 'executor', 'colocate')]
    [string]$Role,
    [Parameter(Mandatory = $true)]
    [string]$Tag,
    [Parameter(Mandatory = $true)]
    [string]$Commit,
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
if ($Tag -cnotmatch '^v[0-9]+\.[0-9]+\.[0-9]+([abrc][0-9]+)?$') {
    throw "A versioned Workgate release tag is required"
}
if ($Commit -cnotmatch '^[0-9a-f]{40}$') {
    throw "A full lowercase pinned commit SHA is required"
}
foreach ($tool in @('git', 'uv')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is required before Workgate bootstrap"
    }
}

$scratch = Join-Path ([System.IO.Path]::GetTempPath()) (
    "workgate-bootstrap-" + [guid]::NewGuid().ToString('N')
)
New-Item -ItemType Directory -Path $scratch -ErrorAction Stop | Out-Null
try {
    $checkout = Join-Path $scratch 'release'
    & git clone --depth 1 --single-branch --branch $Tag -- 'https://github.com/rijuyuezhu/workgate.git' $checkout
    if ($LASTEXITCODE -ne 0) { throw "Git release clone failed" }
    $actual = (& git -C $checkout rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot resolve release HEAD" }
    $tagged = (& git -C $checkout rev-parse --verify "refs/tags/$Tag^{commit}").Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot resolve release tag" }
    if ($actual -cne $Commit -or $tagged -cne $Commit) {
        throw "Release tag and expected commit differ; refusing downloaded code"
    }
    $installer = Join-Path $checkout 'deploy\source\install.ps1'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "Pinned release lacks a source installer"
    }
    if ((Get-Item -LiteralPath $installer).Attributes -band
        [System.IO.FileAttributes]::ReparsePoint) {
        throw "Refusing a reparse-point source installer"
    }
    $forward = @{ Role = $Role; Tag = $Tag; ExpectedCommit = $Commit }
    foreach ($key in @(
        'InstallRoot', 'ControlUrl', 'Name', 'ControlConfig',
        'ExecutorConfig', 'HostAddress', 'Port', 'BaseUrl'
    )) {
        if ($PSBoundParameters.ContainsKey($key)) {
            $forward[$key] = $PSBoundParameters[$key]
        }
    }
    & $installer @forward
} finally {
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction Stop
}
