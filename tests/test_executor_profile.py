from pathlib import Path

import pytest
from pydantic import ValidationError

from workgate.executor.profile import (
    ExecutorAlreadyRunningError,
    ExecutorProfile,
    ExecutorProfileStore,
    executor_run_lock,
)
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.ids import new_executor_id


def _store(tmp_path: Path) -> FileStateStore:
    return FileStateStore(lambda: tmp_path / "state")


def _profile() -> ExecutorProfile:
    return ExecutorProfile(
        control_url="https://control.example/",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )


def test_executor_profile_roundtrips_private_bearer_without_repr_leak(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    profiles = ExecutorProfileStore(store)
    profile = _profile()

    profiles.save(profile)
    restored = profiles.load()

    assert restored == profile
    assert restored is not None
    assert restored.control_url == "https://control.example"
    assert profile.credential in store.layout.executor_profile_path.read_text(
        encoding="utf-8"
    )
    assert profile.credential not in repr(profile)


def test_executor_profile_reader_bound_covers_writer_state_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    profiles = ExecutorProfileStore(store)
    profiles.save(_profile())
    limits: list[int | None] = []
    original = store.read_json

    def observe(path: Path, *, max_bytes: int | None = None):
        limits.append(max_bytes)
        return original(path, max_bytes=max_bytes)

    monkeypatch.setattr(store, "read_json", observe)

    assert profiles.load() is not None
    assert limits == [16 * 1024]
    assert store.layout.executor_profile_path.stat().st_size < 16 * 1024


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/control",
        "https://user:pass@control.example",
        "https://control.example/?token=secret",
        "https://control.example/#fragment",
        "https://control.example/base/path",
        "http://control.example",
        "http://192.168.1.10:8765",
    ],
)
def test_executor_profile_rejects_unsafe_control_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        ExecutorProfile(
            control_url=url,
            executor_id=new_executor_id(),
            credential=new_executor_credential(),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8765",
        "http://localhost.:8765",
        "http://127.0.0.1:8765",
        "http://127.0.0.2:8765",
        "http://[::1]:8765",
        "https://control.example",
    ],
)
def test_executor_profile_accepts_https_or_loopback_http(url: str) -> None:
    profile = ExecutorProfile(
        control_url=url,
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )

    assert profile.control_url == url


def test_executor_profile_lock_rejects_duplicate_process_loop(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    with (
        executor_run_lock(store),
        pytest.raises(ExecutorAlreadyRunningError),
        executor_run_lock(store),
    ):
        raise AssertionError("duplicate executor lock unexpectedly acquired")
