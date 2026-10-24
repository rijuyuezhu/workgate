"""Native ARM64 smoke: cryptography loads before Python SSL, and AES-GCM works."""

import platform

from cryptography import __version__
from cryptography.hazmat.bindings.openssl.binding import Binding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

assert platform.machine().upper() in {"ARM64", "AARCH64"}
assert __version__ == "50.0.2"
key = AESGCM.generate_key(bit_length=256)
cipher = AESGCM(key)
nonce = bytes(range(12))
assert (
    cipher.decrypt(nonce, cipher.encrypt(nonce, b"ARM64", None), None)
    == b"ARM64"
)
linked_openssl = Binding().ffi.string(Binding().lib.OpenSSL_version(0)).decode()
assert linked_openssl.startswith("OpenSSL 3.5.9 "), linked_openssl

import ssl  # noqa: E402 - deliberately load Python SSL after cryptography

print("ARM64_STATIC_CRYPTOGRAPHY_OK", linked_openssl, ssl.OPENSSL_VERSION)
