import workgate.utils.private_files as private_files


def test_private_file_lock_initializes_and_repairs_empty_file(tmp_path):
    path = tmp_path / "lock"
    path.touch()

    with private_files.private_file_lock(path):
        assert path.stat().st_size >= 1


def test_lock_file_initialization_retries_permission_error_from_write(
    monkeypatch, tmp_path
):
    path = tmp_path / "lock"
    real_write = private_files.os.write
    attempts = 0
    sleeps: list[float] = []

    def flaky_write(descriptor: int, data: bytes) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("fixture write race")
        return real_write(descriptor, data)

    monkeypatch.setattr(private_files.os, "write", flaky_write)
    monkeypatch.setattr(private_files.time, "sleep", sleeps.append)

    private_files._ensure_lock_file(path)

    assert path.read_bytes() == b"\0"
    assert attempts == 2
    assert sleeps == [0.01]


def test_private_file_lock_retries_open_sharing_race(monkeypatch, tmp_path):
    path = tmp_path / "lock"
    private_files._ensure_lock_file(path)
    real_open = type(path).open
    attempts = 0
    sleeps: list[float] = []

    def flaky_open(self, *args, **kwargs):  # noqa: ANN001
        nonlocal attempts
        if self == path and args == ("r+b",):
            attempts += 1
            if attempts == 1:
                raise PermissionError("fixture open race")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", flaky_open)
    monkeypatch.setattr(private_files.time, "sleep", sleeps.append)

    with private_files.private_file_lock(path):
        pass

    assert attempts == 2
    assert sleeps == [0.01]


def test_lock_file_initialization_retries_empty_file_permission_race(
    monkeypatch, tmp_path
):
    path = tmp_path / "lock"
    path.touch()
    real_open = private_files.os.open
    append_attempts = 0
    sleeps: list[float] = []

    def fake_open(path_arg, flags, mode=0o777):
        nonlocal append_attempts
        if flags & private_files.os.O_APPEND:
            append_attempts += 1
            if append_attempts == 1:
                raise PermissionError("fixture race")
        return real_open(path_arg, flags, mode)

    monkeypatch.setattr(private_files.os, "open", fake_open)
    monkeypatch.setattr(private_files.time, "sleep", sleeps.append)

    private_files._ensure_lock_file(path)

    assert path.stat().st_size >= 1
    assert sleeps == [0.01]
