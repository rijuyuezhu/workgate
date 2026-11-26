"""Private, restart-safe final upstream Fernet credential envelope."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cryptography.fernet import Fernet

from workgate.hosted_chat.credential_cipher import (
    CredentialCipher,
    CredentialUnavailable,
)
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.utils.private_files import write_private_bytes


def test_restart_and_binding(tmp_path):
    db = SQLiteDatabase(tmp_path)
    cipher = CredentialCipher(db)
    assert not cipher.path.exists()
    with pytest.raises(CredentialUnavailable):
        cipher.seal({"key": "private-key"}, binding="a")
    assert not cipher.path.exists()
    cipher.initialize()
    token = cipher.seal({"key": "private-key"}, binding="a")
    assert "private-key" not in token
    reopened = CredentialCipher(SQLiteDatabase(tmp_path))
    reopened.initialize()
    assert reopened.unseal(token, binding="a") == {"key": "private-key"}
    for invalid, binding in ((token, "b"), ("corrupt", "a")):
        with pytest.raises(CredentialUnavailable):
            reopened.unseal(invalid, binding=binding)
    original_key = cipher.path.read_bytes()
    if os.name != "nt":
        assert cipher.path.stat().st_mode & 0o777 == 0o600
    cipher.path.unlink()
    with pytest.raises(CredentialUnavailable):
        reopened.unseal(token, binding="a")
    assert not cipher.path.exists()
    with pytest.raises(CredentialUnavailable):
        reopened.initialize()
    assert not cipher.path.exists()
    write_private_bytes(cipher.path, original_key, exclusive=True)
    reopened.initialize()
    assert reopened.unseal(token, binding="a") == {"key": "private-key"}


def test_supplied_key_never_written(tmp_path):
    db = SQLiteDatabase(tmp_path)
    key = Fernet.generate_key()
    cipher = CredentialCipher(db, supplied_key=key)
    cipher.initialize()
    token = cipher.seal({"refresh": "secret"}, binding="owner/channel")
    assert not cipher.path.exists()
    assert CredentialCipher(db, supplied_key=key).unseal(
        token, binding="owner/channel"
    ) == {"refresh": "secret"}
    for wrong in (Fernet.generate_key(), b"bad-key"):
        with pytest.raises(CredentialUnavailable):
            CredentialCipher(db, supplied_key=wrong).unseal(
                token, binding="owner/channel"
            )
        with pytest.raises(CredentialUnavailable):
            CredentialCipher(db, supplied_key=wrong).initialize()
        with pytest.raises(CredentialUnavailable):
            CredentialCipher(db, supplied_key=wrong).seal(
                {"key": "new"}, binding="owner/channel"
            )


def test_parallel_key_initialization(tmp_path):
    db = SQLiteDatabase(tmp_path)
    barrier = Barrier(4)

    def initialize(index):
        cipher = CredentialCipher(db)
        barrier.wait(timeout=5)
        cipher.initialize()
        return cipher.seal({"index": index}, binding="a")

    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(initialize, range(4)))
    cipher = CredentialCipher(db)
    assert [
        cipher.unseal(token, binding="a")["index"] for token in tokens
    ] == list(range(4))


def test_corrupt_key_not_replaced(tmp_path):
    cipher = CredentialCipher(SQLiteDatabase(tmp_path))
    cipher.initialize()
    cipher.path.write_bytes(b"bad")
    with pytest.raises(CredentialUnavailable):
        cipher.initialize()
    assert cipher.path.read_bytes() == b"bad"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and symlinks")
def test_unsafe_key_and_lock_refused(tmp_path):
    cipher = CredentialCipher(SQLiteDatabase(tmp_path))
    target = tmp_path / "unrelated"
    target.write_bytes(b"untouched")
    for path in (cipher.path, cipher.lock):
        path.unlink(missing_ok=True)
        path.symlink_to(target)
        with pytest.raises(CredentialUnavailable):
            cipher.initialize()
        path.unlink()
    cipher.initialize()
    cipher.path.chmod(0o644)
    with pytest.raises(CredentialUnavailable):
        cipher.initialize()
    assert target.read_bytes() == b"untouched"
