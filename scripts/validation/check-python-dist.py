#!/usr/bin/env python3
"""Validate the single pure Python wheel and source distribution without dependencies."""

import sys
import tarfile
import zipfile
from pathlib import Path


def main() -> int:
    dist = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("dist")
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            "expected exactly one wheel and one source distribution"
        )
    wheel = wheels[0]
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise SystemExit(f"wheel must be universal: {wheel.name}")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        wheel_metadata = [
            name for name in names if name.endswith(".dist-info/WHEEL")
        ]
        if len(wheel_metadata) != 1:
            raise SystemExit("expected exactly one WHEEL metadata file")
        metadata = archive.read(wheel_metadata[0]).decode("utf-8")
        if (
            "Root-Is-Purelib: true" not in metadata
            or "Tag: py3-none-any" not in metadata
        ):
            raise SystemExit(
                "wheel metadata must declare a pure universal wheel"
            )
        if "workgate/ui/static/xterm_bundle.js" not in names:
            raise SystemExit(
                "universal wheel must include browser terminal assets"
            )
        if any(
            "ui_runtime/" in name or "ui-opentui/" in name for name in names
        ):
            raise SystemExit(
                "universal wheel contains obsolete native UI payload"
            )
    with tarfile.open(sdists[0], "r:gz") as archive:
        names = {name.removeprefix("/") for name in archive.getnames()}
        if not any(
            name.endswith("/ui-terminal/package-lock.json") for name in names
        ):
            raise SystemExit("sdist must include browser terminal build inputs")
        if any(
            "/ui-opentui/" in name or "/ui_runtime/" in name for name in names
        ):
            raise SystemExit(
                "sdist contains obsolete native UI source or payload"
            )
    print("Verified one universal Python wheel and one source distribution")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
