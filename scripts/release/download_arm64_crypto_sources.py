"""Download checksum-verified OpenSSL LTS and locked cryptography source archives."""

import argparse
import hashlib
import tomllib
import urllib.request
from pathlib import Path

OPENSSL_VERSION = "3.5.9"
OPENSSL_SHA256 = (
    "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
)
OPENSSL_URL = (
    "https://github.com/openssl/openssl/releases/download/"
    f"openssl-{OPENSSL_VERSION}/openssl-{OPENSSL_VERSION}.tar.gz"
)
CRYPTOGRAPHY_VERSION = "50.0.2"


def pinned_sources(lock_file: Path) -> list[tuple[str, str, str]]:
    with lock_file.open("rb") as source:
        lock = tomllib.load(source)
    packages = [
        package
        for package in lock["package"]
        if package["name"] == "cryptography"
        and package["version"] == CRYPTOGRAPHY_VERSION
    ]
    if len(packages) != 1:
        raise ValueError("expected one pinned cryptography version in uv.lock")
    source = packages[0]["sdist"]
    if not source["url"].startswith("https://files.pythonhosted.org/"):
        raise ValueError("unexpected cryptography source host")
    if not source["hash"].startswith("sha256:"):
        raise ValueError("cryptography source lacks SHA256 lock")
    return [
        (f"openssl-{OPENSSL_VERSION}.tar.gz", OPENSSL_URL, OPENSSL_SHA256),
        (
            f"cryptography-{CRYPTOGRAPHY_VERSION}.tar.gz",
            source["url"],
            source["hash"].removeprefix("sha256:"),
        ),
    ]


def download(name: str, url: str, expected: str, directory: Path) -> Path:
    destination = directory / name
    digest = hashlib.sha256()
    with (
        urllib.request.urlopen(url, timeout=120) as response,
        destination.open("wb") as output,
    ):
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != expected:
        destination.unlink(missing_ok=True)
        raise ValueError(f"source SHA256 mismatch: {name}")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, url, digest in pinned_sources(args.lock):
        print(f"Verify {name}: SHA256 {digest}", flush=True)
        download(name, url, digest, args.output)


if __name__ == "__main__":
    main()
