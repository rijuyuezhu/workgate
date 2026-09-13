import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control import download_store
from workgate.control import downloads as downloads_module
from workgate.control.download_snapshot import (
    DownloadSnapshot,
    new_staging_path,
    open_private_staging,
    snapshot_directory,
)
from workgate.control.download_store import backup_path, store_path
from workgate.control.downloads import (
    _list_file_links_owned,
    _register_snapshot,
)
from workgate.control.payload_store import PayloadStore


def _configure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_DATA_DIR", str(tmp_path / ".data"))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://files.example.test")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()


def _payloads() -> PayloadStore:
    return PayloadStore(get_settings().data_dir)


def _create_file_link(path: str):
    source = Path(path)
    if not source.is_absolute():
        source = get_settings().workspace_root / source
    data = source.read_bytes()
    staging = new_staging_path()
    with open_private_staging(staging) as handle:
        handle.write(data)
    snapshot = DownloadSnapshot(
        staging_path=staging,
        display_path=str(source),
        source_name=source.name,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    return _register_snapshot(
        snapshot,
        ttl_s=60,
        filename=None,
        max_downloads=None,
        inline=False,
        session_id=None,
    )


def test_concurrent_processes_do_not_lose_links(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    sources = []
    for index in range(6):
        source = tmp_path / f"artifact-{index}.txt"
        source.write_text(f"payload-{index}", encoding="utf-8")
        sources.append(source.name)

    script = """
import hashlib
import sys
from pathlib import Path
from workgate.config.settings import clear_settings_cache
from workgate.control.download_snapshot import DownloadSnapshot, new_staging_path, open_private_staging
from workgate.control.downloads import _register_snapshot
clear_settings_cache()
source = Path(sys.argv[1])
data = source.read_bytes()
staging = new_staging_path()
with open_private_staging(staging) as handle:
    handle.write(data)
_register_snapshot(
    DownloadSnapshot(
        staging_path=staging,
        display_path=str(source),
        source_name=source.name,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    ),
    ttl_s=60, filename=None, max_downloads=None, inline=False, session_id=None,
)
"""
    environment = os.environ.copy()
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, source],
            cwd=tmp_path,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for source in sources
    ]
    failures = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        if process.returncode != 0:
            failures.append((process.returncode, stdout, stderr))

    assert failures == []
    clear_settings_cache()
    links = _list_file_links_owned().links
    assert len(links) == len(sources)
    assert {Path(link.path or "").name for link in links} == set(sources)
    assert len(list(snapshot_directory().glob("*.bin"))) == len(sources)


