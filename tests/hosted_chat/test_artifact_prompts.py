"""Pinned upstream Artifact mode generation and Python SQLite runtime integration."""

import hashlib
from pathlib import Path

import pytest

from workgate.hosted_chat.artifact_prompts import (
    _source,
    agent_instructions,
    artifact_mode,
    generate_artifacts_prompt,
)

PINNED = "e8f3be08623663d4ad7f7241e693c94469b63bb0"
# SHA-256 of the original TypeScript functions executed with upstream dedent 1.7.0.
ORIGINAL_NODE_GOLDENS = {
    "openai|default": "601eb7f4def11e76f0cc8e640d8b874ca1c7123264b501c9e327530892020bc8",
    "openai|shadcnui": "74086a840cbcfde186777b93e9b271bd1e994cf3f5a9e55ea12b1805841224eb",
    "anthropic|default": "d966d0510c64b20fc00a390c2a157e41f4e054a51ea315f804572780874b596f",
    "anthropic|shadcnui": "e074ac1d39b0f4582d9b7521c75379fdd1565b38829447af0197f12cfc086062",
}
UPSTREAM = Path("/root/librechat-reference-e8f3be08")


def test_snapshot_matches_source_hashes_when_upstream_checkout_present():
    data = _source()
    assert data["sourceCommit"] == PINNED
    assert len(data["components"]) == 33
    assert data["prompts"]["openai"].startswith(
        "The assistant can create and reference artifacts during conversations."
    )
    assert set(data["sourceSha256"]) == {
        "packages/api/src/prompts/artifacts/index.ts",
        "packages/api/src/prompts/artifacts/components.ts",
    }
    for path, digest in data["sourceSha256"].items():
        assert len(digest) == 64
        try:
            upstream_bytes = (UPSTREAM / path).read_bytes()
        except FileNotFoundError, NotADirectoryError, PermissionError:
            # The source checkout is optional and can exist but be unreadable
            # inside Linux CI runners (e.g. /root owned by another user).
            continue
        assert hashlib.sha256(upstream_bytes).hexdigest() == digest


@pytest.mark.parametrize(
    "endpoint", ["openai", "anthropic", "google", "openai-compatible"]
)
@pytest.mark.parametrize(
    "mode", ["default", "shadcnui", "custom", "", None, "unknown"]
)
def test_original_artifact_modes(endpoint, mode):
    generated = generate_artifacts_prompt(endpoint, mode)
    if mode == "custom":
        assert generated is None
    else:
        assert generated is not None
        source = _source()["prompts"][
            "anthropic" if endpoint == "anthropic" else "openai"
        ]
        assert generated.startswith(source)
        if mode == "shadcnui":
            assert (
                "## Additional Artifact Instructions for React Components"
                in generated
            )
            assert all(
                item["name"] in generated for item in _source()["components"]
            )
            assert (
                "<component>" in generated
                if endpoint == "anthropic"
                else "# Avatar" in generated
            )
        else:
            assert generated == source
    key = f"{endpoint}|{mode}"
    if key in ORIGINAL_NODE_GOLDENS:
        assert generated is not None
        assert (
            hashlib.sha256(generated.encode()).hexdigest()
            == ORIGINAL_NODE_GOLDENS[key]
        )


def test_prompt_state_and_delegation_instructions():
    saved = {
        "instructions": "System",
        "additional_instructions": "Other",
        "artifacts": "default",
    }
    assert artifact_mode({}, saved) == "default"
    assert (
        artifact_mode({"ephemeralAgent": {"artifacts": "custom"}}, saved)
        == "custom"
    )
    assert artifact_mode({"ephemeralAgent": {"artifacts": ""}}, saved) == ""
    assert (
        artifact_mode({"ephemeralAgent": {"artifacts": "shadcnui"}}, None)
        == "shadcnui"
    )
    assert agent_instructions(saved, "anthropic") == (
        "System\n\nOther\n\n" + _source()["prompts"]["anthropic"]
    )
    assert agent_instructions(saved, "anthropic", "") == "System\n\nOther"
    assert agent_instructions(None, "openai") == ""
    assert agent_instructions({"artifacts": ""}, "openai") == ""
    assert "application/vnd.react" in agent_instructions(
        saved, "openai", "shadcnui"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "saved,ephemeral,expected",
    [
        ("default", None, "default"),
        ("default", "", ""),
        ("default", "custom", "custom"),
        ("custom", "shadcnui", "shadcnui"),
        (None, "default", "default"),
        (None, None, None),
    ],
)
async def test_admitted_mode_and_history_are_stable(
    tmp_path, saved, ephemeral, expected
):
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.database import SQLiteDatabase
    from workgate.hosted_chat.generation_context import (
        GenerationContextResolver,
    )
    from workgate.hosted_chat.generations import GenerationStore

    db = SQLiteDatabase(tmp_path)
    resolver = GenerationContextResolver(db, "alice")
    resolver.providers.save(
        {
            "name": "named",
            "provider": "anthropic",
            "base_url": "https://models.invalid/v1",
            "api_key": "secret",
            "models": ["m"],
        }
    )
    payload: dict = {
        "text": "Make an artifact",
        "provider": "named",
        "model": "m",
        "clientRequestId": "turn",
    }
    agent = None
    if saved is not None:
        agent = AgentRepository(db).create(
            "alice",
            {
                "provider": "named",
                "model": "m",
                "artifacts": saved,
                "instructions": "Saved instructions",
            },
        )
        payload["agent_id"] = agent["id"]
    if ephemeral is not None:
        payload["ephemeralAgent"] = {"artifacts": ephemeral}
    store = GenerationStore(db)
    generation, fresh = store.begin("alice", payload)
    assert fresh and generation.artifacts_mode == expected
    duplicate, fresh = store.begin(
        "alice",
        {
            **payload,
            "ephemeralAgent": {"artifacts": "different"},
        },
    )
    assert duplicate == generation and not fresh
    context = await resolver.root(duplicate)
    instructions = agent_instructions(agent, "anthropic", expected)
    assert resolver.history(duplicate, context) == (
        [{"role": "system", "content": instructions}] if instructions else []
    ) + [{"role": "user", "content": "Make an artifact"}]
    if agent is not None:
        stored = AgentRepository(db).get("alice", agent["id"])
        assert stored is not None and stored["artifacts"] == saved
