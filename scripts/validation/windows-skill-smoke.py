"""Native Windows managed Skill security/lifecycle smoke (run with pydantic+pyyaml)."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("This smoke requires native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.agent_bridge import skill_management as skills  # noqa: E402
from workgate.platform.windows.security import open_private_file  # noqa: E402

ENTRY = "---\ndescription: Windows managed Skill\n---\n# Native Skill\n"


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-native-skill-") as tmp:
        base = Path(tmp) / "agent"
        first = skills.manage_skill(
            base, "create", name="first", content=ENTRY
        )["skill"]
        assert first["enabled"]
        with os.fdopen(
            open_private_file(base / "skills/first/SKILL.md", create=False),
            "rb",
        ) as reader:
            assert reader.read() == ENTRY.encode()
        assert (
            skills.manage_skill(base, "get", name="first")["skill"]["content"]
            == ENTRY
        )
        assert skills.manage_skill(base, "list")["skills"][0]["name"] == "first"

        staged = skills.manage_skill(
            base, "stage", name="second", content=ENTRY
        )["skill"]
        assert not staged["enabled"]
        updated = skills.manage_skill(
            base,
            "update",
            name="first",
            content=ENTRY + "updated\n",
            expected_revision=first["revision"],
        )["skill"]
        try:
            skills.manage_skill(
                base,
                "update",
                name="first",
                content=ENTRY,
                expected_revision=first["revision"],
            )
        except ValueError:
            assert (
                (base / "skills/first/SKILL.md")
                .read_text()
                .endswith("updated\n")
            )
        else:
            raise AssertionError("stale Windows Skill revision was accepted")
        disabled = skills.manage_skill(
            base,
            "disable",
            name="first",
            expected_revision=updated["revision"],
        )["skill"]
        assert not disabled["enabled"]
        enabled = skills.manage_skill(
            base,
            "enable",
            name="first",
            expected_revision=disabled["revision"],
        )["skill"]
        assert enabled["enabled"]

        saved_move = skills._move_skill_directory

        def fail_move(_source: Path, _destination: Path) -> None:
            raise OSError("simulated native Skill staging commit failure")

        skills._move_skill_directory = fail_move
        try:
            try:
                skills.manage_skill(base, "stage", name="failed", content=ENTRY)
            except OSError as exc:
                assert "simulated" in str(exc)
            else:
                raise AssertionError(
                    "injected staging failure did not propagate"
                )
        finally:
            skills._move_skill_directory = saved_move
        assert not (base / "skills/failed").exists()
        assert not list((base / "skills").glob(".creating-*"))
        assert "failed" not in (
            skills.manage_skill(base, "list")["skills"][0]["name"],
            skills.manage_skill(base, "list")["skills"][-1]["name"],
        )
        assert b"failed" not in (base / "config.json").read_bytes()

        second = base / "skills/second"
        (second / "asset.txt").write_bytes(b"auxiliary")
        try:
            skills.manage_skill(
                base,
                "delete",
                name="second",
                expected_revision=staged["revision"],
                entry_only=True,
            )
        except ValueError:
            assert (second / "asset.txt").exists()
        else:
            raise AssertionError("entry-only delete ignored assets")
        external = Path(tmp) / "preserved"
        external.mkdir()
        (external / "secret").write_bytes(b"intact")
        junction = second / "unsafe-junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode:
            raise AssertionError("Windows Skill junction test unavailable")
        try:
            skills.manage_skill(
                base,
                "delete",
                name="second",
                expected_revision=staged["revision"],
            )
        except ValueError:
            assert second.exists()
        else:
            raise AssertionError("unsafe Skill bundle junction accepted")
        junction.rmdir()
        assert (
            skills.manage_skill(
                base,
                "delete",
                name="second",
                expected_revision=staged["revision"],
            )["deleted"]
            == "second"
        )
        assert (external / "secret").read_bytes() == b"intact"
        assert (
            skills.manage_skill(
                base,
                "delete",
                name="first",
                expected_revision=enabled["revision"],
            )["deleted"]
            == "first"
        )
        assert not (base / "skills/first").exists()
        assert not list((base / "skills").glob(".deleting-*"))
    print("Windows native managed Skill CRUD and rollback: PASS")


if __name__ == "__main__":
    run()
