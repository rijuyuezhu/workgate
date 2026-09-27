import gzip
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import workgate.audit.payloads as payloads
from workgate.audit.payloads import AUDIT_PAYLOAD_KEY
from workgate.config.role_config import (
    SharedRoleConfig,
    resolve_shared_role_config,
)
from workgate.config.settings import Settings
from workgate.persistence import StateLayout


def _settings(tmp_path: Path, **overrides: Any) -> SharedRoleConfig:
    values: dict[str, Any] = {
        "workspace_root": tmp_path,
        "state_dir": tmp_path / ".state",
        "agent_bridge_enabled": False,
        "audit_inline_value_bytes": 256,
        "max_audit_payload_bytes": 4_096,
        "max_audit_payload_store_bytes": 8_192,
        "audit_payload_retention_s": 60,
    }
    values.update(overrides)
    return resolve_shared_role_config(Settings(**values))


def _reference(
    *,
    digest: str = "a" * 64,
    json_bytes: object = 2,
    created_at: object = 1.0,
) -> dict[str, Any]:
    return {
        AUDIT_PAYLOAD_KEY: {
            "version": 1,
            "sha256": digest,
            "json_bytes": json_bytes,
            "created_at": created_at,
            "preview": "preview",
        }
    }


def test_payload_path_and_root_validation(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(ValueError, match="invalid audit payload digest"):
        payloads._payload_path(settings, "not-a-digest")

    StateLayout(settings.state_dir).audit_payload_dir.parent.mkdir(
        parents=True, exist_ok=True
    )
    StateLayout(settings.state_dir).audit_payload_dir.write_text(
        "not a directory", encoding="utf-8"
    )
    with pytest.raises(OSError):
        payloads._prepare_payload_root(settings)


def test_reference_metadata_rejects_malformed_shapes_and_recurses():
    malformed = [
        {AUDIT_PAYLOAD_KEY: "not-a-dict"},
        {AUDIT_PAYLOAD_KEY: {"version": 2, "sha256": "a" * 64}},
        {AUDIT_PAYLOAD_KEY: {"version": 1, "sha256": "bad"}},
        {AUDIT_PAYLOAD_KEY: {"version": 1, "sha256": "a" * 64}, "extra": 1},
    ]
    for value in malformed:
        assert payloads._reference_metadata(value) is None
        assert payloads.payload_reference_digests(value) == set()

    nested = {"rows": [_reference(digest="b" * 64), {"deep": _reference()}]}
    assert payloads.payload_reference_digests(nested) == {"a" * 64, "b" * 64}
    assert [
        item["sha256"] for item in payloads.payload_reference_metadata(nested)
    ] == [
        "b" * 64,
        "a" * 64,
    ]


def test_externalize_handles_compressed_object_limit_and_directory_collision(
    tmp_path, monkeypatch
):
    settings = _settings(
        tmp_path,
        max_audit_payload_bytes=1_024,
        max_audit_payload_store_bytes=1_024,
    )
    monkeypatch.setattr(
        payloads, "_deterministic_gzip", lambda _data: b"x" * 1_025
    )
    omitted = payloads.externalize_sanitized_value(
        {"body": "z" * 500},
        settings=settings,
        preview="preview",
        created_at=1.0,
    )
    assert omitted["$workgate_audit_truncated"]["reason"] == (
        "payload_store_object_limit"
    )

    monkeypatch.undo()
    value = {"body": "directory-collision-" * 25}
    encoded = payloads.canonical_json_bytes(value)
    digest = hashlib.sha256(encoded).hexdigest()
    payloads._prepare_payload_root(settings)
    payloads._payload_path(settings, digest).mkdir()
    failed = payloads.externalize_sanitized_value(
        value,
        settings=settings,
        preview="preview",
        created_at=1.0,
    )
    assert failed["$workgate_audit_truncated"]["reason"] == (
        "payload_store_error"
    )


def test_read_regular_file_rejects_symlink_nonregular_and_bounds(
    tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.write_bytes(b"target")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(ValueError, match="symlink"):
        payloads._read_regular_file_nofollow(link, 100)

    large = tmp_path / "large"
    large.write_bytes(b"abcdef")
    with pytest.raises(ValueError, match="exceeds read bound"):
        payloads._read_regular_file_nofollow(large, 3)

    if os.name != "nt":
        with pytest.raises(ValueError, match="not a regular file"):
            payloads._read_regular_file_nofollow(tmp_path, 100)

    real_fstat = payloads.os.fstat

    def fake_fstat(fd: int):
        info = real_fstat(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_size=1)

    monkeypatch.setattr(payloads.os, "fstat", fake_fstat)
    with pytest.raises(ValueError, match="exceeds read bound"):
        payloads._read_regular_file_nofollow(large, 3)


def test_resolve_rejects_invalid_reference_metadata(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    assert payloads.resolve_payload_reference("inline", settings) == "inline"

    bad_created = _reference(created_at=None)
    resolved = payloads.resolve_payload_reference(bad_created, settings)
    assert resolved[AUDIT_PAYLOAD_KEY]["status"] == "corrupt"

    monkeypatch.setattr(payloads.time, "time", lambda: 1.0)
    for value in (
        _reference(json_bytes=None),
        _reference(json_bytes="not-an-int"),
        _reference(json_bytes=10_000),
    ):
        resolved = payloads.resolve_payload_reference(value, settings)
        assert resolved[AUDIT_PAYLOAD_KEY]["status"] == "corrupt"


def test_resolve_detects_byte_count_digest_json_and_open_races(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    monkeypatch.setattr(payloads.time, "time", lambda: 1.0)
    payloads._prepare_payload_root(settings)

    decoded = b'{"body":"ok"}'
    digest = hashlib.sha256(decoded).hexdigest()
    path = payloads._payload_path(settings, digest)
    path.write_bytes(gzip.compress(decoded, mtime=0))

    mismatch = _reference(digest=digest, json_bytes=len(decoded) + 1)
    assert (
        payloads.resolve_payload_reference(mismatch, settings)[
            AUDIT_PAYLOAD_KEY
        ]["status"]
        == "corrupt"
    )

    other = b'{"body":"no"}'
    assert len(other) == len(decoded)
    path.write_bytes(gzip.compress(other, mtime=0))
    digest_mismatch = _reference(digest=digest, json_bytes=len(other))
    assert (
        payloads.resolve_payload_reference(digest_mismatch, settings)[
            AUDIT_PAYLOAD_KEY
        ]["status"]
        == "corrupt"
    )

    invalid_json = b"not-json-value"
    invalid_digest = hashlib.sha256(invalid_json).hexdigest()
    payloads._payload_path(settings, invalid_digest).write_bytes(
        gzip.compress(invalid_json, mtime=0)
    )
    invalid = _reference(digest=invalid_digest, json_bytes=len(invalid_json))
    assert (
        payloads.resolve_payload_reference(invalid, settings)[
            AUDIT_PAYLOAD_KEY
        ]["status"]
        == "corrupt"
    )

    invalid_utf8 = b'"\xff"'
    utf8_digest = hashlib.sha256(invalid_utf8).hexdigest()
    payloads._payload_path(settings, utf8_digest).write_bytes(
        gzip.compress(invalid_utf8, mtime=0)
    )
    utf8 = _reference(digest=utf8_digest, json_bytes=len(invalid_utf8))
    assert (
        payloads.resolve_payload_reference(utf8, settings)[AUDIT_PAYLOAD_KEY][
            "status"
        ]
        == "corrupt"
    )

    monkeypatch.setattr(
        payloads,
        "_read_regular_file_nofollow",
        lambda _path, _max: (_ for _ in ()).throw(FileNotFoundError()),
    )
    race = payloads.resolve_payload_reference(
        _reference(digest=utf8_digest, json_bytes=len(invalid_utf8)), settings
    )
    assert race[AUDIT_PAYLOAD_KEY]["status"] == "missing"


def test_resolve_detects_trailing_decompressed_data(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(payloads.time, "time", lambda: 1.0)
    decoded = b"{}"
    digest = hashlib.sha256(decoded).hexdigest()
    path = payloads._payload_path(settings, digest)
    payloads._prepare_payload_root(settings)
    path.write_bytes(b"placeholder")

    class FakeGzip:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, _size: int) -> bytes:
            self.calls += 1
            return decoded if self.calls == 1 else b"x"

    monkeypatch.setattr(payloads.gzip, "GzipFile", FakeGzip)
    result = payloads.resolve_payload_reference(
        _reference(digest=digest, json_bytes=len(decoded)), settings
    )
    assert result[AUDIT_PAYLOAD_KEY]["status"] == "corrupt"


def test_recursive_resolution_handles_lists(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(payloads.time, "time", lambda: 1.0)
    value = {"body": "list-value-" * 80}
    reference = payloads.externalize_sanitized_value(
        value, settings=settings, preview="preview", created_at=1.0
    )
    resolved = payloads.resolve_payload_references(
        {"items": [reference, "inline"]}, settings
    )
    assert resolved == {"items": [value, "inline"]}


def test_payload_file_sizes_and_prune_failure_branches(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path,
        max_audit_payload_bytes=1_024,
        max_audit_payload_store_bytes=1_024,
    )
    root = payloads._prepare_payload_root(settings)
    invalid = root / "not-a-digest.json.gz"
    invalid.write_bytes(b"x")
    symlink = root / f"{'a' * 64}.json.gz"
    target = tmp_path / "target-file"
    target.write_bytes(b"target")
    try:
        symlink.symlink_to(target)
    except OSError:
        symlink = None

    assert payloads.payload_file_sizes(settings) == {}

    original_lstat = Path.lstat
    troublesome = root / f"{'b' * 64}.json.gz"
    troublesome.write_bytes(b"x")

    def fake_lstat(path: Path):
        if path == troublesome:
            raise OSError("fixture")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    payloads.prune_payload_files(
        settings, referenced=set(), active_references=set()
    )
    assert troublesome.exists()
    assert not invalid.exists()
    if symlink is not None:
        assert not symlink.exists()


def test_prune_removes_inactive_references_and_enforces_quota(tmp_path):
    settings = _settings(
        tmp_path,
        max_audit_payload_bytes=1_024,
        max_audit_payload_store_bytes=1_024,
    )
    root = payloads._prepare_payload_root(settings)
    inactive_digest = "c" * 64
    inactive = root / f"{inactive_digest}.json.gz"
    inactive.write_bytes(b"x" * 100)
    payloads.prune_payload_files(
        settings,
        referenced={inactive_digest},
        active_references=set(),
    )
    assert not inactive.exists()

    first_digest = "d" * 64
    second_digest = "e" * 64
    first = root / f"{first_digest}.json.gz"
    second = root / f"{second_digest}.json.gz"
    first.write_bytes(b"a" * 700)
    second.write_bytes(b"b" * 700)
    os.utime(first, (1, 1))
    os.utime(second, (2, 2))
    payloads.prune_payload_files(
        settings,
        referenced={first_digest, second_digest},
        active_references={first_digest, second_digest},
    )
    assert sum(path.stat().st_size for path in root.glob("*.json.gz")) <= 1_024
    assert not first.exists()
    assert second.exists()
