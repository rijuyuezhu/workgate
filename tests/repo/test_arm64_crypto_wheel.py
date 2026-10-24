"""Portable fail-closed tests for the Windows ARM64 wheel verification gate."""

import importlib.util
import struct
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def verifier():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts/release/verify_arm64_crypto_wheel.py"
    )
    spec = importlib.util.spec_from_file_location("arm64_verifier", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_pe(machine: int, imported: str | None = None) -> bytes:
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x84, machine, 1)
    struct.pack_into("<H", data, 0x94, 0xF0)
    struct.pack_into("<H", data, 0x98, 0x20B)
    section = 0x98 + 0xF0
    struct.pack_into("<IIII", data, section + 8, 0x200, 0x1000, 0x200, 0x200)
    if imported is not None:
        struct.pack_into("<II", data, 0x98 + 112 + 8, 0x1000, 40)
        struct.pack_into("<I", data, 0x200 + 12, 0x1080)
        name = imported.encode() + b"\0"
        data[0x280 : 0x280 + len(name)] = name
    return bytes(data)


def fake_wheel(
    tmp_path: Path,
    machine: int = 0xAA64,
    imported: str | None = None,
    tag: str = "cp39-abi3-win_arm64",
) -> Path:
    wheel = tmp_path / "cryptography-50.0.2-test.whl"
    with zipfile.ZipFile(wheel, "w") as package:
        package.writestr(
            "cryptography/hazmat/bindings/_rust.pyd", fake_pe(machine, imported)
        )
        package.writestr(
            "cryptography-50.0.2.dist-info/WHEEL",
            f"Wheel-Version: 1.0\nTag: {tag}\n",
        )
    return wheel


def test_arm64_static_wheel_is_accepted(verifier, tmp_path):
    result = verifier.verify_wheel(
        fake_wheel(tmp_path, imported="KERNEL32.dll")
    )
    assert result["pe_machine"] == "ARM64 (0xAA64)"
    assert result["openssl_dynamic_imports"] == []
    assert "kernel32.dll" in result["dynamic_imports"]


@pytest.mark.parametrize("machine", [0x8664, 0x14C])
def test_non_arm64_native_extension_rejected(verifier, tmp_path, machine):
    with pytest.raises(ValueError, match="not native ARM64"):
        verifier.verify_wheel(fake_wheel(tmp_path, machine=machine))


@pytest.mark.parametrize(
    "imported", ["libcrypto-3-arm64.dll", "LIBSSL-3-ARM64.dll"]
)
def test_dynamic_openssl_extension_rejected(verifier, tmp_path, imported):
    with pytest.raises(ValueError, match="statically linked"):
        verifier.verify_wheel(fake_wheel(tmp_path, imported=imported))


def test_x64_wheel_metadata_rejected(verifier, tmp_path):
    with pytest.raises(ValueError, match="native Windows ARM64 tag"):
        verifier.verify_wheel(fake_wheel(tmp_path, tag="cp39-abi3-win_amd64"))


def test_cryptography_source_hash_comes_from_lockfile():
    location = (
        Path(__file__).resolve().parents[2]
        / "scripts/release/download_arm64_crypto_sources.py"
    )
    spec = importlib.util.spec_from_file_location("crypto_sources", location)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lock = Path(__file__).resolve().parents[2] / "uv.lock"
    sources = module.pinned_sources(lock)
    assert sources[0][2] == module.OPENSSL_SHA256
    assert (
        sources[1][2]
        == "7b46165bb56eb4704e2eaaf86f3c940d19154535d9b0ca7d6d590b04060e00d5"
    )


def test_source_download_checksum_mismatch_removes_file(tmp_path, monkeypatch):
    import io
    import sys

    root = Path(__file__).resolve().parents[2]
    path = root / "scripts/release/download_arm64_crypto_sources.py"
    spec = importlib.util.spec_from_file_location("source_downloader", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *a, **kw: io.BytesIO(b"untrusted source archive"),
    )
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        module.download(
            "archive.tar.gz",
            "https://files.pythonhosted.org/fake",
            "0" * 64,
            tmp_path,
        )
    assert not (tmp_path / "archive.tar.gz").exists()
