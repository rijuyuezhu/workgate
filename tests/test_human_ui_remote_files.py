import asyncio
import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

import workgate.ui.http.remote_files as remote_files_module
from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.oauth.core.scopes import (
    SCOPE_REMOTE_USE,
    SCOPE_SHELL_READ,
    SCOPE_SHELL_WRITE,
)
from workgate.oauth.protocol.token_codec import issue_access_token
from workgate.remote.tool_specs import (
    REMOTE_WORKER_ORIGIN_ARG,
    REMOTE_WORKER_ORIGIN_HUMAN_UI,
)
from workgate.schemas.result_models.remote import (
    RemoteListMachinesOutput,
    RemoteMachineInfo,
)
from workgate.ui.http.live_state import (
    build_human_ui_runtime,
    configure_human_ui_runtime,
)

BASE_URL = "https://workgate.example"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZP2sAAAAASUVORK5CYII="
)
VALID_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


@pytest.mark.parametrize(
    ("path", "message"),
    [("", "path is required"), ("bad\x00path", "NUL")],
)
def test_remote_path_rejects_empty_and_nul(path: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        remote_files_module.normalize_remote_ui_path(path)


class _FakeManager:
    def __init__(self, status: str = "online") -> None:
        self.status = status

    def list_machines(self) -> RemoteListMachinesOutput:
        return RemoteListMachinesOutput(
            machines=[
                RemoteMachineInfo(
                    name="edge",
                    status=self.status,
                    workdir="/srv/workspace",
                    last_seen=1.0,
                    queue_depth=0,
                    capabilities=["files", "transfer"],
                    info={},
                )
            ],
            counts={self.status: 1, "total": 1},
        )


class _FakeRemoteFiles:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {
            "hello.txt": b"hello\nremote\n",
            "name:raw": b"colon selector-safe\n",
            "blob.bin": b"\x00\x01\x02binary",
            "pixel.png": PNG_1X1,
            "docs/note.txt": b"nested\n",
        }
        self.directories = {".", "docs"}
        self.started: list[dict[str, object]] = []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.next_session = 1
        self.stale_once = False
        self.corrupt_chunk_digest_once = False
        self.returned_workdir: str | None = None

    async def start_session(self, **kwargs):  # noqa: ANN003, ANN201
        self.started.append(dict(kwargs))
        session_id = f"remote-files-{self.next_session}"
        self.next_session += 1
        return {
            "session_id": session_id,
            "workdir": self.returned_workdir
            or kwargs.get("workdir", "/srv/workspace"),
        }

    def _entry(self, path: str, type_: str) -> dict[str, object]:
        return {
            "path": path,
            "type": type_,
            "size": len(self.files[path]) if type_ == "file" else None,
            "modified": 1.0,
            "target": None,
        }

    def _list(self, path: str) -> dict[str, object]:
        prefix = "" if path == "." else f"{path}/"
        entries: list[dict[str, object]] = []
        for directory in sorted(self.directories):
            if directory == "." or not directory.startswith(prefix):
                continue
            remainder = directory[len(prefix) :]
            if remainder and "/" not in remainder:
                entries.append(self._entry(directory, "dir"))
        for file_path in sorted(self.files):
            if not file_path.startswith(prefix):
                continue
            remainder = file_path[len(prefix) :]
            if remainder and "/" not in remainder:
                entries.append(self._entry(file_path, "file"))
        return {
            "limit_count": 1000,
            "count": len(entries),
            "is_truncated": False,
            "entries": entries,
        }

    async def call(
        self,
        machine: str,
        tool: str,
        args: dict[str, object],
        timeout_s: int | None,
    ) -> dict[str, object]:
        assert machine == "edge"
        assert timeout_s is not None
        assert 1 <= timeout_s <= 60
        self.calls.append((tool, dict(args)))
        if self.stale_once and tool == "list_files":
            self.stale_once = False
            return {
                "ok": True,
                "data": {
                    "status": "error",
                    "error_type": "ValueError",
                    "message": "Unknown session_id: stale",
                },
            }
        assert str(args.get("session_id", "")).startswith("remote-files-")
        if tool == "list_files":
            return {"ok": True, "data": self._list(str(args["path"]))}
        if tool == "transfer_stat":
            path = str(args["path"])
            if path in self.directories:
                data = {
                    "path": path,
                    "type": "dir",
                    "size": None,
                    "modified": 1.0,
                    "sha256": None,
                }
            else:
                payload = self.files[path]
                data = {
                    "path": path,
                    "type": "file",
                    "size": len(payload),
                    "modified": 1.0,
                    "sha256": None,
                }
            return {"ok": True, "data": data}
        if tool == "transfer_read_chunk":
            path = str(args["path"])
            payload = self.files[path]
            offset = int(str(args["offset"]))
            chunk_size = int(str(args["chunk_size"]))
            chunk = payload[offset : offset + chunk_size]
            digest = hashlib.sha256(chunk).hexdigest()
            if self.corrupt_chunk_digest_once:
                self.corrupt_chunk_digest_once = False
                digest = "0" * 64
            return {
                "ok": True,
                "data": {
                    "path": path,
                    "offset": offset,
                    "bytes": len(chunk),
                    "size": len(payload),
                    "eof": offset + len(chunk) >= len(payload),
                    "sha256": digest,
                    "data_b64": base64.b64encode(chunk).decode("ascii"),
                },
            }
        if tool == "read":
            selector = str(args["path"])
            path = selector.split(":", 1)[0]
            payload = self.files[path]
            content = payload.decode("utf-8")
            lines = content.splitlines()
            return {
                "ok": True,
                "data": {
                    "kind": "file",
                    "path": path,
                    "raw": True,
                    "content": content,
                    "file": {
                        "path": path,
                        "bytes": len(payload),
                        "bytes_read": len(payload),
                        "truncated_bytes": 0,
                        "total_lines": len(lines),
                        "start_line": 1 if lines else None,
                        "end_line": len(lines) if lines else None,
                        "line_count": len(lines),
                        "truncated": False,
                    },
                    "directory": None,
                },
            }
        if tool == "write_file":
            path = str(args["path"])
            overwrite = bool(args["overwrite"])
            expected_sha256 = args.get("expected_sha256")
            if expected_sha256 is not None and (
                path not in self.files
                or hashlib.sha256(self.files[path]).hexdigest()
                != expected_sha256
            ):
                return {
                    "ok": True,
                    "data": {
                        "status": "error",
                        "error_type": "ValueError",
                        "message": "File changed; reload before saving",
                    },
                }
            if not overwrite and path in self.files:
                return {
                    "ok": True,
                    "data": {
                        "status": "error",
                        "error_type": "FileExistsError",
                        "message": path,
                    },
                }
            self.files[path] = str(args["content"]).encode("utf-8")
            return {
                "ok": True,
                "data": {
                    "path": path,
                    "bytes": len(self.files[path]),
                    "created": not overwrite,
                },
            }
        if tool == "delete_file_or_dir":
            path = str(args["path"])
            if path in self.directories:
                prefix = f"{path}/"
                if not args["recursive"] and any(
                    item.startswith(prefix)
                    for item in [*self.files, *self.directories]
                ):
                    return {
                        "ok": True,
                        "data": {
                            "status": "error",
                            "error_type": "OSError",
                            "message": "directory is not empty",
                        },
                    }
                self.files = {
                    key: value
                    for key, value in self.files.items()
                    if not key.startswith(prefix)
                }
                self.directories = {
                    item
                    for item in self.directories
                    if item != path and not item.startswith(prefix)
                }
                deleted = "dir"
            else:
                del self.files[path]
                deleted = "file"
            return {"ok": True, "data": {"path": path, "deleted": deleted}}
        raise AssertionError(f"Unexpected tool: {tool}")


@pytest.fixture(autouse=True)
def _reset_state():
    clear_settings_cache()
    previous = configure_human_ui_runtime(build_human_ui_runtime())
    try:
        yield
    finally:
        configure_human_ui_runtime(previous)
        clear_settings_cache()


def _configure(
    monkeypatch,
    workspace,
    *,
    auth_mode="none",
    max_file_read_bytes: int | None = None,
) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(workspace / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "true")
    if max_file_read_bytes is not None:
        monkeypatch.setenv(
            "WORKGATE_MAX_FILE_READ_BYTES",
            str(max_file_read_bytes),
        )
    clear_settings_cache()


def _client(
    monkeypatch,
    tmp_path,
    fake: _FakeRemoteFiles,
    *,
    auth_mode="none",
    max_file_read_bytes: int | None = None,
) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _configure(
        monkeypatch,
        workspace,
        auth_mode=auth_mode,
        max_file_read_bytes=max_file_read_bytes,
    )
    monkeypatch.setattr(
        remote_files_module, "remote_manager", lambda: _FakeManager()
    )
    monkeypatch.setattr(
        remote_files_module, "start_worker_session", fake.start_session
    )
    configure_human_ui_runtime(build_human_ui_runtime(fake.call))
    return TestClient(
        build_http_app(),
        base_url=BASE_URL,
        client=("203.0.113.12", 50002),
    )


def _token(scope: str) -> str:
    return issue_access_token(
        client_id="webui-remote-files-test",
        scope=scope,
        resource=f"{BASE_URL}/mcp",
    )


def test_remote_files_browse_preview_edit_create_and_delete(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteFiles()
    client = _client(monkeypatch, tmp_path, fake)

    listing = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "."},
    )
    text_preview = client.get(
        "/api/ui/files/preview",
        params={"machine": "edge", "path": "hello.txt"},
    )
    binary_preview = client.get(
        "/api/ui/files/preview",
        params={"machine": "edge", "path": "blob.bin"},
    )
    image_preview = client.get(
        "/api/ui/files/preview",
        params={"machine": "edge", "path": "pixel.png"},
    )
    content = client.get(
        "/api/ui/files/content",
        params={"machine": "edge", "path": "hello.txt"},
    )
    colon_preview = client.get(
        "/api/ui/files/preview",
        params={"machine": "edge", "path": "name:raw"},
    )
    colon_content = client.get(
        "/api/ui/files/content",
        params={"machine": "edge", "path": "name:raw"},
    )
    created = client.post(
        "/api/ui/files/write",
        json={
            "machine": "edge",
            "path": "created.txt",
            "content": "created remotely\n",
            "overwrite": False,
        },
    )
    deleted = client.post(
        "/api/ui/files/delete",
        json={
            "machine": "edge",
            "path": "created.txt",
            "recursive": False,
        },
    )

    assert listing.status_code == 200
    listing_data = listing.json()["data"]
    assert listing_data["machine"] == "edge"
    assert listing_data["remote"] is True

    assert listing_data["mutations"] == {
        "write": True,
        "delete": True,
        "copy": False,
        "move": False,
        "rename": False,
        "mkdir": False,
    }
    assert [entry["name"] for entry in listing_data["entries"]] == [
        "docs",
        "blob.bin",
        "hello.txt",
        "name:raw",
        "pixel.png",
    ]
    assert text_preview.status_code == 200
    assert text_preview.json()["data"]["kind"] == "text"
    assert text_preview.json()["data"]["content"] == "hello\nremote\n"
    assert binary_preview.status_code == 200
    assert binary_preview.json()["data"]["kind"] == "binary"
    assert binary_preview.json()["data"]["preview"].startswith("000102")
    assert image_preview.status_code == 200
    assert image_preview.json()["data"]["kind"] == "image"
    assert image_preview.json()["data"]["inline"] is True
    assert content.status_code == 200
    assert content.json()["data"]["content"] == "hello\nremote\n"
    assert colon_preview.status_code == 200
    assert colon_preview.json()["data"]["content"] == "colon selector-safe\n"
    assert colon_content.status_code == 200
    assert colon_content.json()["data"]["content"] == "colon selector-safe\n"
    assert not any(tool == "read" for tool, _ in fake.calls)
    assert created.status_code == 200
    write_call = next(args for tool, args in fake.calls if tool == "write_file")
    assert write_call["path"] == "created.txt"
    assert write_call["content"] == "created remotely\n"
    assert deleted.status_code == 200
    assert "created.txt" not in fake.files
    assert len(fake.started) == 1
    assert fake.started[0]["timeout_s"] == 60
    assert {str(args["session_id"]) for _, args in fake.calls} == {
        "remote-files-1"
    }
    assert {str(args[REMOTE_WORKER_ORIGIN_ARG]) for _, args in fake.calls} == {
        REMOTE_WORKER_ORIGIN_HUMAN_UI
    }


