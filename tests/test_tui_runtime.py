import gzip
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import workgate.ui.runtime as runtime
from workgate.config.control import ControlConfig, resolve_control_config
from workgate.config.settings import Settings


def _settings(tmp_path: Path, **overrides: object) -> ControlConfig:
    return resolve_control_config(
        Settings.model_validate(
            {
                "workspace_root": tmp_path / "workspace",
                "state_dir": tmp_path / "state",
                "auth_mode": "none",
                **overrides,
            }
        )
    )


def test_runtime_roots_match_package_and_source_tree() -> None:
    package_root = Path(runtime.__file__).resolve().parents[1]

    assert package_root == runtime._PACKAGE_ROOT
    assert package_root.parents[1] == runtime._SOURCE_TREE_ROOT


def test_runtime_discovery_uses_explicit_package_and_source_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "src" / "workgate"
    source_root = tmp_path
    payload = package_root / "ui_runtime" / f"{runtime.TUI_EXECUTABLE_NAME}.gz"
    source = source_root / "ui-opentui" / "src" / "tui.tsx"
    sidecar = source_root / "ui-opentui" / "dist" / runtime.TUI_EXECUTABLE_NAME
    payload.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    payload.write_bytes(b"payload")
    source.write_text("export {}", encoding="utf-8")
    monkeypatch.setattr(runtime, "_PACKAGE_ROOT", package_root)
    monkeypatch.setattr(runtime, "_SOURCE_TREE_ROOT", source_root)
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)

    assert runtime.embedded_tui_payload() == payload
    assert runtime.tui_source_path() == source
    assert sidecar in runtime._tui_sidecar_candidates()


