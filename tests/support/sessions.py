"""Test-owned tool session stores."""

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.tool_session.store import ToolSessionStore
from workgate.persistence import FileStateStore, StateStore

_TEST_TOOL_SESSION_STORE: ToolSessionStore | None = None


def configure_test_tool_session_store(
    store: ToolSessionStore | None,
) -> ToolSessionStore | None:
    """Install the pytest harness session store and return the previous one."""
    global _TEST_TOOL_SESSION_STORE
    previous = _TEST_TOOL_SESSION_STORE
    _TEST_TOOL_SESSION_STORE = store
    return previous


def get_test_tool_session_store() -> ToolSessionStore:
    """Return the session store owned by the current pytest fixture."""
    if _TEST_TOOL_SESSION_STORE is None:
        raise RuntimeError("test tool-session store is not configured")
    return _TEST_TOOL_SESSION_STORE


def build_tool_session_store(
    settings: Settings, *, state_store: StateStore | None = None
) -> ToolSessionStore:
    """Build one explicitly configured executor session store for tests."""
    config = resolve_executor_config(settings)
    return ToolSessionStore(
        state_store=state_store or FileStateStore(lambda: config.state_dir),
        default_workdir=config.default_workdir,
        max_session_snapshots=config.max_session_snapshots,
        max_session_snapshot_bytes=config.max_session_snapshot_bytes,
    )