def test_remote_files_recreate_stale_worker_session(monkeypatch, tmp_path):
    fake = _FakeRemoteFiles()
    fake.stale_once = True
    client = _client(monkeypatch, tmp_path, fake)

    response = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "."},
    )

    assert response.status_code == 200
    assert len(fake.started) == 2
    list_sessions = [
        str(args["session_id"])
        for tool, args in fake.calls
        if tool == "list_files"
    ]
    assert list_sessions == ["remote-files-1", "remote-files-2"]


def test_remote_files_cache_uses_inventory_workdir_key(monkeypatch, tmp_path):
    fake = _FakeRemoteFiles()
    fake.returned_workdir = "/srv/workspace/."
    client = _client(monkeypatch, tmp_path, fake)

    first = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "."},
    )
    second = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "."},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(fake.started) == 1


def test_remote_files_reject_unsafe_paths_and_unsupported_mutations(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteFiles()
    client = _client(monkeypatch, tmp_path, fake)

    absolute = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "/etc"},
    )
    drive_relative = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "C:outside.txt"},
    )
    home = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "~/outside.txt"},
    )
    parent = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "../outside"},
    )
    root_delete = client.post(
        "/api/ui/files/delete",
        json={"machine": "edge", "path": ".", "recursive": True},
    )
    copy = client.post(
        "/api/ui/files/copy",
        json={
            "machine": "edge",
            "path": "hello.txt",
            "destination": "copy.txt",
        },
    )

    assert absolute.status_code == 400
    assert "workspace-relative" in absolute.json()["message"]
    assert drive_relative.status_code == 400
    assert "workspace-relative" in drive_relative.json()["message"]
    assert home.status_code == 400
    assert "home expansion" in home.json()["message"]
    assert parent.status_code == 400
    assert "must not contain '..'" in parent.json()["message"]
    assert root_delete.status_code == 400
    assert "workspace root" in root_delete.json()["message"]
    assert copy.status_code == 400
    assert "does not support copy" in copy.json()["message"]
    assert not fake.calls


