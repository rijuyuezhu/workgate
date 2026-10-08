import os
from pathlib import Path

import pytest

import workgate.agent_bridge.skills as skills
from workgate.agent_bridge.models import SkillRecord


def _install_skill(
    config_dir: Path,
    name: str = "debugging",
    *,
    content: str = "# Debugging\n\nFind root causes.\n",
) -> Path:
    skill_dir = config_dir / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def _symlink_or_skip(
    path: Path, target: Path, *, target_is_directory: bool = False
) -> None:
    try:
        path.symlink_to(target, target_is_directory=target_is_directory)
    except NotImplementedError, OSError:
        pytest.skip("symlink creation is unavailable")


def test_scan_agent_skills_reads_skill_md_with_bounded_related_paths(tmp_path):
    skill_dir = _install_skill(
        tmp_path,
        "paper-writer",
        content="# Paper Writer\n\nHelps draft ML papers.\n",
    )
    (skill_dir / "template.md").write_text("template", encoding="utf-8")

    result = skills.scan_agent_skills(tmp_path, "skills")

    assert result.warnings == []
    assert result.scanned_entries >= 3
    assert result.skills == {
        "paper-writer": SkillRecord(
            name="paper-writer",
            source="managed",
            source_path=str((tmp_path / "skills").resolve()),
            entry_path="skills/paper-writer/SKILL.md",
            description="Helps draft ML papers.",
            related_files=["template.md"],
        )
    }


def test_scan_agent_skills_skips_missing_entry(tmp_path):
    (tmp_path / "skills" / "broken").mkdir(parents=True)

    result = skills.scan_agent_skills(tmp_path, "skills")

    assert result.skills == {}
    assert len(result.warnings) == 1
    assert "missing SKILL.md" in result.warnings[0]


