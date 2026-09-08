from __future__ import annotations

from typing import Any, cast

import pytest

import workgate.agent_bridge.service as agent_service
import workgate.ops.agent as agent_ops
from workgate.agent_bridge.models import AgentCapabilityRegistry
from workgate.schemas.result_models.agent import (
    ActivateAgentSkillOutput,
    ListAgentSkillsOutput,
    ReadAgentSkillFileOutput,
)


def test_agent_skill_ops_use_injected_registry_without_local_scan(
    monkeypatch,
) -> None:
    registry = cast(AgentCapabilityRegistry, object())
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    listed = ListAgentSkillsOutput(sources=[], skills=[], warnings=[])
    activated = ActivateAgentSkillOutput(
        name="skill-a",
        source="managed",
        source_path="/skills",
        entry_path="skill-a/SKILL.md",
        description="desc",
        content="instructions",
        bytes=12,
        related_files=[],
    )

    def list_payload(value: object):
        calls.append(("list", (value,), {}))
        return listed

    def activate_payload(value: object, name: str):
        calls.append(("activate", (value, name), {}))
        return activated

    def read_payload(value: object, name: str, path: str, **kwargs: Any):
        calls.append(("read", (value, name, path), kwargs))
        return {
            "name": name,
            "source": "managed",
            "source_path": "/skills",
            "path": path,
            "content": "notes",
            "bytes": 5,
        }

    monkeypatch.setattr(
        agent_service, "list_agent_skills_payload", list_payload
    )
    monkeypatch.setattr(
        agent_service, "activate_agent_skill_payload", activate_payload
    )
    monkeypatch.setattr(
        agent_service, "read_agent_skill_file_payload", read_payload
    )

    assert agent_ops.list_agent_skills_execute(registry=registry) == listed
    assert (
        agent_ops.activate_agent_skill_execute("skill-a", registry=registry)
        == activated
    )
    read = agent_ops.read_agent_skill_file_execute(
        "skill-a", "notes.md", registry=registry
    )
    assert isinstance(read, ReadAgentSkillFileOutput)
    assert read.content == "notes"
    assert calls[0] == ("list", (registry,), {})
    assert calls[1] == ("activate", (registry, "skill-a"), {})
    assert calls[2][0:2] == ("read", (registry, "skill-a", "notes.md"))
    assert calls[2][2]["max_file_bytes"] > 0


@pytest.mark.asyncio
async def test_agent_skill_dispatch_with_registry_stays_control_local(
    monkeypatch,
) -> None:
    registry = cast(AgentCapabilityRegistry, object())
    calls: list[tuple[str, tuple[Any, ...]]] = []
    listed = ListAgentSkillsOutput(sources=[], skills=[], warnings=[])
    activated = ActivateAgentSkillOutput(
        name="skill-a",
        source="managed",
        source_path="/skills",
        entry_path="skill-a/SKILL.md",
        description="desc",
        content="instructions",
        bytes=12,
        related_files=[],
    )
    read = ReadAgentSkillFileOutput(
        name="skill-a",
        source="managed",
        source_path="/skills",
        path="notes.md",
        content="notes",
        bytes=5,
    )

    def list_local(session_id=None, registry=None):
        calls.append(("list", (session_id, registry)))
        return listed

    def activate_local(name, session_id=None, registry=None):
        calls.append(("activate", (name, session_id, registry)))
        return activated

    def read_local(name, path, session_id=None, registry=None):
        calls.append(("read", (name, path, session_id, registry)))
        return read

    monkeypatch.setattr(agent_ops, "list_agent_skills_execute", list_local)
    monkeypatch.setattr(
        agent_ops, "activate_agent_skill_execute", activate_local
    )
    monkeypatch.setattr(agent_ops, "read_agent_skill_file_execute", read_local)

    assert (
        await agent_ops.list_agent_skills_dispatch_execute(
            "sess_unused", registry
        )
        == listed
    )
    assert (
        await agent_ops.activate_agent_skill_dispatch_execute(
            "skill-a", "sess_unused", registry
        )
        == activated
    )
    assert (
        await agent_ops.read_agent_skill_file_dispatch_execute(
            "skill-a", "notes.md", "sess_unused", registry
        )
        == read
    )
    assert calls == [
        ("list", ("sess_unused", registry)),
        ("activate", ("skill-a", "sess_unused", registry)),
        ("read", ("skill-a", "notes.md", "sess_unused", registry)),
    ]
