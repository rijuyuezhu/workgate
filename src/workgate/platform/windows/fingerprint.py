"""Native Windows nofollow configuration fingerprinting over bounded NT entries."""

import hashlib
import os
from pathlib import Path

from .archive import list_tree
from .security import open_regular_file_no_follow


def fingerprint_agent_config(root: Path) -> str:
    digest = hashlib.sha256()

    def update(*parts: object) -> None:
        for part in parts:
            digest.update(str(part).encode("utf-8", errors="replace"))
            digest.update(bytes((0,)))

    def relative_path(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    try:
        native_entries, truncated = list_tree(
            root, recursive=True, max_entries=5000
        )
    except OSError as exc:
        update(".", "native_list_error", type(exc).__name__, exc)
        return digest.hexdigest()
    for path, kind, size, modified in sorted(
        native_entries, key=lambda row: row[0].as_posix()
    ):
        relative = relative_path(path)
        update(relative, kind, size, modified)
        if kind != "file":
            continue
        try:
            with os.fdopen(open_regular_file_no_follow(path), "rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            update(relative, "native_read_error", type(exc).__name__, exc)
    if truncated:
        update("native_list_truncated", len(native_entries))
    return digest.hexdigest()
