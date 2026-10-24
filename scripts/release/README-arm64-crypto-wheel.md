# Experimental native Windows ARM64 cryptography wheel builder

This is **only a review-stage CI artifact** for [#475](https://github.com/rijuyuezhu/workgate/issues/475).
It does **not** publish a release or change WorkGate installation resolution.

The independent `windows-11-arm` workflow builds PyCA `cryptography==50.0.2`
from the SHA256 pinned in `uv.lock`, using statically linked
[OpenSSL 3.5.9](https://www.openssl-library.org/source/) (LTS).
The OpenSSL source SHA256 is
`603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a`.

The build requires native Python 3.14, Rust, Perl, and initialized ARM64 MSVC.
It deliberately fails rather than falling back to an x64 wheel or dynamically
linked OpenSSL. The verifier checks the wheel's version/tag, extension PE ARM64
machine and all ordinary/delay import tables; a clean native environment runs
cryptography **before** Python `ssl`, exercises AES-GCM and checks the
cryptography-linked OpenSSL runtime is exactly 3.5.9.

The test environment installs using wheel-only resolution with OpenSSL SDK
variables removed, so source compilation is forbidden. The verifier rejects
extra bundled native DLLs as well as dynamically imported OpenSSL.

The CI artifact includes a wheel and a JSON SHA256/source-provenance report
for short-lived human review. **It is not trusted or installed by
`deploy/source/prepare.py`**, and must not be used as a public release until
a separate review has validated provenance, reproducible builds, security
updates, immutable publication and pinned integrity verification at install
time. Never publish a wheel built against OpenSSL 3.0.22 from an EOL host.
