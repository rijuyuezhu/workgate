#!/usr/bin/env bash
# The single PyInstaller build contract used by CI and GitHub Releases.
set -euo pipefail

uv run --extra gui --with pyinstaller --with typer pyinstaller \
  --clean --noconfirm --onefile --name workgate --specpath build \
  --collect-all workgate \
  --collect-all mcp \
  --collect-all uvicorn \
  --collect-all fastapi \
  --collect-all starlette \
  scripts/release/pyinstaller-entry.py
