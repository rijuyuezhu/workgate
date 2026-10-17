"""Executor-managed Skill CRUD, scoped fallback, path safety and CAS."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from workgate.agent_bridge.skill_management import manage_skill
from workgate.agent_bridge.sources import scan_skill_sources, skill_sources
from workgate.agent_bridge.state import load_agent_manifest

ENTRY = "---\ndescription: Secure managed test\n---\n# Test skill\nSome text.\n"


def test_skill_crud_enable_disable_and_stale_revision(tmp_path):
    base = tmp_path / "agent"
    assert manage_skill(base, "list")["skills"] == []
    assert manage_skill(base, "validate", name="notes", content=ENTRY)["valid"]
    created = manage_skill(base, "create", name="notes", content=ENTRY)["skill"]
    assert created["source"] == "managed" and created["enabled"]
    assert created["content"] == ENTRY
    assert (base / "skills/notes/SKILL.md").read_text() == ENTRY
    assert "content" not in manage_skill(base, "list")["skills"][0]
    revised = manage_skill(
        base,
        "update",
        name="notes",
        content=ENTRY + "more\n",
        expected_revision=created["revision"],
    )["skill"]
    with pytest.raises(ValueError, match="changed"):
        manage_skill(
            base,
            "update",
            name="notes",
            content="stale",
            expected_revision=created["revision"],
        )
    disabled = manage_skill(
        base, "disable", name="notes", expected_revision=revised["revision"]
    )["skill"]
    assert not disabled["enabled"] and not disabled["configured_enabled"]
    assert json.loads((base / "config.json").read_text())["skills"][
        "disabled"
    ] == ["notes"]
    enabled = manage_skill(
        base, "enable", name="notes", expected_revision=disabled["revision"]
    )["skill"]
    assert enabled["enabled"]
    assert (
        manage_skill(
            base, "delete", name="notes", expected_revision=enabled["revision"]
        )["deleted"]
        == "notes"
    )
    assert not (base / "skills/notes").exists()


def test_managed_disabled_falls_through_to_global(tmp_path, monkeypatch):
    base = tmp_path / "agent"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    manage_skill(base, "create", name="same", content=ENTRY)
    global_root = tmp_path / "xdg/agents/skills/same"
    global_root.mkdir(parents=True)
    (global_root / "SKILL.md").write_text("# Global same\n")

    def source():
        manifest = load_agent_manifest(base)
        scan = scan_skill_sources(
            skill_sources(
                project_root=tmp_path,
                managed_config_dir=base,
                managed_directory="skills",
                include_project=False,
            ),
            max_skills=8,
            max_related_files=4,
            max_scan_entries=64,
            max_path_bytes=65536,
            max_entry_bytes=65536,
            disabled_managed=frozenset(manifest.data.skills.disabled),
        )
        return scan.skills["same"].source

    assert source() == "managed"
    rev = manage_skill(base, "get", name="same")["skill"]["revision"]
    manage_skill(base, "disable", name="same", expected_revision=rev)
    assert source() == "global"


def test_reject_path_escape_symlink_and_invalid_frontmatter(tmp_path):
    base = tmp_path / "agent"
    for name in ("../outside", "/root", "bad/name", " ", "é" * 150):
        with pytest.raises(ValueError):
            manage_skill(base, "create", name=name, content=ENTRY)
    for content in (
        "",
        "   ",
        "x" * (128 * 1024 + 1),
        "---\nunclosed",
        "---\ninvalid: [\n---\n",
    ):
        with pytest.raises(ValueError):
            manage_skill(base, "validate", name="notes", content=content)
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "skills").mkdir()
    (base / "skills/trap").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        manage_skill(base, "get", name="trap")
    notes = manage_skill(base, "create", name="notes", content=ENTRY)["skill"]
    (base / "skills/notes/SKILL.md").unlink()
    (base / "skills/notes/SKILL.md").symlink_to(outside / "SKILL.md")
    with pytest.raises(ValueError):
        manage_skill(
            base,
            "update",
            name="notes",
            content=ENTRY,
            expected_revision=notes["revision"],
        )


def test_concurrent_cas_and_related_files_preserved(tmp_path):
    base = tmp_path / "agent"
    item = manage_skill(base, "create", name="notes", content=ENTRY)["skill"]
    related = base / "skills/notes/docs"
    related.mkdir()
    (related / "reference.txt").write_text("retain me")

    def change(label):
        try:
            manage_skill(
                base,
                "update",
                name="notes",
                content=ENTRY + label,
                expected_revision=item["revision"],
            )
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(change, ["left", "right"])) == 1
    assert (related / "reference.txt").read_text() == "retain me"
    current = manage_skill(base, "get", name="notes")["skill"]
    (related / "out").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        manage_skill(
            base, "delete", name="notes", expected_revision=current["revision"]
        )
    assert (base / "skills/notes/SKILL.md").exists()


def test_invalid_management_arguments_and_config_path(tmp_path):
    base = tmp_path / "agent"
    bad = [
        ("unknown", {}),
        ("get", {}),
        ("create", {"name": "x"}),
        ("list", {"content": "hello"}),
        ("update", {"name": "x", "content": "body"}),
        ("get", {"name": "x", "expected_revision": "old"}),
        ("create", {"name": ".creating-abc", "content": ENTRY}),
    ]
    for action, kwargs in bad:
        with pytest.raises(ValueError):
            manage_skill(base, action, **kwargs)
    assert manage_skill(
        base,
        "validate",
        name="v",
        content="+++\ndescription = 'hello'\n+++\nText",
    )["valid"]
    for invalid in ("+++\nunknown = [\n+++", "---\n- not mapping\n---"):
        with pytest.raises(ValueError):
            manage_skill(base, "validate", name="v", content=invalid)
    base.mkdir()
    (base / "config.json").write_text(
        '{"version": 1, "skills":{"directory":"../unsafe"}}'
    )
    with pytest.raises(ValueError, match="Unsafe"):
        manage_skill(base, "list")
    (base / "config.json").write_text('{"version": 2}')
    with pytest.raises(ValueError, match="Invalid Agent Bridge"):
        manage_skill(base, "list")
    (base / "config.json").unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlinked"):
        manage_skill(linked, "list")


def test_invalid_directory_is_reported_and_limit_enforced(
    tmp_path, monkeypatch
):
    from workgate.agent_bridge import skill_management

    base = tmp_path / "agent"
    item = manage_skill(base, "create", name="one", content=ENTRY)["skill"]
    (base / "skills/corrupt").mkdir()
    listing = manage_skill(base, "list")
    assert listing["invalid_entries"] == 1
    assert len(listing["skills"]) == 1
    with pytest.raises(ValueError, match="already exists"):
        manage_skill(base, "create", name="one", content=ENTRY)
    monkeypatch.setattr(skill_management, "MAX_MANAGED_SKILLS", 1)
    with pytest.raises(ValueError, match="limit"):
        manage_skill(base, "create", name="two", content=ENTRY)
    assert manage_skill(base, "list")["truncated"] is True
    with pytest.raises(ValueError, match="Unknown"):
        manage_skill(base, "get", name="missing")
    with pytest.raises(ValueError, match="Expected revision"):
        manage_skill(base, "delete", name="one")
    with pytest.raises(ValueError, match="SKILL.md"):
        (base / "skills/one/SKILL.md").write_bytes(b"A" * (128 * 1024 + 1))
        manage_skill(base, "get", name="one")
    assert item["name"] == "one"


def test_delete_disabled_skill_rolls_back_on_manifest_write_error(
    tmp_path, monkeypatch
):
    from workgate.agent_bridge import skill_management

    base = tmp_path / "agent"
    created = manage_skill(base, "create", name="notes", content=ENTRY)["skill"]
    disabled = manage_skill(
        base, "disable", name="notes", expected_revision=created["revision"]
    )["skill"]
    writer = skill_management.atomic_write_private_text

    def broken(path, payload):
        if path.name == "config.json":
            raise OSError("storage fail")
        return writer(path, payload)

    monkeypatch.setattr(skill_management, "atomic_write_private_text", broken)
    with pytest.raises(OSError, match="storage fail"):
        manage_skill(
            base, "delete", name="notes", expected_revision=disabled["revision"]
        )
    assert (base / "skills/notes/SKILL.md").exists()
    assert manage_skill(base, "get", name="notes")["skill"]["enabled"] is False
    monkeypatch.setattr(skill_management, "atomic_write_private_text", writer)
    manage_skill(
        base, "delete", name="notes", expected_revision=disabled["revision"]
    )
    assert not (base / "skills/notes").exists()
    assert not json.loads((base / "config.json").read_text())["skills"][
        "disabled"
    ]
