"""Native Windows PowerShell bootstrap script served by the Control."""

from .. import __version__


def powershell_bootstrap_script(base_url: str, max_archive_bytes: int) -> str:
    control = base_url.rstrip("/")
    artifact_root = control + "/executor/v1/bootstrap"
    template = r"""param(
    [string]$Name = '',
    [string]$DefaultWorkdir = '',
    [switch]$Persist
)
$ErrorActionPreference = 'Stop'
$Control = '__CONTROL__'
$ArtifactRoot = '__ARTIFACT_ROOT__'
$Version = '__VERSION__'
$MaxArchiveBytes = __MAX_ARCHIVE_BYTES__
$Target = 'windows-x86_64'

if (-not [Environment]::Is64BitOperatingSystem -or
    [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'X64') {
    throw 'Windows x86-64 is required'
}
$installDirectory = ''
$installStarted = $false
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('workgate-bootstrap-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary -ErrorAction Stop | Out-Null
try {
    $expected = (Invoke-WebRequest -UseBasicParsing -Uri "$ArtifactRoot/$Target/sha256").Content.Trim().ToLowerInvariant()
    if ($expected -notmatch '^[0-9a-f]{64}$') { throw 'Invalid executor runtime checksum' }
    $archive = Join-Path $temporary 'workgate.zip'
    Invoke-WebRequest -UseBasicParsing -Uri "$ArtifactRoot/$Target/archive" -OutFile $archive
    if ((Get-Item -LiteralPath $archive).Length -gt $MaxArchiveBytes) {
        throw 'Executor runtime archive exceeds the size limit'
    }
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw 'Executor runtime checksum mismatch' }

    Add-Type -AssemblyName System.IO.Compression
    $staged = Join-Path $temporary 'workgate.exe'
    $stagedService = Join-Path $temporary 'workgatew.exe'
    $file = [IO.File]::OpenRead($archive)
    try {
        $zip = [IO.Compression.ZipArchive]::new($file, [IO.Compression.ZipArchiveMode]::Read)
        try {
            foreach ($binary in @('workgate.exe', 'workgatew.exe')) {
                $entry = $zip.GetEntry("workgate-$Target/$binary")
                if ($null -eq $entry -or $entry.Length -le 0 -or $entry.Length -gt $MaxArchiveBytes) {
                    throw "Executor runtime $binary is missing or invalid"
                }
                $source = $entry.Open()
                try {
                    $destination = [IO.File]::Create((Join-Path $temporary $binary))
                    try {
                        $buffer = New-Object byte[] 65536
                        $written = [long]0
                        while (($count = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
                            $written += $count
                            if ($written -gt $MaxArchiveBytes) {
                                throw "Executor runtime $binary exceeds the size limit"
                            }
                            $destination.Write($buffer, 0, $count)
                        }
                        if ($written -ne $entry.Length) {
                            throw "Executor runtime $binary size mismatch"
                        }
                    }
                    finally { $destination.Dispose() }
                }
                finally { $source.Dispose() }
            }
        }
        finally { $zip.Dispose() }
    }
    finally { $file.Dispose() }

    $reported = (& $staged --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $reported -ne "workgate $Version") {
        throw 'Executor runtime version mismatch'
    }
    $serviceVersion = Join-Path $temporary 'service-version.txt'
    $probe = Start-Process -FilePath $stagedService -ArgumentList @(
        '--_windows-version-file', ('"' + $serviceVersion + '"')
    ) -Wait -PassThru
    if ($probe.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $serviceVersion) -or
        (Get-Content -LiteralPath $serviceVersion -Raw).Trim() -ne "workgate $Version") {
        throw 'Executor windowless runtime version mismatch'
    }
    $connect = @('executor', 'connect', $Control)
    if ($Name) { $connect += @('--name', $Name) }
    & $staged @connect
    if ($LASTEXITCODE -ne 0) { throw 'Executor pairing failed' }

    $run = @('executor', 'run')
    $install = @('executor', 'install-service', '--runtime-ownership', 'control-managed')
    if ($DefaultWorkdir) {
        $run += @('--default-workdir', $DefaultWorkdir)
        $install += @('--default-workdir', $DefaultWorkdir)
    }
    if ($Persist) {
        $runtime = Join-Path $env:LOCALAPPDATA 'workgate\executor-bootstrap'
        New-Item -ItemType Directory -Force -Path $runtime | Out-Null
        # Versioned installation directory: never rename/replace a Windows EXE
        # still mapped by a previous running Task Scheduler process.
        $installDirectory = Join-Path $runtime ([Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $installDirectory -ErrorAction Stop | Out-Null
        Copy-Item -LiteralPath $staged -Destination (Join-Path $installDirectory 'workgate.exe')
        Copy-Item -LiteralPath $stagedService -Destination (Join-Path $installDirectory 'workgatew.exe')
        $target = Join-Path $installDirectory 'workgate.exe'
        $installStarted = $true
        & $target @install
        if ($LASTEXITCODE -ne 0) { throw 'Executor service installation failed' }
        $installDirectory = ''
        Write-Host 'Workgate executor installed and started.'
        Write-Host "Management: $target executor status"
    } else {
        & $staged @run
        if ($LASTEXITCODE -ne 0) { throw "Executor exited with code $LASTEXITCODE" }
    }
}
finally {
    if ($installDirectory -and -not $installStarted) {
        Remove-Item -LiteralPath $installDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
    # Once installation starts, retain the versioned files even on failure:
    # Task Scheduler may already reference them, and Windows may hold them open.
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}
"""
    return (
        template.replace("__CONTROL__", control.replace("'", "''"))
        .replace("__ARTIFACT_ROOT__", artifact_root.replace("'", "''"))
        .replace("__VERSION__", __version__)
        .replace("__MAX_ARCHIVE_BYTES__", str(max_archive_bytes))
    )