def test_remote_files_require_remote_and_write_scopes(monkeypatch, tmp_path):
    fake = _FakeRemoteFiles()
    client = _client(monkeypatch, tmp_path, fake, auth_mode="oauth")
    read_only = {"Authorization": f"Bearer {_token(SCOPE_SHELL_READ)}"}
    remote_read = {
        "Authorization": f"Bearer {_token(f'{SCOPE_SHELL_READ} {SCOPE_REMOTE_USE}')}"
    }
    full = {
        "Authorization": f"Bearer {_token(f'{SCOPE_SHELL_READ} {SCOPE_SHELL_WRITE} {SCOPE_REMOTE_USE}')}"
    }

    missing_remote = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "."},
        headers=read_only,
    )
    readable = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "."},
        headers=remote_read,
    )
    missing_write = client.post(
        "/api/ui/files/write",
        json={
            "machine": "edge",
            "path": "new.txt",
            "content": "new",
            "overwrite": False,
        },
        headers=remote_read,
    )
    writable = client.post(
        "/api/ui/files/write",
        json={
            "machine": "edge",
            "path": "new.txt",
            "content": "new",
            "overwrite": False,
        },
        headers=full,
    )

    assert missing_remote.status_code == 403
    assert SCOPE_REMOTE_USE in missing_remote.text
    assert readable.status_code == 200
    assert missing_write.status_code == 403
    assert SCOPE_SHELL_WRITE in missing_write.text
    assert writable.status_code == 200


