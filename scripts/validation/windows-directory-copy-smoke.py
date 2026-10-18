"""Native Windows protected staged directory copy and rollback smoke."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("This smoke requires native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.platform.windows.archive import copy_directory  # noqa: E402
from workgate.platform.windows.security import (  # noqa: E402
    ensure_private_directory,
    open_private_file,
)
from workgate.utils.private_files import write_private_bytes  # noqa: E402


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-copy-directory-") as tmp:
        base = Path(tmp)
        source = base / "source"
        source.mkdir()
        (source / "public.txt").write_bytes(b"public")
        sub = source / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_bytes(b"nested")
        secret_folder = ensure_private_directory(source / "protected")
        write_private_bytes(secret_folder / "private.txt", b"secret")
        destination = base / "copied"
        copy_directory(source, destination, max_entries=100, max_bytes=10000)
        assert (destination / "public.txt").read_bytes() == b"public"
        assert (destination / "sub/nested.txt").read_bytes() == b"nested"
        with os.fdopen(
            open_private_file(
                destination / "protected/private.txt", create=False
            ),
            "rb",
        ) as reader:
            assert reader.read() == b"secret"
        assert not list(base.glob(".copied.copy-*"))

        try:
            copy_directory(
                source, destination, max_entries=100, max_bytes=10000
            )
        except FileExistsError:
            assert (destination / "public.txt").read_bytes() == b"public"
        else:
            raise AssertionError(
                "Directory copy clobbered an existing destination"
            )
        assert not list(base.glob(".copied.copy-*"))

        for entries, size in ((1, 10000), (100, 2)):
            name = f"rejected-{entries}-{size}"
            try:
                copy_directory(
                    source, base / name, max_entries=entries, max_bytes=size
                )
            except ValueError:
                assert not (base / name).exists()
                assert not list(base.glob(f".{name}.copy-*"))
            else:
                raise AssertionError("Directory copy ignored resource limit")

        outside = base / "untouched"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"keep")
        link = source / "external-junction"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if created.returncode:
            raise AssertionError("Junction creation unavailable for copy test")
        try:
            copy_directory(
                source, base / "unsafe", max_entries=100, max_bytes=10000
            )
        except PermissionError:
            assert not (base / "unsafe").exists()
            assert not list(base.glob(".unsafe.copy-*"))
            assert (outside / "keep.txt").read_bytes() == b"keep"
        else:
            raise AssertionError("Windows copy followed a junction")
        link.rmdir()
    print("Windows native directory copy, ACL and rollback: PASS")


if __name__ == "__main__":
    run()
