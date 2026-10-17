"""Control Skills are mutable on Control even without an online Executor."""

from dataclasses import replace

from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.config.settings import Settings

BASE = "https://workgate.example"
CONTENT = "---\ndescription: Control global helper\n---\n# Instructions\n"


def test_control_skill_crud_without_online_executor(tmp_path, monkeypatch):
    workdir = tmp_path / "project"
    workdir.mkdir()
    app, harness = build_paired_http_app(
        Settings(
            default_workdir=workdir,
            state_dir=tmp_path / "state",
            auth_mode="none",
            base_url=BASE,
        )
    )
    harness.control.config = replace(
        harness.control.config,
        agent_config_dir=tmp_path / "control-skill-source",
    )

    async def offline(_executor_id):
        return False

    monkeypatch.setattr(
        harness.control.executor_transport, "is_online", offline
    )
    with TestClient(app, base_url=BASE) as client:

        def read(action="list", name=""):
            return client.get(
                "/api/ui/integrations",
                params={
                    "owner": "control",
                    "kind": "skill",
                    "action": action,
                    "name": name,
                },
            )

        def write(action, name, **fields):
            return client.post(
                "/api/ui/integrations",
                json={
                    "owner": "control",
                    "kind": "skill",
                    "action": action,
                    "name": name,
                    **fields,
                },
            )

        assert read().status_code == 200
        assert read().json()["data"]["skills"] == []
        assert write("validate", "standalone", content=CONTENT).json()["data"][
            "valid"
        ]
        created = write("create", "standalone", content=CONTENT)
        assert created.status_code == 200, created.text
        assert created.json()["data"]["skill"]["enabled"]
        first = read("get", "standalone").json()["data"]["skill"]
        assert first["content"] == CONTENT
        updated = write(
            "update",
            "standalone",
            content=CONTENT + "More.\n",
            expected_revision=first["revision"],
        )
        assert updated.status_code == 200, updated.text
        assert (
            write(
                "disable", "standalone", expected_revision=first["revision"]
            ).status_code
            == 409
        )
        next_revision = updated.json()["data"]["skill"]["revision"]
        disabled = write(
            "disable", "standalone", expected_revision=next_revision
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["data"]["skill"]["enabled"] is False
        revision = disabled.json()["data"]["skill"]["revision"]
        assert (
            write(
                "enable", "standalone", expected_revision=revision
            ).status_code
            == 200
        )
        latest = read("get", "standalone").json()["data"]["skill"]
        assert (
            write(
                "delete", "standalone", expected_revision=latest["revision"]
            ).status_code
            == 200
        )
        assert read().json()["data"]["skills"] == []
