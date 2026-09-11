import subprocess
from pathlib import Path

import pytest

from tests.e2e_helpers import RestToolClient, run_http_process

pytestmark = pytest.mark.integration

CODE_HUMANIZER_REPOSITORY = "https://github.com/LeonardNJU/code-humanizer.git"
CODE_HUMANIZER_COMMIT = "a315560f58054d091f0df36dc4ed3bf8364f25e2"


def _install_code_humanizer(workspace: Path) -> None:
    skills_dir = workspace / ".agents" / "skills"
    skill_dir = skills_dir / "code-humanizer"
    skills_dir.mkdir(parents=True)

    subprocess.run(
        ["git", "init", "-q", str(skill_dir)], check=True, timeout=10
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(skill_dir),
            "remote",
            "add",
            "origin",
            CODE_HUMANIZER_REPOSITORY,
        ],
        check=True,
        timeout=10,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(skill_dir),
            "fetch",
            "-q",
            "--depth",
            "1",
            "origin",
            CODE_HUMANIZER_COMMIT,
        ],
        check=True,
        timeout=30,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(skill_dir),
            "checkout",
            "-q",
            "--detach",
            "FETCH_HEAD",
        ],
        check=True,
        timeout=10,
    )
    installed_commit = subprocess.run(
        ["git", "-C", str(skill_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    assert installed_commit == CODE_HUMANIZER_COMMIT


@pytest.mark.asyncio
async def test_real_code_humanizer_skill_installation_over_http(tmp_path):
    async with run_http_process(
        tmp_path,
        mode="http",
        agent_bridge_enabled=True,
        workspace_setup=_install_code_humanizer,
    ) as (base_url, _workspace):
        client = RestToolClient(base_url)

        status = await client.call_tool("agent_config_status")
        assert status["skills"]["count"] == 0
        assert status["skills"]["warnings"] == []
        session = await client.call_tool("session_start", {"workdir": "."})
        session_id = session["session_id"]

        listed = await client.call_tool(
            "list_agent_skills", {"session_id": session_id}
        )
        assert listed["warnings"] == []
        assert len(listed["skills"]) == 1
        skill = listed["skills"][0]
        assert skill["name"] == "code-humanizer"
        assert skill["source"] == "project"
        assert skill["entry_path"] == ".agents/skills/code-humanizer/SKILL.md"
        assert "humanize code" in skill["description"]
        assert "README.md" in skill["related_files"]
        assert not any(
            path == ".git" or path.startswith(".git/")
            for path in skill["related_files"]
        )

        activated = await client.call_tool(
            "activate_agent_skill",
            {"session_id": session_id, "name": "code-humanizer"},
        )
        assert activated["name"] == "code-humanizer"
        assert activated["source"] == "project"
        assert activated["bytes"] > 10_000
        assert activated["content"].startswith("---\nname: code-humanizer\n")
        assert "## Iron rules" in activated["content"]
        assert activated["related_files"] == skill["related_files"]

        readme = await client.call_tool(
            "read_agent_skill_file",
            {
                "session_id": session_id,
                "name": "code-humanizer",
                "path": "README.md",
            },
        )
        assert readme["name"] == "code-humanizer"
        assert readme["path"] == "README.md"
        assert readme["bytes"] > 1_000
        assert readme["content"].startswith("# code-humanizer\n")
