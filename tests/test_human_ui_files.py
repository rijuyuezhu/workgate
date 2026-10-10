import base64
import errno
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

import workgate.executor.ui_files as executor_ui_files_module
import workgate.ui.http.files as ui_files_module
from tests.helpers import build_paired_http_app
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.oauth.core.scopes import (
    SCOPE_SHELL_READ,
    SCOPE_SHELL_WRITE,
)
from workgate.oauth.protocol.token_codec import issue_access_token

BASE_URL = "https://workgate.example"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZP2sAAAAASUVORK5CYII="
)
VALID_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def _reset_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


def _configure(
    monkeypatch,
    workspace,
    *,
    auth_mode="none",
    **values,
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(workspace.parent / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    for name, value in values.items():
        monkeypatch.setenv(f"WORKGATE_{name.upper()}", str(value).lower())
    clear_settings_cache()


class _ExecutorTestClient(TestClient):
    def __init__(self, *args: Any, executor_id: str, **kwargs: Any) -> None:
        self.executor_id = executor_id
        super().__init__(*args, **kwargs)

    def request(self, method: str, url: Any, **kwargs: Any):
        if method.upper() in {"GET", "HEAD"}:
            params = dict(kwargs.get("params") or {})
            params.setdefault("executor_id", self.executor_id)
            kwargs["params"] = params
        else:
            body = kwargs.get("json")
            if isinstance(body, dict):
                body = dict(body)
                body.setdefault("executor_id", self.executor_id)
                kwargs["json"] = body
        return super().request(method, url, **kwargs)


def _client(monkeypatch, workspace, **values) -> _ExecutorTestClient:
    _configure(monkeypatch, workspace, **values)
    app, harness = build_paired_http_app(get_settings())
    return _ExecutorTestClient(
        app,
        executor_id=harness.executor_id,
        base_url=BASE_URL,
        client=("203.0.113.11", 50001),
    )


def _token(scope: str) -> str:
    return issue_access_token(
        client_id="webui-files-test",
        scope=scope,
        resource=f"{BASE_URL}/mcp",
    )


def test_file_listing_is_sorted_bounded_and_can_navigate_above_default(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "z-dir").mkdir()
    (workspace / "a-dir").mkdir()
    (workspace / "z.txt").write_text("z", encoding="utf-8")
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / ".hidden").write_text("hidden", encoding="utf-8")
    client = _client(monkeypatch, workspace)

    response = client.get("/api/ui/files", params={"path": "."})

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["executor_id"] == client.executor_id
    assert payload["path"] == "."
    assert payload["parent"] == tmp_path.as_posix()
    assert payload["is_truncated"] is False

    assert payload["mutations"] == {
        "write": True,
        "delete": True,
        "copy": True,
        "move": True,
        "rename": True,
        "mkdir": True,
    }
    assert [entry["name"] for entry in payload["entries"]] == [
        "a-dir",
        "z-dir",
        ".hidden",
        "a.txt",
        "z.txt",
    ]
    hidden = next(
        entry for entry in payload["entries"] if entry["name"] == ".hidden"
    )
    assert hidden["hidden"] is True
    assert all(not os.path.isabs(entry["path"]) for entry in payload["entries"])


def test_file_api_can_access_paths_outside_default_workdir(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)
    client = _client(monkeypatch, workspace)

    listed = client.get("/api/ui/files", params={"path": str(outside)})
    previewed = client.get(
        "/api/ui/files/preview",
        params={"path": str(outside / "secret.txt")},
    )
    written = client.post(
        "/api/ui/files/write",
        json={"path": str(outside / "new.txt"), "content": "escape"},
    )
    linked_write = client.post(
        "/api/ui/files/write",
        json={"path": "outside-link/linked.txt", "content": "escape"},
    )

    for response in (listed, previewed, written, linked_write):
        assert response.status_code == 200
    assert (outside / "new.txt").read_text(encoding="utf-8") == "escape"
    assert (outside / "linked.txt").read_text(encoding="utf-8") == "escape"


def test_browser_binary_upload_is_bounded_and_does_not_overwrite(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = _client(monkeypatch, workspace)
    source = b"\x00\xff\x80\xfe"
    body = {
        "path": "binary.bin",
        "data_base64": base64.b64encode(source).decode(),
    }

    created = client.post("/api/ui/files/upload", json=body)
    duplicate = client.post("/api/ui/files/upload", json=body)
    bad_encoded = client.post(
        "/api/ui/files/upload",
        json={"path": "invalid.bin", "data_base64": "%%%"},
    )
    over_limit = client.post(
        "/api/ui/files/upload",
        json={"path": "too-large.bin", "data_base64": "A" * 2_666_672},
    )
    missing_folder = client.post(
        "/api/ui/files/upload",
        json={"path": "missing/file.bin", "data_base64": ""},
    )
    assert created.status_code == 200
    assert created.json()["data"]["bytes"] == len(source)
    assert workspace.joinpath("binary.bin").read_bytes() == source
    assert duplicate.status_code == 400
    assert duplicate.json()["error"] == "FileExistsError"
    assert bad_encoded.status_code == 400
    assert "Invalid base64" in bad_encoded.json()["message"]
    assert over_limit.status_code == 400
    assert "Upload exceeds" in over_limit.json()["message"]
    assert missing_folder.status_code == 400
    assert not workspace.joinpath("too-large.bin").exists()

    # Base64 can decode to one byte over the bound without exceeding the
    # maximum encoded string length.
    decoded_over_limit = client.post(
        "/api/ui/files/upload",
        json={
            "path": "decoded-too-large.bin",
            "data_base64": base64.b64encode(b"x" * 2_000_001).decode("ascii"),
        },
    )
    assert decoded_over_limit.status_code == 400
    assert "Upload exceeds" in decoded_over_limit.json()["message"]
    assert not workspace.joinpath("decoded-too-large.bin").exists()

    def reject_flush(_descriptor: int) -> None:
        raise OSError("simulated upload write failure")

    with monkeypatch.context() as patch:
        patch.setattr(executor_ui_files_module.os, "fsync", reject_flush)
        failed_write = client.post(
            "/api/ui/files/upload",
            json={"path": "failed-write.bin", "data_base64": "YQ=="},
        )
    assert failed_write.status_code == 400
    assert "simulated upload write failure" in failed_write.json()["message"]
    assert not workspace.joinpath("failed-write.bin").exists()


def test_file_api_refuses_filesystem_root_mutations(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("source", encoding="utf-8")
    client = _client(monkeypatch, workspace)
    filesystem_root = Path(tmp_path.anchor).as_posix()

    mkdir = client.post(
        "/api/ui/files/mkdir",
        json={"path": filesystem_root},
    )
    copy = client.post(
        "/api/ui/files/copy",
        json={"path": filesystem_root, "destination": "root-copy"},
    )
    missing_parent = client.post(
        "/api/ui/files/copy",
        json={
            "path": "source.txt",
            "destination": "missing-parent/copied.txt",
        },
    )

    assert mkdir.status_code == 400
    assert "filesystem root" in mkdir.json()["message"]
    assert copy.status_code == 400
    assert "filesystem root" in copy.json()["message"]
    assert missing_parent.status_code == 400
    assert missing_parent.json()["error"] == "NotADirectoryError"


def test_file_preview_supports_text_binary_directory_and_raster_images(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    folder = workspace / "folder"
    folder.mkdir()
    (folder / "child.txt").write_text("child", encoding="utf-8")
    (workspace / "notes.txt").write_text("alpha\nbeta", encoding="utf-8")
    (workspace / "blob.bin").write_bytes(b"\x00\x01\xff")
    (workspace / "pixel.png").write_bytes(PNG_1X1)
    (workspace / "vector.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        encoding="utf-8",
    )
    client = _client(monkeypatch, workspace)

    directory = client.get(
        "/api/ui/files/preview", params={"path": "folder"}
    ).json()["data"]
    text = client.get(
        "/api/ui/files/preview", params={"path": "notes.txt"}
    ).json()["data"]
    binary = client.get(
        "/api/ui/files/preview", params={"path": "blob.bin"}
    ).json()["data"]
    image = client.get(
        "/api/ui/files/preview", params={"path": "pixel.png"}
    ).json()["data"]
    svg = client.get(
        "/api/ui/files/preview", params={"path": "vector.svg"}
    ).json()["data"]

    assert directory["kind"] == "directory"
    assert directory["entries"][0]["name"] == "child.txt"
    assert text["kind"] == "text"
    assert text["content"] == "alpha\nbeta"
    assert binary == {
        "kind": "binary",
        "path": "blob.bin",
        "bytes": 3,
        "media_type": "application/octet-stream",
        "preview_encoding": "hex",
        "preview_bytes": 3,
        "preview": "0001ff",
    }
    assert image["kind"] == "image"
    assert image["media_type"] == "image/png"
    assert image["inline"] is True
    assert base64.b64decode(image["data_base64"]) == PNG_1X1
    assert svg["kind"] == "text"
    assert svg["media_type"] == "image/svg+xml"
    assert "<script>" in svg["content"]
    assert "data_base64" not in svg


def test_file_preview_respects_image_read_limit_and_editor_rejects_directory(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = workspace / "oversized.png"
    image.write_bytes(PNG_1X1 + b"x" * 100)
    (workspace / "folder").mkdir()
    client = _client(monkeypatch, workspace, max_file_read_bytes=64)

    response = client.get(
        "/api/ui/files/preview", params={"path": "oversized.png"}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["kind"] == "image"
    assert data["inline"] is False
    assert "data_base64" not in data
    assert "64 bytes" in data["message"]

    directory = client.get("/api/ui/files/content", params={"path": "folder"})
    assert directory.status_code == 400
    assert "Only regular text files" in directory.json()["message"]


def test_local_file_preview_handles_utf8_split_at_binary_probe_boundary(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    valid_content = "a" * 4_095 + "模型"
    (workspace / "boundary.txt").write_text(valid_content, encoding="utf-8")
    (workspace / "invalid.txt").write_bytes(b"a" * 4_095 + b"\xe6x")
    client = _client(monkeypatch, workspace)

    preview = client.get(
        "/api/ui/files/preview", params={"path": "boundary.txt"}
    )
    content = client.get(
        "/api/ui/files/content", params={"path": "boundary.txt"}
    )
    invalid_preview = client.get(
        "/api/ui/files/preview", params={"path": "invalid.txt"}
    )
    invalid_content = client.get(
        "/api/ui/files/content", params={"path": "invalid.txt"}
    )

    assert preview.status_code == 200
    assert preview.json()["data"]["kind"] == "text"
    assert preview.json()["data"]["content"] == valid_content
    assert content.status_code == 200
    assert content.json()["data"]["content"] == valid_content
    assert invalid_preview.status_code == 200
    assert invalid_preview.json()["data"]["kind"] == "binary"
    assert invalid_content.status_code == 400
    assert "Binary files" in invalid_content.json()["message"]


def test_editor_reads_complete_text_and_rejects_binary_or_truncated_files(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    complete = "\n".join(f"line-{index}" for index in range(40))
    (workspace / "complete.txt").write_bytes(complete.encode("utf-8"))
    (workspace / "binary.bin").write_bytes(b"\x00binary")
    (workspace / "large.txt").write_text("x" * 200, encoding="utf-8")
    client = _client(
        monkeypatch,
        workspace,
        max_file_read_bytes=64,
    )

    complete_response = client.get(
        "/api/ui/files/content", params={"path": "complete.txt"}
    )
    binary_response = client.get(
        "/api/ui/files/content", params={"path": "binary.bin"}
    )
    large_response = client.get(
        "/api/ui/files/content", params={"path": "large.txt"}
    )

    assert complete_response.status_code == 400
    assert "editor read limit" in complete_response.json()["message"]
    assert binary_response.status_code == 400
    assert "Binary files" in binary_response.json()["message"]
    assert large_response.status_code == 400
    assert "editor read limit" in large_response.json()["message"]

    clear_settings_cache()
    monkeypatch.setenv("WORKGATE_MAX_FILE_READ_BYTES", "4096")
    complete_app, harness = build_paired_http_app(get_settings())
    complete_client = _ExecutorTestClient(
        complete_app, executor_id=harness.executor_id, base_url=BASE_URL
    )
    payload = complete_client.get(
        "/api/ui/files/content", params={"path": "complete.txt"}
    ).json()["data"]
    assert payload["content"] == complete
    assert payload["truncated"] is False


def test_file_mutations_require_write_scope_and_preserve_safe_semantics(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "mode.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)
    client = _client(monkeypatch, workspace, auth_mode="oauth")
    read_headers = {"Authorization": f"Bearer {_token(SCOPE_SHELL_READ)}"}
    write_headers = {
        "Authorization": (
            "Bearer " + _token(f"{SCOPE_SHELL_READ} {SCOPE_SHELL_WRITE}")
        )
    }

    assert client.get("/api/ui/files", headers=read_headers).status_code == 200
    denied = client.post(
        "/api/ui/files/write",
        json={"path": "mode.txt", "content": "new"},
        headers=read_headers,
    )
    assert denied.status_code == 403
    assert SCOPE_SHELL_WRITE in denied.text

    written = client.post(
        "/api/ui/files/write",
        json={"path": "mode.txt", "content": "new", "overwrite": True},
        headers=write_headers,
    )
    assert written.status_code == 200
    assert target.read_text(encoding="utf-8") == "new"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o640

    created = client.post(
        "/api/ui/files/write",
        json={"path": "new.txt", "content": "created", "overwrite": False},
        headers=write_headers,
    )
    assert created.status_code == 200
    assert created.json()["data"]["created"] is True


def test_delete_allows_default_workdir_and_unlinks_symlink_not_target(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = workspace / "outside-link"
    link.symlink_to(outside)
    client = _client(monkeypatch, workspace)

    deleted = client.post(
        "/api/ui/files/delete",
        json={"path": "outside-link", "recursive": False},
    )
    root = client.post(
        "/api/ui/files/delete", json={"path": ".", "recursive": True}
    )

    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] == "link"
    assert not link.exists()
    assert outside.read_text(encoding="utf-8") == "keep"
    assert root.status_code == 200
    assert not workspace.exists()


def test_copy_file_preserves_content_mode_and_source(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_text("copy me", encoding="utf-8")
    source.chmod(0o640)
    client = _client(monkeypatch, workspace)

    response = client.post(
        "/api/ui/files/copy",
        json={"path": "source.txt", "destination": "copied.txt"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "action": "copy",
        "source": "source.txt",
        "destination": "copied.txt",
        "type": "file",
        "executor_id": client.executor_id,
    }
    assert source.read_text(encoding="utf-8") == "copy me"
    copied = workspace / "copied.txt"
    assert copied.read_text(encoding="utf-8") == "copy me"
    if os.name != "nt":
        assert copied.stat().st_mode & 0o777 == 0o640


def test_copy_directory_preserves_symlinks_without_following_targets(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    source = workspace / "source"
    source.mkdir()
    (source / "child.txt").write_text("child", encoding="utf-8")
    (source / "outside-link").symlink_to(outside)
    client = _client(monkeypatch, workspace)

    response = client.post(
        "/api/ui/files/copy",
        json={"path": "source", "destination": "copied"},
    )

    assert response.status_code == 200
    copied = workspace / "copied"
    assert (copied / "child.txt").read_text(encoding="utf-8") == "child"
    copied_link = copied / "outside-link"
    assert copied_link.is_symlink()
    assert os.readlink(copied_link) == os.readlink(source / "outside-link")
    assert outside.read_text(encoding="utf-8") == "outside"


def test_move_and_rename_entries(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    destination_dir = workspace / "destination"
    destination_dir.mkdir()
    (workspace / "move.txt").write_text("moved", encoding="utf-8")
    rename_dir = workspace / "old-dir"
    rename_dir.mkdir()
    (rename_dir / "child.txt").write_text("child", encoding="utf-8")
    client = _client(monkeypatch, workspace)

    moved = client.post(
        "/api/ui/files/move",
        json={"path": "move.txt", "destination": "destination/move.txt"},
    )
    renamed = client.post(
        "/api/ui/files/rename",
        json={"path": "old-dir", "name": "new-dir"},
    )

    assert moved.status_code == 200
    assert moved.json()["data"]["destination"] == "destination/move.txt"
    assert not (workspace / "move.txt").exists()
    assert (destination_dir / "move.txt").read_text(encoding="utf-8") == "moved"
    assert renamed.status_code == 200
    assert renamed.json()["data"]["action"] == "rename"
    assert renamed.json()["data"]["destination"] == "new-dir"
    assert not rename_dir.exists()
    assert (workspace / "new-dir" / "child.txt").read_text(
        encoding="utf-8"
    ) == "child"


def test_move_cross_device_fallback_copies_then_deletes(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.bin"
    source.write_bytes(b"cross-device")
    client = _client(monkeypatch, workspace)

    def cross_device(_source, _destination):  # noqa: ANN001
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(executor_ui_files_module.os, "rename", cross_device)
    response = client.post(
        "/api/ui/files/move",
        json={"path": "source.bin", "destination": "destination.bin"},
    )

    assert response.status_code == 200
    assert not source.exists()
    assert (workspace / "destination.bin").read_bytes() == b"cross-device"


@pytest.mark.parametrize("action", ["copy", "move"])
def test_copy_and_move_refuse_existing_or_unsafe_destinations(
    monkeypatch, tmp_path, action
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("source", encoding="utf-8")
    (workspace / "existing.txt").write_text("existing", encoding="utf-8")
    source_dir = workspace / "source-dir"
    source_dir.mkdir()
    client = _client(monkeypatch, workspace)

    existing = client.post(
        f"/api/ui/files/{action}",
        json={"path": "source.txt", "destination": "existing.txt"},
    )
    same = client.post(
        f"/api/ui/files/{action}",
        json={"path": "source.txt", "destination": "source.txt"},
    )
    nested = client.post(
        f"/api/ui/files/{action}",
        json={"path": "source-dir", "destination": "source-dir/nested"},
    )
    root = client.post(
        f"/api/ui/files/{action}",
        json={"path": ".", "destination": "root-copy"},
    )

    assert existing.status_code == 400
    assert existing.json()["error"] == "FileExistsError"
    assert same.status_code == 400
    assert "different" in same.json()["message"]
    assert nested.status_code == 400
    assert "inside itself" in nested.json()["message"]
    assert root.status_code == 400
    assert "inside itself" in root.json()["message"]
    assert (workspace / "existing.txt").read_text(
        encoding="utf-8"
    ) == "existing"


@pytest.mark.parametrize("action", ["copy", "move"])
def test_copy_and_move_allow_destination_symlink_outside_default(
    monkeypatch, tmp_path, action
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "source.txt").write_text("source", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)
    client = _client(monkeypatch, workspace)

    response = client.post(
        f"/api/ui/files/{action}",
        json={
            "path": "source.txt",
            "destination": "outside-link/escaped.txt",
        },
    )

    assert response.status_code == 200
    assert (outside / "escaped.txt").read_text(encoding="utf-8") == "source"
    assert (workspace / "source.txt").exists() is (action == "copy")


def test_rename_refuses_existing_destination(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("source", encoding="utf-8")
    (workspace / "existing.txt").write_text("existing", encoding="utf-8")
    client = _client(monkeypatch, workspace)

    response = client.post(
        "/api/ui/files/rename",
        json={"path": "source.txt", "name": "existing.txt"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "FileExistsError"
    assert (workspace / "source.txt").read_text(encoding="utf-8") == "source"
    assert (workspace / "existing.txt").read_text(
        encoding="utf-8"
    ) == "existing"


@pytest.mark.parametrize("name", ["", ".", "..", "nested/name", "nested\\name"])
def test_rename_rejects_invalid_names(monkeypatch, tmp_path, name):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("source", encoding="utf-8")
    client = _client(monkeypatch, workspace)

    response = client.post(
        "/api/ui/files/rename",
        json={"path": "source.txt", "name": name},
    )

    assert response.status_code == 400
    assert "one file name" in response.json()["message"]
    assert (workspace / "source.txt").exists()


def test_copy_move_and_rename_require_write_scope(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("source", encoding="utf-8")
    client = _client(monkeypatch, workspace, auth_mode="oauth")
    read_headers = {"Authorization": f"Bearer {_token(SCOPE_SHELL_READ)}"}

    responses = [
        client.post(
            "/api/ui/files/copy",
            json={"path": "source.txt", "destination": "copy.txt"},
            headers=read_headers,
        ),
        client.post(
            "/api/ui/files/move",
            json={"path": "source.txt", "destination": "move.txt"},
            headers=read_headers,
        ),
        client.post(
            "/api/ui/files/rename",
            json={"path": "source.txt", "name": "renamed.txt"},
            headers=read_headers,
        ),
    ]

    assert all(response.status_code == 403 for response in responses)
    assert all(SCOPE_SHELL_WRITE in response.text for response in responses)
    assert (workspace / "source.txt").exists()


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("", "path is required"),
        ("bad\x00path", "NUL"),
        ("x" * 4_097, "path exceeds"),
    ],
)
def test_file_api_rejects_invalid_paths(monkeypatch, tmp_path, path, message):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = _client(monkeypatch, workspace)

    response = client.get("/api/ui/files/preview", params={"path": path})

    assert response.status_code == 400
    assert message in response.json()["message"]


def test_file_executor_id_arg_requires_value_and_rejects_oversized() -> None:
    with pytest.raises(ValueError, match="executor_id is required"):
        ui_files_module._executor_id_arg("   ")
    with pytest.raises(
        ValueError, match="executor_id exceeds 255 encoded bytes"
    ):
        ui_files_module._executor_id_arg("x" * 256)


def test_file_http_helpers_reject_bad_runtime_and_payload() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
    )
    with pytest.raises(RuntimeError, match="requires the control runtime"):
        ui_files_module._runtime(cast(Any, request))
    with pytest.raises(RuntimeError, match="malformed WebUI Files payload"):
        ui_files_module._payload("not-a-mapping", "exec_test")


def test_webui_image_preview_editor_revision_and_mkdir(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_path = workspace / "pixel.png"
    image_path.write_bytes(VALID_PNG_1X1)
    document = workspace / "document.txt"
    document.write_text("first\n", encoding="utf-8")
    client = _client(monkeypatch, workspace)

    preview = client.get("/api/ui/files/preview", params={"path": "pixel.png"})
    content = client.get(
        "/api/ui/files/content", params={"path": "document.txt"}
    ).json()["data"]
    saved = client.post(
        "/api/ui/files/write",
        json={
            "path": "document.txt",
            "content": "second\n",
            "overwrite": True,
            "expected_sha256": content["file_sha256"],
        },
    )
    stale = client.post(
        "/api/ui/files/write",
        json={
            "path": "document.txt",
            "content": "stale\n",
            "overwrite": True,
            "expected_sha256": content["file_sha256"],
        },
    )
    made = client.post("/api/ui/files/mkdir", json={"path": "new-directory"})
    duplicate = client.post(
        "/api/ui/files/mkdir", json={"path": "new-directory"}
    )

    assert preview.status_code == 200
    data = preview.json()["data"]
    assert data["kind"] == "image"
    assert base64.b64decode(data["data_base64"]) == image_path.read_bytes()
    assert "rgba" not in data
    assert "cell_width" not in data
    assert saved.status_code == 200
    assert stale.status_code == 400
    assert "reload before saving" in stale.json()["message"]
    assert document.read_text(encoding="utf-8") == "second\n"
    assert made.status_code == 200
    assert made.json()["data"]["action"] == "mkdir"
    assert (workspace / "new-directory").is_dir()
    assert duplicate.status_code == 400
