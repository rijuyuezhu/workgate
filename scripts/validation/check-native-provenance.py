#!/usr/bin/env python3
"""Verify documented native-artifact inputs and distribution notices."""

import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROVENANCE = REPO / "docs" / "maintenance" / "native-artifact-provenance.md"
EXPECTED_HASHES = {
    "ui-opentui/package.json": "a9b46c8bc5ceb1e3c8a1cc58af555783b49efc0a8f8475d497ecf8f2c709be83",
    "ui-opentui/bun.lock": "583acaf7f7933f2c741c944f448164dd1a64cc82db8c78fb57e93291923dff81",
    "src/workgate/ui/contracts.py": "5b6dcda2bf52fbcc357140782952cd4405e99d8b5bbe4169279b229693685425",
    "scripts/generation/generate-tui-executable-contract.py": "a99480cf845b1adc7f6877f953712530c97eb0c084bbaacd235635db9ebea767",
    "ui-opentui/scripts/executable-contract.ts": "ac972b10fbf21ccef6e94ece2a9bc44a701607d822a1333cf991a68c91d45b13",
    "ui-opentui/scripts/compile-tui.ts": "df87f2c0e891651a44dbda60b5d3cda6007592ee008235bc958ca111e27ad298",
    "scripts/release/build-platform-wheel.py": "a4861240c4189475ae3023a8db93a660faa6cc6acb47b0c6d457cea91405c947",
    "scripts/release/smoke-platform-wheel.py": "b729eb1f88b6d51e72770c38319ad70a2707f2ce20a7a023b0ee875a288a2d92",
    "src/workgate/helpers/opentui.NOTICES": "ddccf8cb0fa731bdca8e0f499a4a2e06deaf155d587cff3af77379201cd00e35",
    "src/workgate/helpers/bun-1.3.14.LICENSE.md": "2c6160ec8fb853f7e8f97d9b249e756c9b0ac44860a68b6bf4f1b0bcbc5c3741",
}


def _sha256(path: Path) -> str:
    # Git may check text files out as CRLF on Windows. Hash the canonical UTF-8
    # text with universal-newline normalization so one locked source has the
    # same digest on every supported runner.
    normalized = path.read_text(encoding="utf-8").encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def main() -> int:
    provenance = PROVENANCE.read_text(encoding="utf-8")
    failures: list[str] = []
    for relative, expected in EXPECTED_HASHES.items():
        actual = _sha256(REPO / relative)
        if actual != expected:
            failures.append(f"{relative}: expected {expected}, got {actual}")
        if expected not in provenance:
            failures.append(
                f"{relative}: hash is not recorded in provenance docs"
            )

    required_fragments = {
        "scripts/generation/generate-tui-executable-contract.py": "render_contract",
        "ui-opentui/scripts/compile-tui.ts": 'from "./executable-contract"',
        "pyproject.toml": '"src/workgate/helpers/opentui.NOTICES"',
        ".github/workflows/release.yml": "BUN-1.3.14-LICENSE.md",
        "scripts/validation/check-release-matrix.py": '"BUN-1.3.14-LICENSE.md"',
        "mkdocs.yml": "maintenance/native-artifact-provenance.md",
    }
    repository_only = {".github/workflows/release.yml", "mkdocs.yml"}
    for relative, fragment in required_fragments.items():
        path = REPO / relative
        if not path.exists() and relative in repository_only:
            continue
        text = path.read_text(encoding="utf-8")
        if fragment not in text:
            failures.append(f"{relative}: missing {fragment!r}")

    if failures:
        print("Native artifact provenance check failed.")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(
        "Native artifact provenance matches locked OpenTUI inputs, notices, and "
        "release packaging."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
