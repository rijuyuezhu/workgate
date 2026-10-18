"""Smoke core WorkGate file workflows on native Windows using Python stdlib."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Run this with native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.agent_bridge.auth_store import AgentAuthStore  # noqa: E402
from workgate.agent_bridge.skill_management import manage_skill  # noqa: E402
from workgate.app_paths import ensure_private_directory, resolve_app_paths  # noqa: E402
from workgate.executor.files import FilesConfig  # noqa: E402
from workgate.executor.ui_files import UiFilesService  # noqa: E402
from workgate.persistence import FileStateStore, use_state_store  # noqa: E402
from workgate.utils.private_files import private_file_lock  # noqa: E402


def run() -> None:
    locations = resolve_app_paths(platform="win32")
    assert "AppData" in str(locations.config_dir)
    assert "AppData" in str(locations.state_dir)

    with tempfile.TemporaryDirectory(prefix="workgate-stdlib-") as tmp:
        root = Path(tmp)
        ensure_private_directory(root / "private")
        locked = root / "private" / "test.lock"
        with private_file_lock(locked):
            contested = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--try-lock",
                    str(locked),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )
            assert "contended: PASS" in contested.stdout, contested.stderr

        credentials = AgentAuthStore(root / "auth")
        credentials.set_secret("server", "token", "secret")
        assert credentials.get_secret("server", "token") == "secret"
        assert credentials.delete_secret("server", "token")

        base = root / "agent"
        content = "---\ndescription: native stdlib\n---\n# Skill\n"
        created = manage_skill(base, "create", name="notes", content=content)[
            "skill"
        ]
        assert (
            manage_skill(base, "get", name="notes")["skill"]["content"]
            == content
        )
        assert (
            manage_skill(
                base,
                "delete",
                name="notes",
                expected_revision=created["revision"],
            )["deleted"]
            == "notes"
        )

        workdir = root / "workspace"
        workdir.mkdir()
        config = FilesConfig(
            default_workdir=workdir,
            max_directory_entries=100,
            max_file_read_bytes=100_000,
            max_file_write_bytes=100_000,
        )
        with use_state_store(FileStateStore(lambda: root / "state")):
            files = UiFilesService(config, None)
            files.mkdir("nested")
            (workdir / "nested" / "one.txt").write_text(
                "hello", encoding="utf-8"
            )
            files.copy_entry("nested/one.txt", "nested/two.txt")
            assert (workdir / "nested/two.txt").read_text(
                encoding="utf-8"
            ) == "hello"
            files.move_entry("nested/two.txt", "nested/three.txt")
            assert (workdir / "nested/three.txt").exists()
            files.delete("nested/three.txt", recursive=False)
            assert not (workdir / "nested/three.txt").exists()
            assert files.list_payload("nested")["count"] == 1

    print(
        "Windows Python stdlib paths, locks, credentials, Skills and files: PASS"
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--try-lock":
        try:
            with private_file_lock(Path(sys.argv[2]), timeout_s=0.1):
                raise AssertionError("Concurrent Windows byte lock was allowed")
        except TimeoutError:
            print("Windows interprocess lock contended: PASS")
    else:
        run()
