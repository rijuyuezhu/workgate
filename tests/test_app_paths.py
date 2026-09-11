import os
from pathlib import Path

import pytest

from workgate.app_paths import ensure_private_directory, resolve_app_paths


def test_linux_paths_follow_absolute_xdg_roots(tmp_path: Path) -> None:
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }

    paths = resolve_app_paths(
        env=env,
        home=tmp_path / "home",
        platform="linux",
        temp_root=tmp_path / "tmp",
    )

    assert paths.config_dir == tmp_path / "config" / "workgate"
    assert paths.state_dir == tmp_path / "state" / "workgate"
    assert paths.data_dir == tmp_path / "data" / "workgate"
    assert paths.cache_dir == tmp_path / "cache" / "workgate"


def test_linux_relative_xdg_roots_fall_back(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = resolve_app_paths(
        env={
            "XDG_CONFIG_HOME": "relative-config",
            "XDG_STATE_HOME": "relative-state",
            "XDG_DATA_HOME": "relative-data",
            "XDG_CACHE_HOME": "relative-cache",
            "XDG_RUNTIME_DIR": "relative-runtime",
        },
        home=home,
        platform="linux",
        temp_root=tmp_path / "tmp",
    )

    assert paths.config_dir == home / ".config" / "workgate"
    assert paths.state_dir == home / ".local" / "state" / "workgate"
    assert paths.data_dir == home / ".local" / "share" / "workgate"
    assert paths.cache_dir == home / ".cache" / "workgate"
    assert paths.runtime_dir.parent == tmp_path / "tmp"
    assert paths.runtime_dir.name.startswith("workgate-")


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX XDG runtime ownership/mode contract"
)
def test_linux_uses_suitable_runtime_dir(tmp_path: Path) -> None:
    runtime = tmp_path / "xdg-runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    paths = resolve_app_paths(
        env={"XDG_RUNTIME_DIR": str(runtime)},
        home=tmp_path / "home",
        platform="linux",
        temp_root=tmp_path / "tmp",
    )

    assert paths.runtime_dir == runtime / "workgate"
    assert paths.temp_dir == runtime / "workgate" / "tmp"


def test_linux_rejects_expanded_and_insecure_xdg_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "xdg-runtime"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)

    paths = resolve_app_paths(
        env={
            "HOME": str(home),
            "XDG_CONFIG_HOME": "~/.config-alt",
            "XDG_STATE_HOME": "$HOME/state-alt",
            "XDG_RUNTIME_DIR": str(runtime),
        },
        home=home,
        platform="linux",
        temp_root=tmp_path / "tmp",
    )

    assert paths.config_dir == home / ".config" / "workgate"
    assert paths.state_dir == home / ".local" / "state" / "workgate"
    assert paths.runtime_dir.parent == tmp_path / "tmp"
    assert paths.runtime_dir.name.startswith("workgate-")


def test_macos_lifetime_namespaces_do_not_overlap(tmp_path: Path) -> None:
    paths = resolve_app_paths(
        env={},
        home=tmp_path / "home",
        platform="darwin",
        temp_root=tmp_path / "tmp",
    )

    assert (
        paths.config_dir.parent
        == paths.state_dir.parent
        == paths.data_dir.parent
    )
    assert len({paths.config_dir, paths.state_dir, paths.data_dir}) == 3
    assert paths.config_dir.name == "config"
    assert paths.state_dir.name == "state"
    assert paths.data_dir.name == "data"


def test_private_directory_tolerates_racing_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "private"
    original_mkdir = Path.mkdir
    raced = False

    def racing_mkdir(path: Path, *args, **kwargs) -> None:
        nonlocal raced
        if path == private and not raced:
            raced = True
            original_mkdir(path, parents=True, exist_ok=False, mode=0o700)
            raise FileExistsError(path)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    assert ensure_private_directory(private) == private
    assert private.is_dir()


def test_windows_uses_native_roaming_and_local_namespaces(
    tmp_path: Path,
) -> None:
    roaming = tmp_path / "roaming"
    local = tmp_path / "local"
    paths = resolve_app_paths(
        env={"APPDATA": str(roaming), "LOCALAPPDATA": str(local)},
        home=tmp_path / "home",
        platform="win32",
        temp_root=tmp_path / "tmp",
    )

    assert paths.config_dir == roaming / "workgate" / "config"
    assert paths.state_dir == local / "workgate" / "state"
    assert paths.data_dir == local / "workgate" / "data"
    assert paths.cache_dir == local / "workgate" / "cache"
    assert paths.runtime_dir.parent == tmp_path / "tmp" / "workgate"
    assert paths.runtime_dir.name.startswith("runtime-")
    assert len({paths.state_dir, paths.data_dir, paths.cache_dir}) == 3
