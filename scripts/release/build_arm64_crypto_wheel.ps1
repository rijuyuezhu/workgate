# CI-only wheel build. No publication, installation into WorkGate, or release.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:PROCESSOR_ARCHITECTURE -cne 'ARM64') {
    throw 'Requires a native Windows ARM64 runner'
}
foreach ($tool in @('git', 'uv', 'perl')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw ("Missing {0}: ARM64 wheel CI needs Git, uv, Perl, Rust, and MSVC." -f $tool)
    }
}
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$work = Join-Path $env:RUNNER_TEMP 'workgate-crypto-wheel'
$archives = Join-Path $work 'sources'
$prefix = Join-Path $work 'openssl-static'
$output = Join-Path $work 'wheel'
New-Item -ItemType Directory -Force $archives, $output | Out-Null

# Import the native MSVC environment from the installed VS tools.
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
if (-not (Test-Path $vswhere)) { throw 'Missing Visual Studio vswhere.exe' }
$installation = (& $vswhere -latest -products '*' -property installationPath).Trim()
if ($LASTEXITCODE -ne 0 -or -not $installation) {
    throw 'ARM64 MSVC installation not found'
}
$vcvars = Join-Path $installation 'VC/Auxiliary/Build/vcvarsall.bat'
if (-not (Test-Path $vcvars)) { throw 'Missing vcvarsall.bat' }
$setup = Join-Path $work 'set-vc-env.cmd'
@"
@echo off
call "$vcvars" arm64 >nul
if errorlevel 1 exit /b 1
set
"@ | Set-Content -LiteralPath $setup -Encoding ascii
$variables = @(& cmd.exe /d /c $setup)
if ($LASTEXITCODE -ne 0) { throw 'ARM64 vcvarsall initialization failed' }
foreach ($line in $variables) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
}
foreach ($tool in @('cl.exe', 'link.exe', 'nmake.exe', 'cargo.exe')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Missing $tool after native ARM64 MSVC/Rust setup"
    }
}

uv python install 3.14
if ($LASTEXITCODE -ne 0) { throw 'native Python install failed' }
uv venv --python 3.14 (Join-Path $work 'runtime')
if ($LASTEXITCODE -ne 0) { throw 'native Python venv creation failed' }
$python = Join-Path $work 'runtime/Scripts/python.exe'
& $python -c 'import platform; assert platform.machine().upper() == "ARM64"'
if ($LASTEXITCODE -ne 0) { throw 'Python is not ARM64 native' }

& $python (Join-Path $root 'scripts/release/download_arm64_crypto_sources.py') --lock (Join-Path $root 'uv.lock') --output $archives
if ($LASTEXITCODE -ne 0) { throw 'Pinned source SHA256 verification failed' }
Push-Location $work
try {
    tar -xzf (Join-Path $archives 'openssl-3.5.9.tar.gz')
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSL extraction failed' }
    Push-Location (Join-Path $work 'openssl-3.5.9')
    try {
        & perl Configure VC-WIN64-ARM no-shared no-tests "--prefix=$prefix"
        if ($LASTEXITCODE -ne 0) { throw 'ARM64 OpenSSL configure failed' }
        & nmake.exe
        if ($LASTEXITCODE -ne 0) { throw 'ARM64 OpenSSL build failed' }
        & nmake.exe install_sw
        if ($LASTEXITCODE -ne 0) { throw 'ARM64 static OpenSSL install failed' }
    } finally { Pop-Location }

    if (-not (Test-Path (Join-Path $prefix 'lib/libcrypto.lib'))) {
        throw 'Static OpenSSL SDK lacks libcrypto.lib'
    }
    $env:OPENSSL_DIR = $prefix
    $env:OPENSSL_STATIC = '1'
    $env:OPENSSL_NO_VENDOR = '1'
    tar -xzf (Join-Path $archives 'cryptography-50.0.2.tar.gz')
    if ($LASTEXITCODE -ne 0) { throw 'PyCA source extraction failed' }
    uv build --wheel (Join-Path $work 'cryptography-50.0.2') --out-dir $output
    if ($LASTEXITCODE -ne 0) { throw 'ARM64 cryptography wheel build failed' }
} finally { Pop-Location }

$wheel = @(Get-ChildItem -LiteralPath $output -Filter '*.whl')
if ($wheel.Count -ne 1) { throw 'Expected exactly one cryptography wheel' }
$metadata = Join-Path $output 'provenance.json'
& $python (Join-Path $root 'scripts/release/verify_arm64_crypto_wheel.py') $wheel[0].FullName --openssl-version 3.5.9 --openssl-source-sha256 603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a --cryptography-source-sha256 7b46165bb56eb4704e2eaaf86f3c940d19154535d9b0ca7d6d590b04060e00d5 --provenance $metadata
if ($LASTEXITCODE -ne 0) { throw 'Native wheel verification failed' }
uv pip install --python $python $wheel[0].FullName
if ($LASTEXITCODE -ne 0) { throw 'Fresh native wheel installation failed' }
& $python (Join-Path $root 'scripts/release/smoke_arm64_crypto_wheel.py')
if ($LASTEXITCODE -ne 0) { throw 'Native cryptography smoke failed' }
Write-Output "Validated ARM64 wheel + provenance at $output (CI only; NOT published)."
