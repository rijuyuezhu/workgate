"""Executor-owned implementation of the Human UI Files surface."""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import mimetypes
import os
import shutil
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..utils.image_preview import make_image_preview
from ..utils.path_locks import path_locks
from .files import (
    FilesConfig,
    _delete_file_or_dir_local,
    _display_file,
    _list_files_local,
    _read_file_local,
    _resolve_file_path,
    _write_file_local,
)
from .tool_session.store import ToolSessionStore, file_sha256

UI_FILE_PREVIEW_MAX_LINES = 400
UI_FILE_DIRECTORY_MAX_ENTRIES = 1_000
UI_FILE_BINARY_PREVIEW_BYTES = 256
UI_FILE_INLINE_IMAGE_TYPES = frozenset(
    {
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


class UiFilesService:
    """Serve Human UI file operations strictly inside this executor workspace."""

    def __init__(self, config: FilesConfig, store: ToolSessionStore) -> None:
        # Human UI browsing is workspace-confined even when public tools allow full control.
        self.config = replace(config, allow_full_control=False)
        self.store = store

    def _resolve(
        self,
        path: str,
        *,
        must_exist: bool = False,
        follow_final_symlink: bool = True,
    ) -> Path:
        return _resolve_file_path(
            self.config,
            None,
            path,
            must_exist=must_exist,
            follow_final_symlink=follow_final_symlink,
        )

    def _display(self, path: Path) -> str:
        return _display_file(self.config, path)

    @staticmethod
    def _entry_payload(entry: Any) -> dict[str, Any]:
        data = entry.model_dump(mode="json")
        path = str(data["path"])
        data["name"] = Path(path).name
        data["hidden"] = data["name"].startswith(".")
        return data

    @classmethod
    def _sorted_entries(cls, entries: list[Any]) -> list[dict[str, Any]]:
        rows = [cls._entry_payload(entry) for entry in entries]
        rows.sort(
            key=lambda item: (
                0 if item["type"] == "dir" else 1,
                str(item["name"]).casefold(),
                str(item["name"]),
            )
        )
        return rows

    def list_payload(
        self, path: str, *, max_entries: int = UI_FILE_DIRECTORY_MAX_ENTRIES
    ) -> dict[str, Any]:
        resolved = self._resolve(path, must_exist=True)
        if not resolved.is_dir():
            raise NotADirectoryError(str(resolved))
        result = _list_files_local(
            self.config,
            None,
            str(resolved),
            False,
            min(max_entries, self.config.max_directory_entries),
        )
        root = self.config.workspace_root.resolve()
        parent = root if resolved == root else resolved.parent
        return {
            "path": self._display(resolved),
            "parent": self._display(parent),
            "count": result.count,
            "limit_count": result.limit_count,
            "is_truncated": result.is_truncated,
            "entries": self._sorted_entries(list(result.entries)),
            "mutations": {
                "write": True,
                "delete": True,
                "copy": True,
                "move": True,
                "rename": True,
                "mkdir": True,
            },
        }

    @staticmethod
    def _looks_binary(sample: bytes, *, continuation: bytes = b"") -> bool:
        if b"\x00" in sample:
            return True
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.end != len(sample):
                return True
            lead = sample[exc.start]
            if lead < 0xC2 or lead > 0xF4:
                return True
            width = 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
            missing = width - (len(sample) - exc.start)
            sequence = sample[exc.start :] + continuation[:missing]
            if len(sequence) != width:
                return True
            try:
                sequence.decode("utf-8")
            except UnicodeDecodeError:
                return True
        return False

    @staticmethod
    def _file_sample(path: Path) -> tuple[bytes, bytes]:
        sample_bytes = max(4_096, UI_FILE_BINARY_PREVIEW_BYTES)
        with path.open("rb") as handle:
            probe = handle.read(sample_bytes + 3)
        return probe[:sample_bytes], probe[sample_bytes:]

    def preview(
        self,
        path: str,
        *,
        columns: int | None = None,
        rows: int | None = None,
        cell_aspect: float | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve(path, must_exist=True)
        if resolved.is_dir():
            return {
                "kind": "directory",
                **self.list_payload(
                    str(resolved), max_entries=UI_FILE_DIRECTORY_MAX_ENTRIES
                ),
            }
        if not resolved.is_file():
            raise ValueError(
                "Only regular files and directories can be previewed"
            )

        size = resolved.stat().st_size
        media_type = (
            mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        )
        sample, continuation = self._file_sample(resolved)
        if media_type in UI_FILE_INLINE_IMAGE_TYPES:
            if size > self.config.max_file_read_bytes:
                return {
                    "kind": "image",
                    "path": self._display(resolved),
                    "bytes": size,
                    "media_type": media_type,
                    "inline": False,
                    "message": (
                        "Image exceeds the configured inline preview limit of "
                        f"{self.config.max_file_read_bytes} bytes"
                    ),
                }
            data = resolved.read_bytes()
            payload: dict[str, Any] = {
                "kind": "image",
                "path": self._display(resolved),
                "bytes": size,
                "media_type": media_type,
                "inline": True,
                "data_base64": base64.b64encode(data).decode("ascii"),
            }
            if columns is not None or rows is not None:
                preview = make_image_preview(
                    data,
                    80 if columns is None else columns,
                    24 if rows is None else rows,
                    2.0 if cell_aspect is None else cell_aspect,
                )
                payload.update(
                    {
                        "rgba": base64.b64encode(preview.rgba).decode("ascii"),
                        "width": preview.width,
                        "height": preview.height,
                        "cell_width": preview.cell_width,
                        "cell_height": preview.cell_height,
                        "original_width": preview.original_width,
                        "original_height": preview.original_height,
                    }
                )
            return payload

        if self._looks_binary(sample, continuation=continuation):
            return {
                "kind": "binary",
                "path": self._display(resolved),
                "bytes": size,
                "media_type": media_type,
                "preview_encoding": "hex",
                "preview_bytes": min(len(sample), UI_FILE_BINARY_PREVIEW_BYTES),
                "preview": binascii.hexlify(
                    sample[:UI_FILE_BINARY_PREVIEW_BYTES]
                ).decode("ascii"),
            }

        result = _read_file_local(
            self.config,
            self.store,
            None,
            str(resolved),
            1,
            UI_FILE_PREVIEW_MAX_LINES,
        )
        payload = result.model_dump(mode="json")
        return {
            "kind": "text",
            **payload,
            "media_type": media_type,
            "preview_truncated": bool(
                result.truncated
                or (
                    result.total_lines is not None
                    and result.total_lines > UI_FILE_PREVIEW_MAX_LINES
                )
            ),
        }

    def content(self, path: str) -> dict[str, Any]:
        resolved = self._resolve(path, must_exist=True)
        if not resolved.is_file():
            raise ValueError("Only regular text files can be edited")
        sample, continuation = self._file_sample(resolved)
        if self._looks_binary(sample, continuation=continuation):
            raise ValueError(
                "Binary files cannot be edited in the built-in editor"
            )
        result = _read_file_local(self.config, self.store, None, str(resolved))
        if result.truncated:
            raise ValueError(
                "File exceeds the configured editor read limit; use a terminal or external editor"
            )
        return {
            "kind": "text",
            **result.model_dump(mode="json"),
            "file_sha256": file_sha256(resolved),
        }

    def _ensure_mutable(self, path: str, *, follow_final_symlink: bool) -> Path:
        root = self.config.workspace_root.resolve()
        raw = Path(path)
        candidate = raw if raw.is_absolute() else root / raw
        if Path(os.path.abspath(candidate)) == root:
            raise ValueError("Refusing to mutate the workspace root")
        resolved = self._resolve(
            path,
            must_exist=False,
            follow_final_symlink=follow_final_symlink,
        )
        return resolved

    def _source_entry(self, path: str) -> Path:
        root = self.config.workspace_root.resolve()
        raw = Path(path)
        candidate = raw if raw.is_absolute() else root / raw
        if Path(os.path.abspath(candidate)) == root:
            raise ValueError("Refusing to mutate the workspace root")
        return self._resolve(path, must_exist=True, follow_final_symlink=False)

    def _destination_entry(self, path: str) -> Path:
        resolved = self._ensure_mutable(path, follow_final_symlink=False)
        if not resolved.parent.is_dir():
            raise NotADirectoryError(str(resolved.parent))
        return resolved

    @staticmethod
    def _entry_kind(path: Path) -> str:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            return "link"
        if stat.S_ISREG(mode):
            return "file"
        if stat.S_ISDIR(mode):
            return "dir"
        raise ValueError(
            "Only regular files, directories, and symbolic links can be copied or moved"
        )

    @staticmethod
    def _validate_copy_destination(
        source: Path, destination: Path, kind: str
    ) -> None:
        if source == destination:
            raise ValueError("Source and destination must be different")
        if os.path.lexists(destination):
            raise FileExistsError(str(destination))
        if kind == "dir":
            try:
                destination.relative_to(source)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "A directory cannot be copied or moved inside itself"
                )

    @staticmethod
    def _remove_entry(path: Path) -> None:
        if not os.path.lexists(path):
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    @classmethod
    def _copy_regular_file(cls, source: Path, destination: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        with source.open("rb") as input_handle:
            before = os.fstat(input_handle.fileno())
            fd = os.open(destination, flags, stat.S_IMODE(before.st_mode))
            try:
                with os.fdopen(fd, "wb") as output_handle:
                    shutil.copyfileobj(
                        input_handle, output_handle, length=1024 * 1024
                    )
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
                after = os.fstat(input_handle.fileno())
                signature_before = (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                )
                signature_after = (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                )
                if signature_before != signature_after:
                    raise RuntimeError(
                        "Source file changed while it was being copied"
                    )
                shutil.copystat(source, destination, follow_symlinks=False)
            except BaseException:
                cls._remove_entry(destination)
                raise

    @classmethod
    def _copy_entry_locked(
        cls, source: Path, destination: Path, kind: str
    ) -> None:
        if kind == "file":
            cls._copy_regular_file(source, destination)
            return
        if kind == "link":
            target = os.readlink(source)
            try:
                os.symlink(
                    target,
                    destination,
                    target_is_directory=source.resolve(strict=False).is_dir(),
                )
                with contextlib.suppress(OSError):
                    shutil.copystat(source, destination, follow_symlinks=False)
            except BaseException:
                cls._remove_entry(destination)
                raise
            return
        try:
            shutil.copytree(
                source,
                destination,
                symlinks=True,
                copy_function=shutil.copy2,
            )
        except BaseException:
            cls._remove_entry(destination)
            raise

    def copy_entry(
        self, source_path: str, destination_path: str
    ) -> dict[str, Any]:
        source = self._source_entry(source_path)
        destination = self._destination_entry(destination_path)
        with path_locks([source, destination]):
            if not os.path.lexists(source):
                raise FileNotFoundError(str(source))
            kind = self._entry_kind(source)
            self._validate_copy_destination(source, destination, kind)
            self._copy_entry_locked(source, destination, kind)
        return {
            "action": "copy",
            "source": self._display(source),
            "destination": self._display(destination),
            "type": kind,
        }

    def move_entry(
        self,
        source_path: str,
        destination_path: str,
        *,
        action: str = "move",
    ) -> dict[str, Any]:
        source = self._source_entry(source_path)
        destination = self._destination_entry(destination_path)
        with path_locks([source, destination]):
            if not os.path.lexists(source):
                raise FileNotFoundError(str(source))
            kind = self._entry_kind(source)
            self._validate_copy_destination(source, destination, kind)
            try:
                os.rename(source, destination)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                self._copy_entry_locked(source, destination, kind)
                try:
                    self._remove_entry(source)
                except BaseException:
                    self._remove_entry(destination)
                    raise
        return {
            "action": action,
            "source": self._display(source),
            "destination": self._display(destination),
            "type": kind,
        }

    def rename_entry(self, path: str, name: str) -> dict[str, Any]:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError(
                "name must be one file name without path separators"
            )
        if len(name.encode("utf-8")) > 255:
            raise ValueError("name exceeds 255 encoded bytes")
        source = self._source_entry(path)
        return self.move_entry(
            str(source), str(source.with_name(name)), action="rename"
        )

    def write_file(
        self,
        path: str,
        content: str,
        overwrite: bool,
        expected_sha256: str | None,
    ) -> Any:
        resolved = self._ensure_mutable(path, follow_final_symlink=True)
        return _write_file_local(
            self.config,
            None,
            str(resolved),
            content,
            overwrite,
            expected_sha256,
        )

    def mkdir(self, path: str) -> dict[str, Any]:
        resolved = self._ensure_mutable(path, follow_final_symlink=False)
        with path_locks([resolved]):
            resolved.mkdir(parents=False, exist_ok=False)
        return {"action": "mkdir", "path": self._display(resolved)}

    def delete(self, path: str, recursive: bool) -> Any:
        resolved = self._ensure_mutable(path, follow_final_symlink=False)
        return _delete_file_or_dir_local(
            self.config, None, str(resolved), recursive
        )

    async def execute(self, op: str, args: dict[str, Any]) -> Any:
        """Execute one narrow internal UI file operation."""
        if op == "ui.files.list":
            return self.list_payload(str(args.get("path") or "."))
        if op == "ui.files.preview":
            return self.preview(
                str(args["path"]),
                columns=_optional_int(args.get("columns")),
                rows=_optional_int(args.get("rows")),
                cell_aspect=_optional_float(args.get("cell_aspect")),
            )
        if op == "ui.files.content":
            return self.content(str(args["path"]))
        if op == "ui.files.write":
            content = args.get("content")
            if not isinstance(content, str):
                raise ValueError("content must be a string")
            expected = args.get("expected_sha256")
            return self.write_file(
                str(args["path"]),
                content,
                bool(args.get("overwrite", True)),
                None if expected is None else str(expected),
            )
        if op == "ui.files.mkdir":
            return self.mkdir(str(args["path"]))
        if op == "ui.files.delete":
            return self.delete(
                str(args["path"]), bool(args.get("recursive", False))
            )
        if op == "ui.files.copy":
            return self.copy_entry(str(args["path"]), str(args["destination"]))
        if op == "ui.files.move":
            return self.move_entry(str(args["path"]), str(args["destination"]))
        if op == "ui.files.rename":
            name = args.get("name")
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            return self.rename_entry(str(args["path"]), name.strip())
        raise NotImplementedError(
            f"unsupported executor UI file operation: {op}"
        )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
