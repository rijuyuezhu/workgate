"""Typed input annotations for agent bridge tools."""

from typing import Annotated, Any

from pydantic import Field

AgentSessionIdArg = Annotated[
    str,
    Field(
        description=(
            "Required execution session id. The bound executor adds "
            "<workdir>/.agents/skills as the highest-priority source."
        )
    ),
]
AgentMcpSessionIdArg = Annotated[
    str | None,
    Field(
        description=(
            "Optional execution session id. Pass it to include/call stdio MCP "
            "servers owned by the executor bound to that session."
        )
    ),
]
AgentSkillNameArg = Annotated[
    str, Field(description="Exact skill name returned by list_agent_skills.")
]
AgentSkillFilePathArg = Annotated[
    str,
    Field(
        description=(
            "Canonical POSIX path relative to the selected skill directory. "
            "Use one of activate_agent_skill.related_files."
        )
    ),
]
AgentServerArg = Annotated[
    str,
    Field(description="Exact configured agent MCP server name."),
]
AgentServerFilterArg = Annotated[
    str | None,
    Field(
        description="Optional exact agent MCP server name to filter listed tools."
    ),
]
AgentToolArg = Annotated[
    str,
    Field(
        description="Exact upstream tool name exposed by the selected agent MCP server."
    ),
]
AgentToolArgsArg = Annotated[
    dict[str, Any] | None,
    Field(
        description="JSON object of arguments passed to the upstream MCP tool."
    ),
]


AgentMcpSearchQueryArg = Annotated[
    str,
    Field(
        description="Keywords to match an external MCP tool name or description."
    ),
]
AgentMcpSearchLimitArg = Annotated[
    int, Field(ge=1, le=50, description="Maximum tools returned by discovery.")
]


AgentMcpManageConfigArg = Annotated[
    dict[str, Any] | None,
    Field(
        description=(
            "Complete MCP server connection config for register/update. "
            "Include type (http/stdio), URL or command/args, optional "
            "integrationId and auth. env/headers require {secret: key} "
            "references; never pass inline tokens, passwords, or API keys."
        )
    ),
]
