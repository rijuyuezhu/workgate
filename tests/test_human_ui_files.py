import base64
import errno
import os

import pytest
from fastapi.testclient import TestClient

import workgate.ui.http.files as ui_files_module
from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
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
    allow_full_control=False,
    **values,
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(workspace / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", auth_mode)
    monkeypatch.setenv("WORKGATE_BASE_URL", BASE_URL)
    monkeypatch.setenv(
        "WORKGATE_ALLOW_FULL_CONTROL",
        str(allow_full_control).lower(),
    )
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "false")
    for name, value in values.items():
        monkeypatch.setenv(f"WORKGATE_{name.upper()}", str(value).lower())
    clear_settings_cache()


def _client(monkeypatch, workspace, **values) -> TestClient:
    _configure(monkeypatch, workspace, **values)
    return TestClient(
        build_http_app(),
        base_url=BASE_URL,
        client=("203.0.113.11", 50001),
    )


def _token(scope: str) -> str:
    return issue_access_token(
        client_id="webui-files-test",
        scope=scope,
        resource=f"{BASE_URL}/mcp",
    )


def test_file_listing_is_sorted_bounded_and_workspace_relative(
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
    assert payload["machine"] == "local"
    assert payload["remote"] is False
    assert payload["path"] == "."
    assert payload["parent"] == "."
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
    assert not (workspace / ".state").exists()
    hidden = next(
        entry for entry in payload["entries"] if entry["name"] == ".hidden"
    )
    assert hidden["hidden"] is True
    assert all(not os.path.isabs(entry["path"]) for entry in payload["entries"])


def test_file_api_stays_inside_workspace_even_in_full_control_mode(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)
    client = _client(
        monkeypatch,
        workspace,
        allow_full_control=True,
    )

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
        assert response.status_code == 400
        assert "escapes workspace" in response.json()["message"].lower()
    assert not (outside / "new.txt").exists()
    assert not (outside / "linked.txt").exists()


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
    complete_client = TestClient(build_http_app(), base_url=BASE_URL)
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


def test_delete_refuses_workspace_root_and_unlinks_symlink_not_target(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = workspace / "outside-link"
    link.symlink_to(outside)
    client = _client(monkeypatch, workspace)

    root = client.post(
        "/api/ui/files/delete", json={"path": ".", "recursive": True}
    )
    deleted = client.post(
        "/api/ui/files/delete",
        json={"path": "outside-link", "recursive": False},
    )

    assert root.status_code == 400
    assert "workspace root" in root.json()["message"]
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] == "link"
    assert not link.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


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
        "machine": "local",
        "remote": False,
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

    monkeypatch.setattr(ui_files_module.os, "rename", cross_device)
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
    assert "workspace root" in root.json()["message"]
    assert (workspace / "existing.txt").read_text(
        encoding="utf-8"
    ) == "existing"


@pytest.mark.parametrize("action", ["copy", "move"])
def test_copy_and_move_reject_destination_symlink_escape(
    monkeypatch, tmp_path, action
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "source.txt").write_text("source", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)
    client = _client(monkeypatch, workspace, allow_full_control=True)

    response = client.post(
        f"/api/ui/files/{action}",
        json={
            "path": "source.txt",
            "destination": "outside-link/escaped.txt",
        },
    )

    assert response.status_code == 400
    assert "escapes workspace" in response.json()["message"].lower()
    assert not (outside / "escaped.txt").exists()
    assert (workspace / "source.txt").read_text(encoding="utf-8") == "source"


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


def test_file_machine_arg_normalizes_blank_and_rejects_oversized() -> None:
    assert ui_files_module._machine_arg("   ") == "local"
    with pytest.raises(ValueError, match="machine exceeds 255 encoded bytes"):
        ui_files_module._machine_arg("x" * 256)


def test_opentui_image_preview_editor_revision_and_mkdir(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_path = workspace / "pixel.png"
    image_path.write_bytes(VALID_PNG_1X1)
    document = workspace / "document.txt"
    document.write_text("first\n", encoding="utf-8")
    client = _client(monkeypatch, workspace)

    preview = client.get(
        "/api/ui/files/preview",
        params={
            "path": "pixel.png",
            "columns": 10,
            "rows": 5,
            "cell_aspect": 2,
        },
    )
    invalid = client.get(
        "/api/ui/files/preview",
        params={"path": "pixel.png", "columns": 1, "rows": 5},
    )
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
    rgba = base64.b64decode(data["rgba"])
    assert data["width"] >= 1
    assert data["height"] >= 1
    assert len(rgba) == data["width"] * data["height"] * 4
    assert data["cell_width"] >= 1
    assert data["cell_height"] >= 1
    assert invalid.status_code == 400
    assert saved.status_code == 200
    assert stale.status_code == 400
    assert "reload before saving" in stale.json()["message"]
    assert document.read_text(encoding="utf-8") == "second\n"
    assert made.status_code == 200
    assert made.json()["data"]["action"] == "mkdir"
    assert (workspace / "new-directory").is_dir()
    assert duplicate.status_code == 400
