# Match build-binary.sh on a native Windows runner; no WSL or Bash required.
$ErrorActionPreference = 'Stop'
& uv run --extra gui --with pyinstaller --with typer pyinstaller `
    --clean --noconfirm --onefile --name workgate --specpath build `
    --collect-all workgate `
    --collect-all mcp `
    --collect-all uvicorn `
    --collect-all fastapi `
    --collect-all starlette `
    scripts/release/pyinstaller-entry.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# The interactive CLI needs a console; the per-user Executor Task Scheduler
# requires a windowless sibling to avoid launching a console at login.
& uv run --extra gui --with pyinstaller --with typer pyinstaller `
    --noconfirm --onefile --windowed --name workgatew --specpath build `
    --collect-all workgate `
    --collect-all mcp `
    --collect-all uvicorn `
    --collect-all fastapi `
    --collect-all starlette `
    scripts/release/pyinstaller-entry.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
