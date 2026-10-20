# Shared Windows CI/release asset contract; the ZIP is consumed by bootstrap.
$ErrorActionPreference = 'Stop'
$artifact = 'workgate-windows-x86_64'
$directory = Join-Path release $artifact
$archive = Join-Path release "$artifact.zip"
New-Item -ItemType Directory -Force -Path $directory | Out-Null
Copy-Item dist/workgate.exe, README.md, LICENSE -Destination $directory
@'
Start the Workgate control:
  .\workgate.exe control --mode mcp
Pair and start an executor:
  .\workgate.exe executor connect http://127.0.0.1:8765
  .\workgate.exe executor run
A native Windows interactive user session is required for GUI actions.
'@ | Set-Content -Encoding utf8 (Join-Path $directory 'QUICKSTART.txt')
# Compress-Archive writes backslash member names on Windows. The bootstrap
# expects stable ZIP names with '/', so specify each member explicitly.
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::Open((Join-Path (Get-Location).Path $archive), [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($file in Get-ChildItem -LiteralPath $directory -File) {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip, $file.FullName, "$artifact/$($file.Name)"
        ) | Out-Null
    }
}
finally { $zip.Dispose() }
$zip = [IO.Compression.ZipFile]::OpenRead((Resolve-Path $archive).Path)
try {
    $entry = $zip.GetEntry("$artifact/workgate.exe")
    if ($null -eq $entry -or $entry.Length -le 0) {
        throw 'Windows archive is missing the expected workgate.exe'
    }
}
finally { $zip.Dispose() }

$digest = (Get-FileHash -Algorithm SHA256 $archive).Hash.ToLowerInvariant()
"$digest  $artifact.zip" | Set-Content -Encoding ascii (Join-Path release "$artifact.sha256")