def test_remote_files_bound_large_text_preview_and_refuse_editor(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteFiles()
    fake.files["large.txt"] = b"alpha\nbeta\ngamma\ndelta\nepsilon\n"
    client = _client(
        monkeypatch,
        tmp_path,
        fake,
        max_file_read_bytes=16,
    )

    preview = client.get(
        "/api/ui/files/preview",
        params={"machine": "edge", "path": "large.txt"},
    )
    chunks_before_editor = sum(
        tool == "transfer_read_chunk" for tool, _ in fake.calls
    )
    content = client.get(
        "/api/ui/files/content",
        params={"machine": "edge", "path": "large.txt"},
    )
    chunks_after_editor = sum(
        tool == "transfer_read_chunk" for tool, _ in fake.calls
    )

    assert preview.status_code == 200
    data = preview.json()["data"]
    assert data["kind"] == "text"
    assert data["bytes_read"] == 16
    assert data["truncated"] is True
    assert data["preview_truncated"] is True
    assert data["content"] == "alpha\nbeta\ngamma"
    assert content.status_code == 400
    assert "editor read limit" in content.json()["message"]
    assert chunks_after_editor == chunks_before_editor


def test_remote_files_keep_utf8_when_limit_splits_multibyte_character(
    monkeypatch, tmp_path
):
    fake = _FakeRemoteFiles()
    fake.files["utf8-boundary.txt"] = b"a" * 4_095 + "é\n".encode()
    client = _client(
        monkeypatch,
        tmp_path,
        fake,
        max_file_read_bytes=4_096,
    )

    preview = client.get(
        "/api/ui/files/preview",
        params={"machine": "edge", "path": "utf8-boundary.txt"},
    )
    content = client.get(
        "/api/ui/files/content",
        params={"machine": "edge", "path": "utf8-boundary.txt"},
    )

    assert preview.status_code == 200
    data = preview.json()["data"]
    assert data["kind"] == "text"
    assert data["content"] == "a" * 4_095
    assert data["bytes_read"] == 4_096
    assert data["truncated"] is True
    assert data["preview_truncated"] is True
    assert content.status_code == 400
    assert "editor read limit" in content.json()["message"]


def test_remote_files_reject_corrupt_chunk_digest(monkeypatch, tmp_path):
    fake = _FakeRemoteFiles()
    fake.corrupt_chunk_digest_once = True
    client = _client(monkeypatch, tmp_path, fake)

    response = client.get(
        "/api/ui/files/preview",
        params={"machine": "edge", "path": "hello.txt"},
    )

    assert response.status_code == 400
    assert "chunk metadata" in response.json()["message"]


@pytest.mark.asyncio
async def test_remote_file_machine_lock_wait_is_cancellation_safe():
    lock = remote_files_module._machine_lock("edge")
    lock.acquire()
    try:
        waiter = asyncio.create_task(
            remote_files_module._acquire_machine_lock("edge")
        )
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        lock.release()

    acquired = await asyncio.wait_for(
        remote_files_module._acquire_machine_lock("edge"),
        timeout=0.2,
    )
    acquired.release()


@pytest.mark.parametrize("status", ["offline", "revoked"])
def test_remote_files_reject_unavailable_machine(monkeypatch, tmp_path, status):
    fake = _FakeRemoteFiles()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _configure(monkeypatch, workspace)
    monkeypatch.setattr(
        remote_files_module,
        "remote_manager",
        lambda: _FakeManager(status=status),
    )
    monkeypatch.setattr(
        remote_files_module, "start_worker_session", fake.start_session
    )
    client = TestClient(build_http_app(), base_url=BASE_URL)

    response = client.get(
        "/api/ui/files",
        params={"machine": "edge", "path": "."},
    )

    assert response.status_code == 400
    assert status in response.json()["message"]
    assert not fake.started


def test_remote_opentui_preview_and_revision_guard(monkeypatch, tmp_path):
    fake = _FakeRemoteFiles()
    fake.files["pixel.png"] = VALID_PNG_1X1
    client = _client(monkeypatch, tmp_path, fake)

    preview = client.get(
        "/api/ui/files/preview",
        params={
            "machine": "edge",
            "path": "pixel.png",
            "columns": 12,
            "rows": 6,
            "cell_aspect": 2,
        },
    )
    content = client.get(
        "/api/ui/files/content",
        params={"machine": "edge", "path": "hello.txt"},
    ).json()["data"]
    saved = client.post(
        "/api/ui/files/write",
        json={
            "machine": "edge",
            "path": "hello.txt",
            "content": "updated remotely\n",
            "overwrite": True,
            "expected_sha256": content["file_sha256"],
        },
    )
    stale = client.post(
        "/api/ui/files/write",
        json={
            "machine": "edge",
            "path": "hello.txt",
            "content": "stale remotely\n",
            "overwrite": True,
            "expected_sha256": content["file_sha256"],
        },
    )

    assert preview.status_code == 200
    image = preview.json()["data"]
    assert (
        len(base64.b64decode(image["rgba"]))
        == image["width"] * image["height"] * 4
    )
    assert (
        content["file_sha256"] == hashlib.sha256(b"hello\nremote\n").hexdigest()
    )
    assert saved.status_code == 200
    assert stale.status_code == 400
    assert fake.files["hello.txt"] == b"updated remotely\n"
    writes = [args for tool, args in fake.calls if tool == "write_file"]
    assert writes[0]["expected_sha256"] == content["file_sha256"]
