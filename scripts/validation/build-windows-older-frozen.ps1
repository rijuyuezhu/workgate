# Build a distinct frozen PE for automatic A -> B upgrade acceptance.
# This is an intentionally synthetic older version, NOT an official release.
$ErrorActionPreference = 'Stop'
$source = 'src/workgate/__init__.py'
$original = [IO.File]::ReadAllText((Resolve-Path $source).Path)
$older = $original -replace '(?m)^__version__ = "[^"\r\n]+"\r?$', '__version__ = "5.0.0a3.dev0"'
if ($older -eq $original) { throw 'Unable to substitute synthetic old frozen version' }
try {
    [IO.File]::WriteAllText((Resolve-Path $source).Path, $older)
    & uv run --extra gui --with pyinstaller --with typer pyinstaller `
        --clean --noconfirm --onefile --windowed --name workgatew-older --specpath build `
        --collect-all workgate --collect-all mcp --collect-all uvicorn `
        --collect-all fastapi --collect-all starlette `
        scripts/release/pyinstaller-entry.py
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic old frozen PE build failed' }
}
finally {
    [IO.File]::WriteAllText((Resolve-Path $source).Path, $original)
}
if ((git diff -- $source)) { throw 'Synthetic version build modified tracked source' }
if (-not (Test-Path 'dist/workgatew-older.exe')) { throw 'Missing old frozen PE' }
