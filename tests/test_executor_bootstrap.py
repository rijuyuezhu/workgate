import hashlib
import os
import platform
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from workgate import __version__
from workgate.control import executor_bootstrap as bootstrap


def test_checksum_parser_validates_digest() -> None:
    digest = "A" * 64
    assert (
        bootstrap._parse_checksum(f"{digest}  workgate-linux-x86_64.tar.gz\n")
        == digest.lower()
    )

    with pytest.raises(
        bootstrap._BootstrapUnavailable,
        match="checksum is invalid",
    ):
        bootstrap._parse_checksum("not-a-checksum\n")


def test_bootstrap_routes_are_public_and_target_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "a" * 64

    async def fake_checksum(target: str) -> str:
        assert target == "linux-x86_64"
        return digest

    monkeypatch.setattr(bootstrap, "_fetch_checksum", fake_checksum)

    client = TestClient(
        Starlette(
            routes=bootstrap.executor_bootstrap_routes(
                "https://control.example/"
            )
        )
    )

    script = client.get(bootstrap._BOOTSTRAP_PATH)
    assert script.status_code == 200
    assert script.headers["cache-control"] == "no-store"
    assert "executor connect" in script.text
    assert "executor install-service" in script.text
    assert "tar -xOzf" in script.text
    assert "tar -xzf" not in script.text
    assert "Bearer" not in script.text
    assert "device_code" not in script.text
    assert "invite" not in script.text
    assert "github.com" not in script.text
    assert "https://control.example/executor/v1/bootstrap" in script.text

    checksum = client.get(bootstrap._BOOTSTRAP_PATH + "/linux-x86_64/sha256")
    assert checksum.status_code == 200
    assert checksum.text == f"{digest}\n"

    missing = client.get(bootstrap._BOOTSTRAP_PATH + "/windows-x86_64/sha256")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_archive_proxy_enforces_streaming_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"12345")
    )

    monkeypatch.setattr(bootstrap, "_MAX_ARCHIVE_BYTES", 4)
    monkeypatch.setattr(
        bootstrap.httpx,
        "AsyncClient",
        lambda **_kwargs: real_client(transport=transport),
    )

    with pytest.raises(
        bootstrap._BootstrapUnavailable,
        match="exceeds the size limit",
    ):
        _ = [chunk async for chunk in bootstrap._archive_stream("linux-x86_64")]


def _host_bootstrap_target() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if system == "Linux" and machine in {"aarch64", "arm64"}:
        return "linux-aarch64"
    if system == "Darwin" and machine in {"x86_64", "amd64"}:
        return "macos-x86_64"
    if system == "Darwin" and machine in {"aarch64", "arm64"}:
        return "macos-aarch64"
    pytest.skip("POSIX executor bootstrap is unsupported on this test host")


def _persistent_runtime_dir(tmp_path: Path) -> Path:
    target = _host_bootstrap_target()
    if target.startswith("macos-"):
        return (
            tmp_path
            / "home"
            / "Library"
            / "Application Support"
            / "workgate"
            / "executor-bootstrap"
        )
    return tmp_path / "data" / "workgate" / "executor-bootstrap"


def _fake_release_archive(
    tmp_path: Path,
    target: str,
) -> tuple[Path, str]:
    tree = tmp_path / "archive"
    runtime = tree / f"workgate-{target}" / "workgate"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then\n'
        f'  echo "workgate {__version__}"\n'
        "  exit 0\n"
        "fi\n"
        'printf \'%s\\n\' "$*" >> "$BOOTSTRAP_CALLS"\n'
        'if [ "$BOOTSTRAP_FAIL_INSTALL" = "1" ] '
        '&& [ "$1 $2" = "executor install-service" ]; then\n'
        "  exit 42\n"
        "fi\n"
        "exit 0\n"
    )

    archive = tmp_path / f"workgate-{target}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(
            runtime,
            arcname=f"workgate-{target}/workgate",
        )
        escape = tmp_path / "escape-payload"
        escape.write_text("must-not-be-extracted")
        handle.add(escape, arcname="../../bootstrap-escape")

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, digest


def _fake_curl(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import os, shutil, sys\n"
        "url = next(a for a in reversed(sys.argv) if a.startswith('http'))\n"
        "if url.endswith('.sha256') or url.endswith('/sha256'):\n"
        "    print(os.environ['BOOTSTRAP_TEST_SHA'])\n"
        "else:\n"
        "    out = sys.argv[sys.argv.index('-o') + 1]\n"
        "    shutil.copyfile(os.environ['BOOTSTRAP_TEST_ARCHIVE'], out)\n"
    )
    curl.chmod(0o755)
    return bindir


def _run_bootstrap(
    tmp_path: Path,
    *,
    persist: bool,
    fail_install: bool = False,
) -> subprocess.CompletedProcess[str]:
    target = _host_bootstrap_target()
    archive, digest = _fake_release_archive(tmp_path, target)
    bindir = _fake_curl(tmp_path)
    script = tmp_path / "bootstrap.sh"
    script.write_text(bootstrap.bootstrap_script("https://control.example"))

    calls = tmp_path / "calls"
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(bindir) + os.pathsep + env["PATH"],
            "HOME": str(tmp_path / "home"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "BOOTSTRAP_CALLS": str(calls),
            "BOOTSTRAP_FAIL_INSTALL": "1" if fail_install else "0",
            "BOOTSTRAP_TEST_ARCHIVE": str(archive),
            "BOOTSTRAP_TEST_SHA": digest,
        }
    )
    args = [
        "bash",
        str(script),
        "--name",
        "node-a",
        "--default-workdir",
        "/srv/work",
    ]
    if persist:
        args.append("--persist")
    return subprocess.run(
        args,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_bootstrap_script_composes_pairing_and_persistent_service(
    tmp_path: Path,
) -> None:
    result = _run_bootstrap(tmp_path, persist=True)

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls == [
        "executor connect https://control.example --name node-a",
        "executor install-service --runtime-ownership control-managed --default-workdir /srv/work",
    ]
    runtime = _persistent_runtime_dir(tmp_path) / "workgate"
    assert runtime.is_file()
    assert not (tmp_path.parent / "bootstrap-escape").exists()


def test_bootstrap_script_rolls_back_runtime_when_service_install_fails(
    tmp_path: Path,
) -> None:
    runtime_dir = _persistent_runtime_dir(tmp_path)
    runtime_dir.mkdir(parents=True)
    runtime = runtime_dir / "workgate"
    runtime.write_text("old-runtime")

    result = _run_bootstrap(
        tmp_path,
        persist=True,
        fail_install=True,
    )

    assert result.returncode == 42
    assert runtime.read_text() == "old-runtime"
    assert not list(runtime_dir.glob(".workgate.*"))


def test_nonpersistent_bootstrap_runs_foreground_without_installing(
    tmp_path: Path,
) -> None:
    result = _run_bootstrap(tmp_path, persist=False)

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls == [
        "executor connect https://control.example --name node-a",
        "executor run --default-workdir /srv/work",
    ]
    assert not _persistent_runtime_dir(tmp_path).exists()
