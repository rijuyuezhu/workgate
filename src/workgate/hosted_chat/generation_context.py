"""Resolve provider context for an already admitted owner-scoped generation."""

import copy
import json

from .agent_runtime import AgentContext
from .agents import AgentRepository
from .artifact_prompts import agent_instructions
from .credential_cipher import CredentialCipher
from .database import SQLiteDatabase
from .generations import Generation, GenerationStore, generation_model_history
from .provider_profiles import ProviderRepository
from .providers import provider_config, selected_provider
from .sampling import merge_sampling


def with_agent_instructions(
    history: list[dict],
    agent: dict | None,
    *,
    endpoint: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    instructions = agent_instructions(
        agent, endpoint or "openai", mode if endpoint is not None else ""
    )
    return (
        [{"role": "system", "content": instructions}] if instructions else []
    ) + copy.deepcopy(history)


class GenerationContextResolver:
    """Load server-owned profiles and live Agent revisions; not a grant.

    The generation actor and child scope remain responsible for authorization.
    Provider credentials are used only in the internal model context.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        owner: str,
        *,
        allow_account: bool = False,
    ):
        self.database = database
        self.owner = owner
        self.allow_account = allow_account
        self.providers = ProviderRepository(
            database, CredentialCipher(database)
        )
        self.agents = AgentRepository(database)

    def _row(self, db, generation: Generation):
        row = db.execute(
            "SELECT r.* FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=? AND c.owner=?",
            (generation.run_id, self.owner),
        ).fetchone()
        if (
            row is None
            or GenerationStore._run(row) != generation
            or generation.status != "running"
        ):
            raise PermissionError(
                "Generation model identity is no longer current"
            )
        return row

    def _context(
        self,
        generation: Generation,
        agent: dict | None,
        *,
        use_admitted_model: bool,
    ) -> AgentContext:
        provider = (
            generation.provider
            if use_admitted_model
            else selected_provider({}, agent)
        )
        model = (
            generation.model
            if use_admitted_model
            else (agent or {}).get("model")
        )
        profile = provider_config(
            self.providers, provider, model, allow_account=self.allow_account
        )
        if use_admitted_model and (
            profile.model != generation.model
            or (profile.channel or profile.provider) != generation.provider
        ):
            raise PermissionError("Admitted provider identity changed")
        overrides = (
            generation.sampling
            if agent is None
            else agent.get("model_parameters", {})
        )
        return AgentContext(
            profile,
            [],
            parameters=merge_sampling(profile.sampling, overrides),
            recursion_limit=(agent or {}).get("recursion_limit"),
            tool_end=(agent or {}).get("end_after_tools") is True,
            agent=copy.deepcopy(agent),
        )

    def root_agent(self, generation: Generation) -> dict | None:
        with self.database.read_snapshot() as db:
            row = self._row(db, generation)
            agent = None
            if generation.agent_id is not None:
                agent_row = db.execute(
                    "SELECT * FROM agents WHERE id=? AND owner=?",
                    (generation.agent_id, self.owner),
                ).fetchone()
                if agent_row is None:
                    raise PermissionError("Generation Agent is no longer owned")
                agent = self.agents._from_row(agent_row)
                if (
                    json.loads(row["state"]).get("agentRevision")
                    != agent["version"]
                ):
                    raise PermissionError("Generation Agent revision changed")
                if selected_provider({}, agent) != generation.provider or (
                    agent.get("model") and agent["model"] != generation.model
                ):
                    raise PermissionError(
                        "Generation Agent model identity changed"
                    )
        return agent

    async def root(self, generation: Generation) -> AgentContext:
        return self._context(
            generation, self.root_agent(generation), use_admitted_model=True
        )

    async def child(
        self, generation: Generation, agent: dict | None
    ) -> AgentContext:
        with self.database.read_snapshot() as db:
            self._row(db, generation)
            if agent is None:
                if generation.agent_id is not None:
                    raise PermissionError(
                        "Saved Agent cannot resolve as ordinary chat"
                    )
            else:
                row = db.execute(
                    "SELECT * FROM agents WHERE id=? AND owner=?",
                    (agent.get("id"), self.owner),
                ).fetchone()
                if row is None:
                    raise PermissionError("Child Agent is no longer owned")
                current = self.agents._from_row(row)
                if any(
                    current.get(key) != agent.get(key)
                    for key in ("version", "provider", "model")
                ):
                    raise PermissionError(
                        "Child Agent model identity or revision changed"
                    )
        return self._context(
            generation, agent, use_admitted_model=agent is None
        )

    def history(
        self, generation: Generation, context: AgentContext
    ) -> list[dict]:
        with self.database.read_snapshot() as db:
            self._row(db, generation)
            history = generation_model_history(db, self.owner, generation)
        if context.agent != self.root_agent(generation):
            raise PermissionError("Generation history Agent identity changed")
        return with_agent_instructions(
            history,
            context.agent,
            endpoint=context.profile.provider,
            mode=generation.artifacts_mode,
        )
