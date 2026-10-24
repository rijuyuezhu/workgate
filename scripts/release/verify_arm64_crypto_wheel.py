"""Fail-closed preflight for an independently built cryptography win_arm64 wheel."""

import argparse
import hashlib
import json
import struct
import zipfile
from pathlib import Path


def _pe_imports(data: bytes) -> tuple[int, set[str]]:
    if data[:2] != b"MZ":
        raise ValueError("extension is not a Windows PE binary")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        raise ValueError("invalid PE header")
    machine, section_count = struct.unpack_from("<HH", data, pe + 4)
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic != 0x20B:
        raise ValueError("extension must be PE32+")
    sections = []
    for i in range(section_count):
        pos = opt + opt_size + 40 * i
        virtual_size, rva, raw_size, raw = struct.unpack_from(
            "<IIII", data, pos + 8
        )
        sections.append((rva, max(virtual_size, raw_size), raw))

    def offset(rva: int) -> int:
        for base, size, raw in sections:
            if base <= rva < base + size:
                return raw + rva - base
        raise ValueError(f"PE import RVA cannot be mapped: {rva:#x}")

    imports: set[str] = set()
    directories = opt + 112
    for kind, stride, name_pos in ((1, 20, 12), (13, 32, 4)):
        rva = struct.unpack_from("<I", data, directories + 8 * kind)[0]
        if not rva:
            continue
        entry = offset(rva)
        for _ in range(256):
            if data[entry : entry + stride] == bytes(stride):
                break
            if kind == 13 and struct.unpack_from("<I", data, entry)[0] != 1:
                raise ValueError("unsupported absolute-address PE delay import")
            name_rva = struct.unpack_from("<I", data, entry + name_pos)[0]
            start = offset(name_rva)
            end = data.index(b"\0", start)
            imports.add(data[start:end].decode("ascii").lower())
            entry += stride
        else:
            raise ValueError("PE import table not terminated")
    return machine, imports


def verify_wheel(path: Path, version: str = "50.0.2") -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        rust = [
            name for name in archive.namelist() if name.endswith("/_rust.pyd")
        ]
        if len(rust) != 1 or not rust[0].startswith("cryptography/"):
            raise ValueError("expected exactly one cryptography Rust extension")
        native_files = [
            name
            for name in archive.namelist()
            if name.lower().endswith((".pyd", ".dll", ".exe"))
        ]
        if native_files != rust:
            raise ValueError(
                "unexpected bundled native binary alongside Rust extension"
            )
        metadata = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/WHEEL")
        ]
        if len(metadata) != 1 or not metadata[0].startswith(
            f"cryptography-{version}.dist-info/"
        ):
            raise ValueError("cryptography wheel version mismatch")
        wheel_metadata = archive.read(metadata[0]).decode()
        if not any(
            line.startswith("Tag: ") and line.endswith("-win_arm64")
            for line in wheel_metadata.splitlines()
        ):
            raise ValueError(
                "wheel does not declare a native Windows ARM64 tag"
            )
        machine, imports = _pe_imports(archive.read(rust[0]))
    if machine != 0xAA64:
        raise ValueError(
            f"cryptography extension is not native ARM64: {machine:#x}"
        )
    forbidden = [
        name
        for name in imports
        if name.startswith(("libcrypto", "libssl", "libeay", "ssleay"))
    ]
    if forbidden:
        raise ValueError(f"OpenSSL must be statically linked: {forbidden}")
    return {
        "wheel": path.name,
        "wheel_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "cryptography_version": version,
        "pe_machine": "ARM64 (0xAA64)",
        "dynamic_imports": sorted(imports),
        "openssl_dynamic_imports": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--openssl-version", required=True)
    parser.add_argument("--openssl-source-sha256", required=True)
    parser.add_argument("--cryptography-source-sha256", required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    args = parser.parse_args()
    result = verify_wheel(args.wheel)
    result.update(
        openssl_version=args.openssl_version,
        openssl_source_sha256=args.openssl_source_sha256,
        cryptography_source_sha256=args.cryptography_source_sha256,
    )
    args.provenance.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