def test_corrupt_primary_and_backup_refuse_silent_reset(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    (tmp_path / "artifact.txt").write_text("payload", encoding="utf-8")
    _create_file_link("artifact.txt")
    store_path().write_text("{broken-primary", encoding="utf-8")
    backup_path().write_text("{broken-backup", encoding="utf-8")

    with pytest.raises(RuntimeError, match="no valid backup"):
        _list_file_links_owned()

    assert store_path().read_text(encoding="utf-8") == "{broken-primary"
    assert backup_path().read_text(encoding="utf-8") == "{broken-backup"
    assert len(list(snapshot_directory().glob("*.bin"))) == 1


def test_failed_primary_recovery_preserves_valid_backup(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    source = tmp_path / "artifact.txt"
    source.write_text("payload", encoding="utf-8")
    created = _create_file_link(str(source))
    backup_before = backup_path().read_bytes()
    store_path().write_text("{broken-primary", encoding="utf-8")

    real_atomic_write = download_store.atomic_write_private_text

    def fail_primary_write(path: Path, content: str) -> None:
        if path == store_path():
            raise OSError("simulated primary restore failure")
        real_atomic_write(path, content)

    monkeypatch.setattr(
        download_store, "atomic_write_private_text", fail_primary_write
    )

    with pytest.raises(OSError, match="simulated primary restore failure"):
        _list_file_links_owned()

    assert backup_path().read_bytes() == backup_before
    assert created.link_id in json.loads(backup_before)["links"]

    monkeypatch.setattr(
        download_store, "atomic_write_private_text", real_atomic_write
    )
    recovered = _list_file_links_owned().links
    assert [link.link_id for link in recovered] == [created.link_id]


def test_legacy_live_path_links_are_dropped_on_migration(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    state_dir = get_settings().state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "version": 1,
        "links": {
            "legacy-token": {
                "path": str(tmp_path / "live-secret.txt"),
                "display_path": "live-secret.txt",
                "filename": "live-secret.txt",
                "expires_at": 9_999_999_999,
                "downloads": 0,
                "max_downloads": 0,
            }
        },
    }
    store_path().write_text(json.dumps(legacy), encoding="utf-8")

    assert _list_file_links_owned().links == []
    assert json.loads(store_path().read_text(encoding="utf-8")) == {
        "links": {},
        "version": 3,
    }
    assert json.loads(backup_path().read_text(encoding="utf-8")) == {
        "links": {},
        "version": 3,
    }


def test_v2_snapshot_links_are_invalidated_and_scrubbed(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    state_dir = get_settings().state_dir
    legacy_dir = state_dir / "downloads"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_snapshot = legacy_dir / "legacy.bin"
    payload = b"legacy payload"
    legacy_snapshot.write_bytes(payload)
    token = "legacy-sensitive-bearer"
    legacy = {
        "version": 2,
        "links": {
            token: {
                "snapshot_name": legacy_snapshot.name,
                "display_path": "legacy.txt",
                "filename": "legacy.txt",
                "expires_at": 9_999_999_999.0,
                "downloads": 0,
                "max_downloads": 0,
            }
        },
    }
    store_path().parent.mkdir(parents=True, exist_ok=True)
    store_path().write_text(json.dumps(legacy), encoding="utf-8")
    backup_path().write_text(json.dumps(legacy), encoding="utf-8")

    assert _list_file_links_owned().links == []
    persisted = store_path().read_text(encoding="utf-8")
    persisted_backup = backup_path().read_text(encoding="utf-8")
    assert token not in persisted
    assert token not in persisted_backup
    assert json.loads(persisted) == {"links": {}, "version": 3}
    assert json.loads(persisted_backup) == {"links": {}, "version": 3}
    assert not legacy_snapshot.exists()


def test_v3_primary_scrubs_stale_legacy_backup(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    source = tmp_path / "artifact.txt"
    source.write_text("payload", encoding="utf-8")
    _create_file_link(str(source))

    stale_token = "stale-plaintext-bearer"
    backup_path().write_text(
        json.dumps({"version": 2, "links": {stale_token: {}}}),
        encoding="utf-8",
    )

    links = _list_file_links_owned().links

    assert len(links) == 1
    refreshed = backup_path().read_text(encoding="utf-8")
    assert stale_token not in refreshed
    assert json.loads(refreshed)["version"] == 3


def test_revoke_persists_metadata_before_payload_cleanup(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    source = tmp_path / "artifact.txt"
    source.write_text("payload", encoding="utf-8")
    created = _create_file_link(str(source))
    payload_files = list(snapshot_directory().glob("*.bin"))
    assert len(payload_files) == 1

    monkeypatch.setattr(
        downloads_module,
        "save_locked",
        lambda _store: (_ for _ in ()).throw(
            OSError("simulated state failure")
        ),
    )

    with pytest.raises(OSError, match="simulated state failure"):
        downloads_module._revoke_file_link_owned(created.link_id)

    assert payload_files[0].read_bytes() == b"payload"
    persisted = json.loads(store_path().read_text(encoding="utf-8"))
    assert created.link_id in persisted["links"]


def test_legacy_bearer_backup_scrub_failure_fails_closed(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    source = tmp_path / "artifact.txt"
    source.write_text("payload", encoding="utf-8")
    _create_file_link(str(source))

    stale_token = "stale-plaintext-bearer"
    backup_path().write_text(
        json.dumps({"version": 2, "links": {stale_token: {}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        download_store,
        "_remove_stale_backup",
        lambda: (_ for _ in ()).throw(
            OSError("simulated backup cleanup failure")
        ),
    )

    with pytest.raises(RuntimeError, match="backup could not be invalidated"):
        _list_file_links_owned()

    assert stale_token not in store_path().read_text(encoding="utf-8")
    assert stale_token in backup_path().read_text(encoding="utf-8")


def test_failed_backup_refresh_cannot_resurrect_revoked_link(
    tmp_path, monkeypatch
):
    _configure(tmp_path, monkeypatch)
    source = tmp_path / "artifact.txt"
    source.write_text("payload", encoding="utf-8")
    created = _create_file_link(str(source))
    assert (
        created.link_id
        in json.loads(backup_path().read_text(encoding="utf-8"))["links"]
    )

    real_atomic_write = download_store.atomic_write_private_text

    def fail_backup_write(path: Path, content: str) -> None:
        if path == backup_path():
            raise OSError("simulated backup write failure")
        real_atomic_write(path, content)

    monkeypatch.setattr(
        download_store, "atomic_write_private_text", fail_backup_write
    )

    revoked = downloads_module._revoke_file_link_owned(created.link_id)

    assert revoked.revoked is True
    assert not backup_path().exists()
    persisted = json.loads(store_path().read_text(encoding="utf-8"))
    assert created.link_id not in persisted["links"]

    store_path().write_text("{broken-primary", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no valid backup"):
        _list_file_links_owned()


def test_prune_removes_only_stale_staging_files(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    directory = snapshot_directory()
    payloads = _payloads()
    stale = directory / ".stale.tmp"
    recent = directory / ".recent.tmp"
    other_feature = payloads.directory("transfer") / ".stale.tmp"
    stale.write_bytes(b"stale")
    recent.write_bytes(b"recent")
    other_feature.write_bytes(b"transfer")
    old = 1_700_000_000
    os.utime(stale, (old, old))
    os.utime(other_feature, (old, old))

    _list_file_links_owned()

    assert not stale.exists()
    assert recent.read_bytes() == b"recent"
    assert other_feature.read_bytes() == b"transfer"


def test_prune_removes_download_orphans_without_touching_transfer_payloads(
    tmp_path, monkeypatch
):
    _configure(tmp_path, monkeypatch)
    payloads = _payloads()

    download_staging = payloads.new_staging_path("download")
    with payloads.open_private_staging(
        download_staging, namespace="download"
    ) as handle:
        handle.write(b"download orphan")
    download = payloads.commit_staging(
        download_staging,
        namespace="download",
        size=len(b"download orphan"),
        sha256=hashlib.sha256(b"download orphan").hexdigest(),
    )

    transfer_staging = payloads.new_staging_path("transfer")
    with payloads.open_private_staging(
        transfer_staging, namespace="transfer"
    ) as handle:
        handle.write(b"transfer payload")
    transfer = payloads.commit_staging(
        transfer_staging,
        namespace="transfer",
        size=len(b"transfer payload"),
        sha256=hashlib.sha256(b"transfer payload").hexdigest(),
    )

    _list_file_links_owned()

    assert not payloads.path(download.payload_id, namespace="download").exists()
    assert payloads.path(
        transfer.payload_id, namespace="transfer"
    ).read_bytes() == (b"transfer payload")
