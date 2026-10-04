"""Typed input annotations for execution sessions."""

from typing import Annotated, Literal

from pydantic import Field

SessionIdArg = Annotated[
    str,
    Field(
        min_length=8,
        max_length=128,
        pattern=r"^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
        description="Opaque execution session_id returned by session_start.",
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
        min_length=1,
        max_length=4096,
        description="Working directory to bind to the session. Relative paths resolve against the executor's effective default workdir.",
    ),
]
SessionStartWorkdirArg = Annotated[
    str | None,
    Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="Optional initial workdir. Omit to use the executor's effective default workdir.",
    ),
]
SessionLabelArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=80,
        description="Optional human-readable label for this execution session.",
    ),
]
SessionTaskIdArg = Annotated[
    str | None,
    Field(
        default=None,
        max_length=128,
        pattern=r"^task_[A-Za-z0-9_-]{22,}$",
        description=(
            "Optional existing semantic task_id to attach to this execution session. "
            "It never selects or changes the executor or workdir."
        ),
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
        description="Path to copy. Relative values resolve from the corresponding source or destination session workdir."
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
    Field(
        description="Whether an existing destination may be replaced. Defaults to false; set true explicitly to replace it."
    ),
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
        description="Defaults to true: start a durable managed copy and return a job_id immediately. Use the job companion with the source session_id to poll, cancel, or retry it. Set false explicitly for a synchronous copy subject to the normal tool timeout."
    ),
]
OptionalSessionIdArg = Annotated[
    str | None,
    Field(
        min_length=8,
        max_length=128,
        pattern=r"^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
        description="Optional execution session_id used by internal transfer primitives.",
    ),
]
