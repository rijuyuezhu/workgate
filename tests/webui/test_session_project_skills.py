"""WebUI Session Skills are effective, read-only and scoped to Session identity."""

from dataclasses import replace

from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.app_paths import app_paths
from workgate.config.settings import Settings

BASE = "https://workgate.example"


def install(root, name, description="Private source location"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )


def test_session_skills_resolve_per_project_without_private_paths(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    projects = [tmp_path / "project-a", tmp_path / "project-b"]
    for root in projects:
        root.mkdir()
    install(projects[0] / ".agents" / "skills", "shared")
    install(projects[0] / ".agents" / "skills", "alpha")
    install(projects[1] / ".agents" / "skills", "shared")
    install(projects[1] / ".agents" / "skills", "beta")
    install(app_paths().executor_agent_config_dir / "skills", "shared")
    install(app_paths().executor_agent_config_dir / "skills", "managed-only")
    install(tmp_path / "xdg" / "agents" / "skills", "global-only")
    install(tmp_path / "control-agent" / "skills", "control-only")
    app, harness = build_paired_http_app(
        Settings(
            default_workdir=projects[0],
            state_dir=tmp_path / "state",
            agent_bridge_enabled=True,
            auth_mode="none",
            base_url=BASE,
        )
    )
    harness.control.config = replace(
        harness.control.config, agent_config_dir=tmp_path / "control-agent"
    )
    harness.control.agent_bridge._settings = harness.control.config

    with TestClient(app, base_url=BASE) as client:
        ids = []
        for project in projects:
            result = client.post(
                "/tools/session_start",
                json={
                    "workdir": str(project),
                    "executor_id": harness.executor_id,
                },
            )
            assert result.status_code == 200, result.text
            ids.append(result.json()["structuredContent"]["session_id"])

        for sid, unique in zip(ids, ("alpha", "beta"), strict=True):
            response = client.get(
                "/api/ui/sessions/skills", params={"session_id": sid}
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["session_id"] == sid
            by_name = {row["name"]: row for row in data["skills"]}
            assert set(by_name) == {
                "shared",
                unique,
                "managed-only",
                "global-only",
                "control-only",
            }
            assert {name: row["source"] for name, row in by_name.items()} == {
                "shared": "project",
                unique: "project",
                "managed-only": "managed",
                "global-only": "global",
                "control-only": "control",
            }
            assert by_name["shared"]["priority"] == 1
            assert by_name["control-only"]["priority"] == 4
            assert not any(
                key in row
                for row in data["skills"]
                for key in (
                    "source_path",
                    "entry_path",
                    "description",
                    "related_files",
                    "content",
                )
            )
            assert str(tmp_path) not in response.text

        assert (
            client.get(
                "/api/ui/sessions/skills", params={"session_id": "unknown"}
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/ui/sessions/skills", params={"session_id": "x" * 256}
            ).status_code
            == 400
        )

        async def offline(executor_id: str) -> bool:
            return False

        monkeypatch.setattr(
            harness.control.executor_transport, "is_online", offline
        )
        assert (
            client.get(
                "/api/ui/sessions/skills", params={"session_id": ids[0]}
            ).status_code
            == 503
        )
        # Read-only and rejected state must not resolve a stale Session directory.
        harness.control.control_state.update_session(ids[1], status="ended")
        assert (
            client.get(
                "/api/ui/sessions/skills", params={"session_id": ids[1]}
            ).status_code
            == 409
        )


def test_session_skills_requires_read_scope(monkeypatch, tmp_path):
    from fastapi import HTTPException

    import workgate.ui.http.sessions as routes

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    app, _harness = build_paired_http_app(
        Settings(
            default_workdir=workdir,
            state_dir=tmp_path / "state",
            auth_mode="none",
            base_url=BASE,
        )
    )

    def forbid(*, write=False):
        raise HTTPException(status_code=403, detail="read scope missing")

    monkeypatch.setattr(routes, "_require_scopes", forbid)
    with TestClient(app, base_url=BASE) as client:
        assert (
            client.get(
                "/api/ui/sessions/skills", params={"session_id": "a"}
            ).status_code
            == 403
        )
