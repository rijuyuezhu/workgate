"""Native Windows smoke for the opt-in session-owned Job shell path."""

import sys

import pytest

from tests.support.responses import python_shell_command
from tests.support.sessions import get_test_tool_session_store
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import get_settings
from workgate.executor.shell_service import ShellService
from workgate.platform.windows_conpty import ConPtyRegistry, use_conpty_registry
from workgate.protocol.ids import new_session_id

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="native Windows ConPTY job backend only"
)


@pytest.mark.asyncio
async def test_windows_persisted_shell_and_python_use_same_job_companion(
    tmp_path,
):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    store = get_test_tool_session_store()
    session_id = str(new_session_id())
    store.create_session(session_id=session_id, workdir=workdir)
    service = ShellService(resolve_executor_config(get_settings()), store)

    registry = ConPtyRegistry()
    try:
        with use_conpty_registry(registry):
            quick = await service.bash(
                {
                    "session_id": session_id,
                    "command": python_shell_command(
                        "print('quick windows', flush=True)"
                    ),
                    "timeout_s": 15,
                    "persist_on_timeout": True,
                }
            )
            assert quick.mode == "job"
            assert quick.result["job_status"] == "succeeded", quick.result
            assert "quick windows" in quick.result["stdout"]

            slow = await service.run_python_code(
                {
                    "session_id": session_id,
                    "code": "import time\nprint('started',flush=True)\ntime.sleep(5)\nprint('completed',flush=True)",
                    "timeout_s": 1,
                    "persist_on_timeout": True,
                }
            )
            assert slow.mode == "job"
            assert slow.result["job_id"].startswith("job_")
            assert slow.result["attempts"] == 1
            final = await service.jobs.wait(
                session_id, slow.result["job_id"], 20
            )
            assert final.job.status == "succeeded"
            assert "completed" in final.output
            assert final.job.attempts == 1
    finally:
        registry.close()