def test_same_regular_file_content_rejects_unsafe_or_mismatched_inputs(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    right.write_bytes(b"a")

    assert runtime._same_regular_file_content(left, right) is False

    left.mkdir()
    assert runtime._same_regular_file_content(left, right) is False

    left.rmdir()
    left.write_bytes(b"bb")
    assert runtime._same_regular_file_content(left, right) is False


def test_same_regular_file_content_compares_bytes(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.write_bytes(b"same")
    right.write_bytes(b"same")

    assert runtime._same_regular_file_content(left, right) is True

    right.write_bytes(b"diff")
    assert runtime._same_regular_file_content(left, right) is False


def test_materialize_embedded_tui_is_atomic_executable_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / f"{runtime.TUI_EXECUTABLE_NAME}.gz"
    expected = b"#!/bin/sh\necho tui\n"
    with gzip.open(payload, "wb") as handle:
        handle.write(expected)
    Path(f"{payload}.sha256").write_bytes(
        (hashlib.sha256(expected).hexdigest() + "\n").encode("ascii")
    )

    first = runtime.materialize_embedded_tui(
        tmp_path / "state", payload=payload
    )

    def extraction_should_not_run(_source: Path, _destination: Path) -> None:
        raise AssertionError(
            "validated TUI cache should use the digest fast path"
        )

    monkeypatch.setattr(
        runtime, "_copy_bounded_gzip", extraction_should_not_run
    )
    second = runtime.materialize_embedded_tui(
        tmp_path / "state", payload=payload
    )

    assert first == second
    assert first is not None
    assert first.read_bytes() == expected
    if os.name != "nt":
        assert first.stat().st_mode & 0o100
    assert not list(first.parent.glob("*.tmp"))


def test_materialize_embedded_tui_rejects_empty_payload(tmp_path: Path) -> None:
    payload = tmp_path / f"{runtime.TUI_EXECUTABLE_NAME}.gz"
    with gzip.open(payload, "wb"):
        pass

    with pytest.raises(ValueError, match="empty"):
        runtime.materialize_embedded_tui(tmp_path / "state", payload=payload)


def test_materialize_embedded_tui_replaces_tampered_cache(
    tmp_path: Path,
) -> None:
    payload = tmp_path / f"{runtime.TUI_EXECUTABLE_NAME}.gz"
    expected = b"#!/bin/sh\necho trusted\n"
    with gzip.open(payload, "wb") as handle:
        handle.write(expected)
    Path(f"{payload}.sha256").write_bytes(
        (hashlib.sha256(expected).hexdigest() + "\n").encode("ascii")
    )

    cache = tmp_path / "cache" / "ui-runtime"
    target_dir = cache / runtime.__version__
    target_dir.mkdir(parents=True)
    target = target_dir / runtime.TUI_EXECUTABLE_NAME
    target.write_bytes(b"#!/bin/sh\necho attacker\n")
    if os.name != "nt":
        target.chmod(0o700)

    resolved = runtime.materialize_embedded_tui(cache, payload=payload)

    assert resolved == target
    assert target.read_bytes() == expected


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires privileges on Windows"
)
def test_materialize_embedded_tui_replaces_symlink_cache_target(
    tmp_path: Path,
) -> None:
    payload = tmp_path / f"{runtime.TUI_EXECUTABLE_NAME}.gz"
    expected = b"#!/bin/sh\necho trusted\n"
    with gzip.open(payload, "wb") as handle:
        handle.write(expected)

    cache = tmp_path / "cache" / "ui-runtime"
    target_dir = cache / runtime.__version__
    target_dir.mkdir(parents=True)
    attacker = tmp_path / "attacker"
    attacker.write_bytes(b"attacker")
    target = target_dir / runtime.TUI_EXECUTABLE_NAME
    target.symlink_to(attacker)

    resolved = runtime.materialize_embedded_tui(cache, payload=payload)

    assert resolved == target
    assert not target.is_symlink()
    assert target.read_bytes() == expected
    assert attacker.read_bytes() == b"attacker"


def test_split_tui_command_preserves_windows_quoted_path() -> None:
    assert runtime.split_tui_command(
        '"C:\\Program Files\\Workgate\\workgate-tui.exe" --flag',
        windows=True,
    ) == ["C:\\Program Files\\Workgate\\workgate-tui.exe", "--flag"]
    with pytest.raises(ValueError, match="empty"):
        runtime.split_tui_command("   ")


def test_resolve_tui_command_prefers_configured_command(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        ui_tui_command='"/opt/Workgate TUI" --compact',
    )

    assert runtime.resolve_tui_command(settings) == [
        "/opt/Workgate TUI",
        "--compact",
    ]


def test_resolve_tui_command_finds_sidecar_on_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / runtime.TUI_EXECUTABLE_NAME
    sidecar.write_bytes(b"sidecar")
    monkeypatch.setattr(
        runtime.sys, "executable", str(tmp_path / "missing-python")
    )
    monkeypatch.setattr(runtime.sys, "argv", [str(tmp_path / "missing-main")])
    monkeypatch.setattr(runtime, "tui_source_path", lambda: None)
    monkeypatch.setattr(runtime, "embedded_tui_payload", lambda: None)
    monkeypatch.setattr(
        runtime.shutil,
        "which",
        lambda name: (
            str(sidecar) if name == runtime.TUI_EXECUTABLE_NAME else None
        ),
    )

    assert runtime.resolve_tui_command(_settings(tmp_path)) == [str(sidecar)]
    assert runtime.tui_runtime_available(_settings(tmp_path)) is True


def test_resolve_tui_command_uses_bun_source_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "tui.tsx"
    source.write_text("export {}", encoding="utf-8")

    monkeypatch.setattr(runtime, "tui_source_path", lambda: source)
    monkeypatch.setattr(runtime, "_tui_sidecar_candidates", lambda: ())
    monkeypatch.setattr(
        runtime, "materialize_embedded_tui", lambda _state: None
    )
    monkeypatch.setattr(
        runtime.shutil,
        "which",
        lambda name: "/usr/bin/bun" if name == "bun" else None,
    )
    monkeypatch.setattr(runtime.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(runtime.sys, "argv", [str(tmp_path / "workgate")])

    assert runtime.resolve_tui_command(_settings(tmp_path)) == [
        "/usr/bin/bun",
        str(source),
    ]


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/api/ui",
        "http://user@localhost/api/ui",
        "http://localhost/api/ui?token=secret",
        "http://localhost/api/ui#fragment",
        "http://localhost/wrong",
    ],
)
def test_validate_tui_api_base_rejects_non_loopback_or_ambiguous_urls(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="Native TUI --api-base"):
        runtime.validate_tui_api_base(value)


def test_validate_tui_api_base_accepts_loopback_variants() -> None:
    assert (
        runtime.validate_tui_api_base("http://127.0.0.1:8765/api/ui/")
        == "http://127.0.0.1:8765/api/ui"
    )
    assert (
        runtime.validate_tui_api_base("https://[::1]:9443/api/ui")
        == "https://[::1]:9443/api/ui"
    )


def test_run_tui_keeps_token_out_of_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        runtime, "resolve_tui_command", lambda _settings: ["tui", "--safe"]
    )
    monkeypatch.setattr(
        runtime, "get_or_create_ui_local_token", lambda: "private-token"
    )

    def fake_run(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        calls.append((command, env))
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    result = runtime.run_tui(
        "http://localhost:8765/api/ui", settings=_settings(tmp_path)
    )

    assert result == 7
    assert calls[0][0] == ["tui", "--safe"]
    assert "private-token" not in " ".join(calls[0][0])
    assert calls[0][1][runtime.UI_LOCAL_TOKEN_ENV] == "private-token"
    assert calls[0][1]["WORKGATE_UI_MODE"] == "tui"
