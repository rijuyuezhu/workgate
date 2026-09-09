"""Typed filesystem and process-start errors shared across local and worker runtimes."""

import os
from pathlib import Path
from typing import Any


class PathNotFoundError(FileNotFoundError):
    """A missing filesystem path selected from a trusted operation endpoint."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        super().__init__(str(self.path))


class ShellExecutableNotFoundError(FileNotFoundError):
    """A configured shell executable that could not be started."""

    def __init__(
        self,
        executable: str,
        command: str,
        cwd: str | Path,
        original_error: str,
    ) -> None:
        self.executable = executable
        self.command = command
        self.cwd = str(cwd)
        self.original_error = original_error
        super().__init__(f"Shell executable not found: {executable}")


def process_start_not_found_error(
    exc: FileNotFoundError,
    *,
    executable: str,
    command: str,
    cwd: str | Path,
) -> FileNotFoundError:
    """Classify a subprocess ENOENT as a missing cwd or shell executable."""
    cwd_path = Path(cwd)
    missing = Path(exc.filename) if exc.filename else None
    if missing is not None and os.path.normcase(
        os.path.abspath(missing)
    ) == os.path.normcase(os.path.abspath(cwd_path)):
        return PathNotFoundError(cwd_path)
    if not cwd_path.exists():
        return PathNotFoundError(cwd_path)
    return ShellExecutableNotFoundError(
        executable,
        command,
        cwd_path,
        str(exc),
    )


def workspace_path_not_found_error(
    exc: FileNotFoundError,
    workspace_root: str | Path,
) -> PathNotFoundError | None:
    """Select an actually missing absolute syscall endpoint inside the workspace."""
    root = Path(os.path.abspath(workspace_root))
    candidates: list[Path] = []
    for value in (exc.filename, exc.filename2):
        if not value:
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            continue
        lexical = Path(os.path.abspath(candidate))
        try:
            lexical.relative_to(root)
        except ValueError:
            continue
        candidates.append(lexical)
    for candidate in candidates:
        if not candidate.exists():
            return PathNotFoundError(candidate)
    return None


def public_error_type(exc: BaseException) -> str:
    """Return the stable public error name for typed internal subclasses."""
    remote_type = getattr(exc, "workgate_public_error_type", None)
    if isinstance(remote_type, str) and remote_type:
        return remote_type
    if isinstance(exc, FileNotFoundError):
        return "FileNotFoundError"
    return type(exc).__name__


def tool_error_payload(
    exc: Exception,
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Serialize one tool exception without relying on platform-specific text."""
    if isinstance(exc, ShellExecutableNotFoundError):
        return {
            "status": "executable_not_found",
            "error_type": "FileNotFoundError",
            "message": str(exc),
            "executable": exc.executable,
            "command": exc.command,
            "cwd": exc.cwd,
            "original_error": exc.original_error,
        }

    path_error = exc if isinstance(exc, PathNotFoundError) else None
    if (
        isinstance(exc, FileNotFoundError)
        and path_error is None
        and workspace_root is not None
    ):
        path_error = workspace_path_not_found_error(exc, workspace_root)
    if path_error is not None:
        return {
            "status": "not_found",
            "error_type": "FileNotFoundError",
            "message": str(path_error),
            "path": str(path_error.path),
        }

    message = str(exc) or type(exc).__name__
    return {
        "status": "error",
        "error_type": public_error_type(exc),
        "message": message,
    }


def exception_from_tool_error(data: dict[str, Any]) -> Exception:
    """Reconstruct typed worker failures at the controller session boundary."""
    status = str(data.get("status") or "error")
    message = str(data.get("message") or "remote tool failed")
    if status == "executable_not_found":
        return ShellExecutableNotFoundError(
            str(data.get("executable") or "<unknown>"),
            str(data.get("command") or ""),
            str(data.get("cwd") or "."),
            str(data.get("original_error") or message),
        )
    if status == "not_found":
        return PathNotFoundError(str(data.get("path") or message))
    error_type = str(data.get("error_type") or "remote_error")
    from .protocol.terminal import (
        TerminalBridgeBusyError,
        TerminalBridgeNotFoundError,
        TerminalBridgeUnsupportedError,
    )

    public_types: dict[str, type[Exception]] = {
        "TerminalBridgeBusyError": TerminalBridgeBusyError,
        "TerminalBridgeNotFoundError": TerminalBridgeNotFoundError,
        "TerminalBridgeUnsupportedError": TerminalBridgeUnsupportedError,
        "FileNotFoundError": FileNotFoundError,
        "FileExistsError": FileExistsError,
        "IsADirectoryError": IsADirectoryError,
        "NotADirectoryError": NotADirectoryError,
        "PermissionError": PermissionError,
        "TimeoutError": TimeoutError,
        "ValueError": ValueError,
        "OSError": OSError,
        "RuntimeError": RuntimeError,
    }
    if exception_type := public_types.get(error_type):
        return exception_type(message)
    reconstructed = RuntimeError(message)
    reconstructed.workgate_public_error_type = error_type  # type: ignore[attr-defined]
    return reconstructed
