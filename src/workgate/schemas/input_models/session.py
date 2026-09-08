"""Typed input annotations for explicit agent sessions."""

from typing import Annotated, Literal

from pydantic import Field

SessionIdArg = Annotated[
    str,
    Field(
        min_length=8,
        max_length=128,
        pattern=r"^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
        description="Opaque shared agent/workspace session_id returned by session_start.",
    ),
]
SessionExecutorIdArg = Annotated[
    str | None,
    Field(
        max_length=128,
        pattern=r"^exec_[A-Za-z0-9_-]{22,}$",
        description="Optional stable executor_id. Omit only when exactly one eligible executor is online.",
    ),
]
SessionWorkdirArg = Annotated[
    str,
    Field(
        description="Working directory to bind to the session on the selected executor. Relative paths resolve against that executor's configured workspace root."
    ),
]
SessionLabelArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=80,
        description="Optional human-readable label for this agent session.",
    ),
]
SessionEndForceArg = Annotated[
    bool,
    Field(
        description="When the bound executor is permanently unreachable, release only the control binding without claiming executor-side cleanup. This may leave orphaned executor resources."
    ),
]


SessionCopyPathArg = Annotated[
    str,
    Field(
        description="Path to copy, resolved inside the corresponding source or destination session workdir."
    ),
]
SessionCopyKindArg = Annotated[
    Literal["auto", "file", "dir"],
    Field(
        description="What to copy. Use auto to infer file or directory from the source path."
    ),
]
SessionCopyOverwriteArg = Annotated[
    bool,
    Field(description="Whether an existing destination may be replaced."),
]
SessionCopyChunkSizeArg = Annotated[
    int | None,
    Field(
        description="Optional chunk size in bytes for binary transfer. Omit to use the server default."
    ),
]
SessionCopyBackgroundArg = Annotated[
    bool,
    Field(
        description="When true, start a managed background copy and return a job_id immediately. Use the job companion with the source session_id to poll, cancel, or retry it."
    ),
]
OptionalSessionIdArg = Annotated[
    str | None,
    Field(
        min_length=8,
        max_length=128,
        pattern=r"^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
        description="Optional explicit agent/workspace session_id used by internal transfer primitives.",
    ),
]
