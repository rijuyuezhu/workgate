"""Executor-managed Skill file authoring and safety regression tests."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.agent_bridge.skill_files import manage_skill_file
from workgate.agent_bridge.skill_management import manage_skill
from workgate.agent_bridge.skills import (
    activate_skill,
    read_agent_skill_file,
    scan_agent_skills,
)
from workgate.config.settings import Settings

ENTRY = "---\ndescription: Complete Skill bundle\n---\n# Instructions\n"


def make_skill(tmp_path):
    base = tmp_path / "agent"
    manage_skill(base, "create", name="review", content=ENTRY)
    return base


def test_author_and_activate_real_two_file_skill(tmp_path):
    base = make_skill(tmp_path)
    created = manage_skill_file(
        base,
        "create",
        name="review",
        path="examples/checklist.md",
        content="Check the diff.\n",
    )
    assert len(created["revision"]) == 64
    assert manage_skill_file(base, "list", name="review") == {
        "files": ["examples/checklist.md"]
    }
    got = manage_skill_file(
        base,
        "get",
        name="review",
        path="examples/checklist.md",
    )
    assert got == {**created, "content": "Check the diff.\n"}

    scan = scan_agent_skills(base)
    record = scan.skills["review"]
    assert record.related_files == ["examples/checklist.md"]
    activated = activate_skill(base, record)
    assert activated["content"] == ENTRY
    assert activated["related_files"] == ["examples/checklist.md"]
    related = read_agent_skill_file(base, "review", "examples/checklist.md")
    assert related["content"] == "Check the diff.\n"

    updated = manage_skill_file(
        base,
        "update",
        name="review",
        path="examples/checklist.md",
        content="Updated checklist.\n",
        expected_revision=got["revision"],
    )
    assert updated["revision"] != got["revision"]
    assert (
        read_agent_skill_file(
            base,
            "review",
            "examples/checklist.md",
        )["content"]
        == "Updated checklist.\n"
    )
    with pytest.raises(ValueError, match="changed"):
        manage_skill_file(
            base,
            "update",
            name="review",
            path="examples/checklist.md",
            content="Stale",
            expected_revision=got["revision"],
        )
    with pytest.raises(ValueError, match="changed"):
        manage_skill_file(
            base,
            "delete",
            name="review",
            path="examples/checklist.md",
            expected_revision=got["revision"],
        )
    manage_skill_file(
        base,
        "delete",
        name="review",
        path="examples/checklist.md",
        expected_revision=updated["revision"],
    )
    assert manage_skill_file(base, "list", name="review") == {"files": []}
    assert not (base / "skills/review/examples").exists()
    assert not manage_skill(base, "get", name="review")["skill"]["has_assets"]


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "/tmp/escape.md",
        "x/../../escape",
        "SKILL.md",
        ".env",
        ".git/config",
        "a//b",
        "a/../b",
        "a\\b",
        "C:/foo",
        "examples/.secret",
        "examples/__pycache__/a.py",
    ],
)
def test_reject_invalid_auxiliary_paths(tmp_path, path):
    base = make_skill(tmp_path)
    with pytest.raises(ValueError):
        manage_skill_file(
            base, "create", name="review", path=path, content="unsafe"
        )
    assert (base / "skills/review/SKILL.md").read_text() == ENTRY


def test_never_follow_auxiliary_symlinks_or_escape_root(tmp_path):
    base = make_skill(tmp_path)
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    root = base / "skills/review"
    (root / "references").symlink_to(tmp_path, target_is_directory=True)
    for action, fields in (
        ("get", {}),
        ("create", {"content": "overwrite"}),
    ):
        with pytest.raises((OSError, ValueError)):
            manage_skill_file(
                base,
                action,
                name="review",
                path="references/private.txt",
                **fields,
            )
    (root / "references").unlink()
    (root / "linked.txt").symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        manage_skill_file(base, "get", name="review", path="linked.txt")
    with pytest.raises((OSError, ValueError)):
        manage_skill_file(
            base, "create", name="review", path="linked.txt", content="x"
        )
    assert outside.read_text() == "private"


def test_auxiliary_limits_and_revisions(tmp_path):
    base = make_skill(tmp_path)
    with pytest.raises(ValueError, match="128 KiB"):
        manage_skill_file(
            base,
            "create",
            name="review",
            path="large.txt",
            content="é" * (128 * 1024),
        )
    first = manage_skill_file(
        base,
        "create",
        name="review",
        path="script.py",
        content="print('ok')\n",
    )
    assert (base / "skills/review/script.py").stat().st_mode & 0o777 == 0o600
    assert (base / "skills/review/script.py").read_text() == "print('ok')\n"
    with pytest.raises(ValueError, match="exists"):
        manage_skill_file(
            base, "create", name="review", path="script.py", content="replace"
        )
    with pytest.raises(ValueError, match="revision"):
        manage_skill_file(
            base, "update", name="review", path="script.py", content="x"
        )
    with pytest.raises(ValueError):
        manage_skill_file(
            base, "create", name="review", path="binary.dat", content="a\x00b"
        )
    assert (
        manage_skill_file(base, "get", name="review", path="script.py")[
            "revision"
        ]
        == first["revision"]
    )


def test_concurrent_updates_use_same_manifest_lock(tmp_path):
    base = make_skill(tmp_path)
    created = manage_skill_file(
        base, "create", name="review", path="a.md", content="first"
    )

    def edit(value):
        try:
            return manage_skill_file(
                base,
                "update",
                name="review",
                path="a.md",
                content=value,
                expected_revision=created["revision"],
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["version A", "version B"]))
    assert sum(result is not None for result in results) == 1
    assert manage_skill_file(base, "get", name="review", path="a.md")[
        "content"
    ] in {"version A", "version B"}


def test_executor_skill_bundle_file_http_and_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    workdir = tmp_path / "project"
    workdir.mkdir()
    app, harness = build_paired_http_app(
        Settings(
            default_workdir=workdir,
            state_dir=tmp_path / "state",
            agent_bridge_enabled=True,
            auth_mode="none",
            base_url="https://workgate.example",
        )
    )
    manage_skill(
        harness.executor.config.agent_config_dir,
        "create",
        name="review",
        content=ENTRY,
    )
    with TestClient(app, base_url="https://workgate.example") as client:
        base = {"executor_id": harness.executor_id, "name": "review"}

        def read(action="list", path=None):
            return client.get(
                "/api/ui/integrations/skill-files",
                params={
                    **base,
                    "action": action,
                    **({"path": path} if path else {}),
                },
            )

        def write(action, path, **fields):
            return client.post(
                "/api/ui/integrations/skill-files",
                json={
                    **base,
                    "action": action,
                    "path": path,
                    **fields,
                },
            )

        assert read().json()["data"] == {"files": []}
        created = write(
            "create", "examples/checklist.md", content="Checklist!\n"
        )
        assert created.status_code == 200, created.text
        assert read().json()["data"]["files"] == ["examples/checklist.md"]
        detail = read("get", "examples/checklist.md")
        assert detail.status_code == 200, detail.text
        before = detail.json()["data"]
        assert before["content"] == "Checklist!\n"
        started = client.post(
            "/tools/session_start",
            json={
                "workdir": str(workdir),
                "executor_id": harness.executor_id,
            },
        )
        assert started.status_code == 200, started.text
        sid = started.json()["structuredContent"]["session_id"]
        activated = client.post(
            "/tools/activate_agent_skill",
            json={
                "session_id": sid,
                "name": "review",
            },
        )
        assert activated.status_code == 200, activated.text
        assert not activated.json().get("isError"), activated.text
        assert (
            "examples/checklist.md"
            in activated.json()["structuredContent"]["related_files"]
        )
        related = client.post(
            "/tools/read_agent_skill_file",
            json={
                "session_id": sid,
                "name": "review",
                "path": "examples/checklist.md",
            },
        )
        assert related.status_code == 200, related.text
        assert not related.json().get("isError"), related.text
        assert related.json()["structuredContent"]["content"] == "Checklist!\n"
        assert (
            write(
                "update",
                "examples/checklist.md",
                content="new",
                expected_revision="0" * 64,
            ).status_code
            == 409
        )
        new = write(
            "update",
            "examples/checklist.md",
            content="new",
            expected_revision=before["revision"],
        )
        assert new.status_code == 200, new.text
        assert (
            read("get", "examples/checklist.md").json()["data"]["content"]
            == "new"
        )
        assert (
            write("create", "../escaped.txt", content="no").status_code == 409
        )
        assert not (tmp_path / "escaped.txt").exists()
        assert (
            write(
                "delete",
                "examples/checklist.md",
                expected_revision=new.json()["data"]["revision"],
            ).status_code
            == 200
        )
        assert read().json()["data"]["files"] == []

        async def offline(_id):
            return False

        monkeypatch.setattr(
            harness.control.executor_transport, "is_online", offline
        )
        assert read().status_code == 503
        assert write("create", "foo.txt", content="no").status_code == 503
        # An Executor-managed file request must never mutate a Control source.
        assert not (
            harness.control.config.agent_config_dir / "skills/review/foo.txt"
        ).exists()


def test_reject_symlinked_config_root_and_non_utf8_file(tmp_path):
    base = make_skill(tmp_path)
    alias = tmp_path / "agent-alias"
    alias.symlink_to(base, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlinked"):
        manage_skill_file(alias, "list", name="review")
    with pytest.raises(ValueError, match="Symlinked"):
        manage_skill_file(
            alias, "create", name="review", path="unsafe.txt", content="x"
        )
    assert not (base / "skills/review/unsafe.txt").exists()
    (base / "skills/review/raw.bin").write_bytes(b"\xff\x00binary")
    with pytest.raises(ValueError, match="UTF-8"):
        manage_skill_file(base, "get", name="review", path="raw.bin")


def test_skill_file_http_rejects_missing_write_scope(tmp_path, monkeypatch):
    from workgate.control.http import executor_admin
    from workgate.oauth.core.context import MissingOAuthScopeError

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    app, harness = build_paired_http_app(
        Settings(
            default_workdir=workdir,
            state_dir=tmp_path / "state",
            auth_mode="none",
            base_url="https://workgate.example",
        )
    )
    manage_skill(
        harness.executor.config.agent_config_dir,
        "create",
        name="review",
        content=ENTRY,
    )

    def denied(_scopes):
        raise MissingOAuthScopeError("shell:write")

    monkeypatch.setattr(executor_admin, "require_oauth_scopes", denied)
    with TestClient(app, base_url="https://workgate.example") as client:
        args = {"executor_id": harness.executor_id, "name": "review"}
        assert (
            client.get(
                "/api/ui/integrations/skill-files",
                params={
                    **args,
                    "action": "list",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/ui/integrations/skill-files",
                json={
                    **args,
                    "action": "create",
                    "path": "private.md",
                    "content": "x",
                },
            ).status_code
            == 403
        )
        assert not (
            harness.executor.config.agent_config_dir
            / "skills/review/private.md"
        ).exists()


def test_custom_nested_managed_root_stays_scoped(tmp_path):
    base = tmp_path / "agent"
    base.mkdir()
    (base / "config.json").write_text(
        '{"version":1,"skills":{"directory":"managed/skill-bundles"}}',
        encoding="utf-8",
    )
    manage_skill(base, "create", name="review", content=ENTRY)
    result = manage_skill_file(
        base,
        "create",
        name="review",
        path="scripts/verify.py",
        content="print('ok')\n",
    )
    assert result["path"] == "scripts/verify.py"
    assert (base / "managed/skill-bundles/review/scripts/verify.py").is_file()
    assert manage_skill_file(base, "list", name="review")["files"] == [
        "scripts/verify.py"
    ]


def test_auxiliary_file_scan_limit(tmp_path, monkeypatch):
    from workgate.agent_bridge import skill_files

    base = make_skill(tmp_path)
    manage_skill_file(base, "create", name="review", path="one.md", content="1")
    manage_skill_file(base, "create", name="review", path="two.md", content="2")
    monkeypatch.setattr(skill_files, "MAX_AUXILIARY_FILES", 1)
    with pytest.raises(ValueError, match="Too many"):
        manage_skill_file(base, "list", name="review")


def test_atomic_publication_never_scans_temporary_files(tmp_path, monkeypatch):
    from workgate.agent_bridge import skill_files

    base = make_skill(tmp_path)
    published = []
    original_link = skill_files.os.link
    original_replace = skill_files.os.replace

    def assert_clean_scan():
        related = scan_agent_skills(base).skills["review"].related_files
        assert not any(".writing-" in name for name in related)
        published.append(related)

    def checked_link(*args, **kwargs):
        assert_clean_scan()
        return original_link(*args, **kwargs)

    def checked_replace(*args, **kwargs):
        assert_clean_scan()
        return original_replace(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(skill_files.os, "link", checked_link)
        patch.setattr(skill_files.os, "replace", checked_replace)
        created = manage_skill_file(
            base,
            "create",
            name="review",
            path="references/check.md",
            content="Initial",
        )
        manage_skill_file(
            base,
            "update",
            name="review",
            path="references/check.md",
            content="Updated",
            expected_revision=created["revision"],
        )
    assert published == [[], ["references/check.md"]]
    assert not any(
        file.name.startswith(".writing-")
        for file in (base / "skills").iterdir()
    )
