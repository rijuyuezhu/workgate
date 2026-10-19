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
$previous = ''
$next = ''
$published = $false
$runtime = ''
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
    $file = [IO.File]::OpenRead($archive)
    try {
        $zip = [IO.Compression.ZipArchive]::new($file, [IO.Compression.ZipArchiveMode]::Read)
        try {
            $entry = $zip.GetEntry("workgate-$Target/workgate.exe")
            if ($null -eq $entry -or $entry.Length -le 0 -or $entry.Length -gt $MaxArchiveBytes) {
                throw 'Executor runtime executable is missing or invalid'
            }
            $source = $entry.Open()
            try {
                $destination = [IO.File]::Create($staged)
                try {
                    $buffer = New-Object byte[] 65536
                    $written = [long]0
                    while (($count = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $written += $count
                        if ($written -gt $MaxArchiveBytes) {
                            throw 'Executor runtime executable exceeds the size limit'
                        }
                        $destination.Write($buffer, 0, $count)
                    }
                    if ($written -ne $entry.Length) {
                        throw 'Executor runtime executable size mismatch'
                    }
                }
                finally { $destination.Dispose() }
            }
            finally { $source.Dispose() }
        }
        finally { $zip.Dispose() }
    }
    finally { $file.Dispose() }

    $reported = (& $staged --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $reported -ne "workgate $Version") {
        throw 'Executor runtime version mismatch'
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
        $target = Join-Path $runtime 'workgate.exe'
        $next = Join-Path $runtime ([Guid]::NewGuid().ToString('N') + '.next.exe')
        $previous = Join-Path $runtime ([Guid]::NewGuid().ToString('N') + '.previous.exe')
        Copy-Item -LiteralPath $staged -Destination $next
        if (Test-Path -LiteralPath $target) {
            Move-Item -LiteralPath $target -Destination $previous
        }
        $published = $true
        Move-Item -LiteralPath $next -Destination $target
        & $target @install
        if ($LASTEXITCODE -ne 0) { throw 'Executor service installation failed' }
        $published = $false
        Write-Host 'Workgate executor installed and started.'
        Write-Host "Management: $target executor status"
    } else {
        & $staged @run
        if ($LASTEXITCODE -ne 0) { throw "Executor exited with code $LASTEXITCODE" }
    }
}
finally {
    $rollbackError = $null
    if ($published -and $runtime) {
        $target = Join-Path $runtime 'workgate.exe'
        try {
            if (Test-Path -LiteralPath $target) {
                Remove-Item -LiteralPath $target -Force -ErrorAction Stop
            }
            if ($previous -and (Test-Path -LiteralPath $previous)) {
                Move-Item -LiteralPath $previous -Destination $target -ErrorAction Stop
            }
        } catch {
            # Keep the old executable if Windows cannot release the new one.
            $rollbackError = $_
        }
    }
    if (-not $rollbackError -and $previous -and (Test-Path -LiteralPath $previous)) {
        Remove-Item -LiteralPath $previous -Force -ErrorAction SilentlyContinue
    }
    if ($next -and (Test-Path -LiteralPath $next)) {
        Remove-Item -LiteralPath $next -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
    if ($rollbackError) {
        throw "Executor installation rollback failed; previous executable preserved at '$previous': $rollbackError"
    }
}
"""
    return (
        template.replace("__CONTROL__", control.replace("'", "''"))
        .replace("__ARTIFACT_ROOT__", artifact_root.replace("'", "''"))
        .replace("__VERSION__", __version__)
        .replace("__MAX_ARCHIVE_BYTES__", str(max_archive_bytes))
    )
