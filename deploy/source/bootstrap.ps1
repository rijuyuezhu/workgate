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
    [string]$BaseUrl,
    [string]$Arm64Wheel,
    [string]$Arm64WheelSha256
)
$ErrorActionPreference = 'Stop'
if ($Tag -cnotmatch '^v[0-9]+\.[0-9]+\.[0-9]+([abrc][0-9]+)?$') {
    throw "A versioned Workgate release tag is required"
}
if ($Commit -cnotmatch '^[0-9a-f]{40}$') {
    throw "A full lowercase pinned commit SHA is required"
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required before Workgate bootstrap"
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
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        $uvBin = if ($env:UV_INSTALL_DIR) {
            $env:UV_INSTALL_DIR
        } else {
            Join-Path $HOME '.local\bin'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $uvBin 'uv.exe') -PathType Leaf)) {
            $uvInstaller = Join-Path $scratch 'uv-install.ps1'
            Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $uvInstaller
            $oldInstallDir = $env:UV_INSTALL_DIR
            $oldModifyPath = $env:UV_NO_MODIFY_PATH
            try {
                $env:UV_INSTALL_DIR = $uvBin
                $env:UV_NO_MODIFY_PATH = '1'
                & $uvInstaller
            } finally {
                $env:UV_INSTALL_DIR = $oldInstallDir
                $env:UV_NO_MODIFY_PATH = $oldModifyPath
            }
        }
        $env:PATH = "$uvBin;$env:PATH"
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw "uv installer did not provide an executable in $uvBin"
        }
    }
    $forward = @{ Role = $Role; Tag = $Tag; ExpectedCommit = $Commit }
    foreach ($key in @(
        'InstallRoot', 'ControlUrl', 'Name', 'ControlConfig',
        'ExecutorConfig', 'HostAddress', 'Port', 'BaseUrl',
        'Arm64Wheel', 'Arm64WheelSha256'
    )) {
        if ($PSBoundParameters.ContainsKey($key)) {
            $forward[$key] = $PSBoundParameters[$key]
        }
    }
    & $installer @forward
} finally {
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction Stop
}