def test_scan_agent_skills_follows_symlinked_entry(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nShared instructions.\n", encoding="utf-8")
    skill_dir = tmp_path / "skills" / "escape"
    skill_dir.mkdir(parents=True)
    _symlink_or_skip(skill_dir / "SKILL.md", outside)

    result = skills.scan_agent_skills(tmp_path, "skills")
    record = result.skills["escape"]
    activated = skills.activate_skill(tmp_path, record)

    assert result.warnings == []
    assert record.entry_path == "skills/escape/SKILL.md"
    assert record.description == "Shared instructions."
    assert activated["content"] == "# Outside\n\nShared instructions.\n"


def test_scan_agent_skills_follows_symlinked_skill_directory(tmp_path):
    config_dir = tmp_path / "config"
    skills_dir = config_dir / "skills"
    skills_dir.mkdir(parents=True)
    shared = tmp_path / "shared-skill"
    shared.mkdir()
    (shared / "SKILL.md").write_text(
        "# Shared\n\nShared directory skill.\n", encoding="utf-8"
    )
    (shared / "guide.md").write_text("shared guide", encoding="utf-8")
    _symlink_or_skip(skills_dir / "linked", shared, target_is_directory=True)

    result = skills.scan_agent_skills(config_dir, "skills")
    record = result.skills["linked"]
    activated = skills.activate_skill(config_dir, record)

    assert result.warnings == []
    assert record.entry_path == "skills/linked/SKILL.md"
    assert record.related_files == ["guide.md"]
    assert activated["content"] == "# Shared\n\nShared directory skill.\n"


def test_broken_symlinked_skills_root_is_a_bounded_warning(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _symlink_or_skip(
        config_dir / "skills",
        tmp_path / "missing-skills",
        target_is_directory=True,
    )

    result = skills.scan_agent_skills(config_dir, "skills")

    assert result.skills == {}
    assert result.warnings == [
        "Skills directory symlink target is unavailable: skills"
    ]


def test_looping_symlinked_skills_root_is_a_bounded_warning(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    skills_root = config_dir / "skills"
    loop_target = config_dir / "loop"
    _symlink_or_skip(skills_root, loop_target, target_is_directory=True)
    _symlink_or_skip(loop_target, skills_root, target_is_directory=True)

    result = skills.scan_agent_skills(config_dir, "skills")

    assert result.skills == {}
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith(
        "Could not inspect skills directory skills:"
    )


def test_scan_agent_skills_preserves_lexical_symlinked_source_root(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shared_root = tmp_path / "shared-skills"
    shared_skill = shared_root / "debugging"
    shared_skill.mkdir(parents=True)
    (shared_skill / "SKILL.md").write_text(
        "# Debugging\n\nFollow the links.\n", encoding="utf-8"
    )
    _symlink_or_skip(
        config_dir / "skills", shared_root, target_is_directory=True
    )

    result = skills.scan_agent_skills(config_dir, "skills")
    record = result.skills["debugging"]

    assert record.source_path == str(config_dir.resolve() / "skills")
    assert record.entry_path == "skills/debugging/SKILL.md"
    assert str(shared_root.resolve()) not in record.entry_path


def test_related_symlink_file_is_exposed_and_read_lexically(tmp_path):
    skill_dir = _install_skill(tmp_path)
    shared = tmp_path / "shared-guide.md"
    shared.write_text("shared guide", encoding="utf-8")
    _symlink_or_skip(skill_dir / "guide.md", shared)

    result = skills.scan_agent_skills(tmp_path)
    record = result.skills["debugging"]
    related = skills.read_agent_skill_file(tmp_path, "debugging", "guide.md")
    limited = skills.scan_agent_skills(
        tmp_path, max_path_bytes=len(b"guide.md") - 1
    )

    assert record.related_files == ["guide.md"]
    assert related["path"] == "guide.md"
    assert related["content"] == "shared guide"
    assert limited.skills["debugging"].related_files == []
    assert any(
        "path" in warning and "truncated" in warning
        for warning in limited.warnings
    )


def test_related_symlink_directory_uses_logical_paths_and_breaks_loops(
    tmp_path,
):
    skill_dir = _install_skill(tmp_path)
    shared_docs = tmp_path / "shared-docs"
    shared_docs.mkdir()
    (shared_docs / "guide.md").write_text("linked docs", encoding="utf-8")
    _symlink_or_skip(skill_dir / "docs", shared_docs, target_is_directory=True)
    _symlink_or_skip(shared_docs / "back", skill_dir, target_is_directory=True)

    result = skills.scan_agent_skills(tmp_path, max_scan_entries=20)
    record = result.skills["debugging"]
    related = skills.read_agent_skill_file(
        tmp_path, "debugging", "docs/guide.md"
    )

    assert record.related_files == ["docs/guide.md"]
    assert related["content"] == "linked docs"
    assert result.scanned_entries <= 6


def test_broken_related_symlink_is_a_bounded_warning(tmp_path):
    skill_dir = _install_skill(tmp_path)
    _symlink_or_skip(skill_dir / "missing.md", tmp_path / "missing-related.md")

    result = skills.scan_agent_skills(tmp_path)

    assert result.skills["debugging"].related_files == []
    assert any(
        "Skipping related path missing.md" in warning
        for warning in result.warnings
    )


def test_broken_skill_symlink_is_a_bounded_warning(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _symlink_or_skip(
        skills_dir / "broken",
        tmp_path / "missing-skill",
        target_is_directory=True,
    )

    result = skills.scan_agent_skills(tmp_path)

    assert result.skills == {}
    assert len(result.warnings) == 1
    assert "Skipping skill 'broken'" in result.warnings[0]


def test_scan_agent_skills_rejects_directories_outside_config_root(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    outside = tmp_path / "outside"

    relative_result = skills.scan_agent_skills(config_dir, "../outside")
    absolute_result = skills.scan_agent_skills(config_dir, str(outside))

    assert relative_result.skills == {}
    assert absolute_result.skills == {}
    assert "inside config directory" in relative_result.warnings[0]
    assert "inside config directory" in absolute_result.warnings[0]


def test_activate_and_read_related_skill_file(tmp_path):
    skill_dir = _install_skill(tmp_path)
    (skill_dir / "guide.md").write_bytes(b"Reproduce first.\r\n")
    record = skills.scan_agent_skills(tmp_path, "skills").skills["debugging"]

    activated = skills.activate_skill(tmp_path, record)
    related = skills.read_agent_skill_file(
        tmp_path, "debugging", "guide.md", "skills"
    )

    assert activated["name"] == "debugging"
    assert activated["source"] == "managed"
    assert activated["source_path"] == str((tmp_path / "skills").resolve())
    assert activated["entry_path"] == "skills/debugging/SKILL.md"
    assert activated["content"] == "# Debugging\n\nFind root causes.\n"
    assert activated["bytes"] == (skill_dir / "SKILL.md").stat().st_size
    assert activated["related_files"] == ["guide.md"]
    assert related == {
        "name": "debugging",
        "source": "managed",
        "source_path": str((tmp_path / "skills").resolve()),
        "path": "guide.md",
        "content": "Reproduce first.\n",
        "bytes": len(b"Reproduce first.\r\n"),
    }


def test_code_humanizer_style_front_matter_and_large_body(tmp_path):
    description = (
        "Use when code was written by an AI coding agent and needs structural cleanup "
        "— or when asked to deslop, remove AI slop, or humanize code."
    )
    body = "\n".join(
        f"## Pattern {index}\nEvidence and remediation." for index in range(300)
    )
    content = (
        "---\n"
        "name: code-humanizer\n"
        "version: 0.1.0\n"
        f"description: {description}\n"
        "license: MIT\n"
        "compatibility: any-agent\n"
        "---\n\n"
        "# Code Humanizer\n\n"
        f"{body}\n"
    )
    _install_skill(tmp_path, "code-humanizer", content=content)

    result = skills.scan_agent_skills(tmp_path)
    record = result.skills["code-humanizer"]
    activated = skills.activate_skill(tmp_path, record)

    assert result.warnings == []
    assert record.description == description
    assert record.related_files == []
    assert activated["content"].startswith("---\nname: code-humanizer\n")
    assert activated["bytes"] > 10_000


def test_cloned_skill_ignores_vcs_and_cache_metadata(tmp_path):
    skill_dir = _install_skill(tmp_path, "cloned")
    (skill_dir / ".git" / "objects").mkdir(parents=True)
    (skill_dir / ".git" / "objects" / "pack.bin").write_bytes(b"\x00pack")
    (skill_dir / ".hg").mkdir()
    (skill_dir / ".hg" / "store").write_text("metadata", encoding="utf-8")
    (skill_dir / "__pycache__").mkdir()
    (skill_dir / "__pycache__" / "cache.pyc").write_bytes(b"\x00pyc")
    (skill_dir / "README.md").write_text(
        "# Installed skill\n", encoding="utf-8"
    )

    record = skills.scan_agent_skills(tmp_path).skills["cloned"]

    assert record.related_files == ["README.md"]
    assert not any(path.startswith(".git/") for path in record.related_files)
    assert not any(path.startswith(".hg/") for path in record.related_files)
    assert not any(
        path.startswith("__pycache__/") for path in record.related_files
    )


def test_read_related_file_rejects_entry_and_traversal(tmp_path):
    _install_skill(tmp_path)

    with pytest.raises(ValueError, match="activate_agent_skill"):
        skills.read_agent_skill_file(tmp_path, "debugging", "SKILL.md")
    with pytest.raises(ValueError, match="relative"):
        skills.read_agent_skill_file(tmp_path, "debugging", "../outside.md")
    with pytest.raises(ValueError, match="portable POSIX"):
        skills.read_agent_skill_file(tmp_path, "debugging", r"docs\guide.md")


def test_skill_name_and_file_path_validation():
    for value in (
        None,
        "",
        " name",
        "name ",
        ".",
        "..",
        "a/b",
        r"a\b",
        "x" * 256,
        "a\x01",
    ):
        with pytest.raises(ValueError):
            skills.validate_skill_name(value)  # type: ignore[arg-type]

    for value in (
        None,
        "",
        r"a\b",
        "a:b",
        "a\x01",
        "/a",
        "../a",
        ".",
        "a//b",
        "a/./b",
    ):
        with pytest.raises(ValueError):
            skills.validate_skill_file_path(value)  # type: ignore[arg-type]

    assert skills.validate_skill_name("合法-name") == "合法-name"
    assert skills.validate_skill_file_path("docs/guide.md") == Path(
        "docs/guide.md"
    )


def test_regular_file_reads_are_bounded_and_normalized(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    entry = root / "entry.md"
    entry.write_bytes(b"line\r\nnext\r")

    content, size, resolved = skills._open_regular_file(entry, 100)
    linked = root / "linked.md"
    _symlink_or_skip(linked, entry)
    linked_content, linked_size, linked_resolved = skills._open_regular_file(
        linked, 100
    )

    assert content == "line\nnext\n"
    assert size == len(b"line\r\nnext\r")
    assert resolved == entry.resolve()
    assert linked_content == content
    assert linked_size == size
    assert linked_resolved == entry.resolve()
    with pytest.raises(ValueError, match="maximum"):
        skills._open_regular_file(linked, 1)
    with pytest.raises(ValueError, match="readable regular"):
        skills._open_regular_file(root / "missing", 100)


def test_regular_file_read_rejects_target_identity_change(
    tmp_path, monkeypatch
):
    if os.name == "nt":
        pytest.skip("inode identity check is POSIX-only")
    entry = tmp_path / "entry.md"
    entry.write_text("content", encoding="utf-8")
    actual = entry.stat()
    if not actual.st_ino:
        pytest.skip("filesystem does not expose inode identity")

    class ChangedStat:
        st_mode = actual.st_mode
        st_size = actual.st_size
        st_dev = actual.st_dev
        st_ino = actual.st_ino + 1

    monkeypatch.setattr(skills.os, "fstat", lambda _descriptor: ChangedStat())

    with pytest.raises(ValueError, match="changed while it was being opened"):
        skills._open_regular_file(entry, 100)


def test_related_file_scan_honors_budgets(tmp_path):
    skill_dir = _install_skill(tmp_path)
    for name in ("a.txt", "b.txt"):
        (skill_dir / name).write_text(name, encoding="utf-8")

    result = skills.scan_agent_skills(
        tmp_path,
        max_related_files=1,
        max_scan_entries=100,
        max_path_bytes=100,
    )

    assert len(result.skills["debugging"].related_files) == 1
    assert any("truncated" in warning for warning in result.warnings)


def test_scan_agent_skills_reports_scandir_failure(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    original_scandir = os.scandir

    def fail_scandir(path):
        if Path(path) == skills_dir.resolve():
            raise OSError("racing directory")
        return original_scandir(path)

    monkeypatch.setattr(skills.os, "scandir", fail_scandir)

    result = skills.scan_agent_skills(tmp_path)

    assert result.skills == {}
    assert result.warnings == [
        "Could not scan skills directory skills: racing directory"
    ]
