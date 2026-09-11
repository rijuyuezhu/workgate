import base64
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue, TypeAdapter

from workgate.config.settings import Settings
from workgate.control.runtime import ControlRuntime, build_control_runtime
from workgate.control.state import ExecutorTrustRecord
from workgate.executor.connection import operation_error_from_exception
from workgate.executor.hello import build_executor_hello
from workgate.executor.runtime import ExecutorRuntime, build_executor_runtime
from workgate.executor.services import install_runtime_services
from workgate.persistence import use_state_store
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.executor import ExecutorCommand, ExecutorResult
from workgate.protocol.ids import new_command_id, new_executor_id
from workgate.utils.serialization import to_jsonable

_JSON_VALUE = TypeAdapter(JsonValue)


@dataclass
class PairedControlHarness:
    """Test-only direct executor bridge with final shared-session semantics."""

    control: ControlRuntime
    executor: ExecutorRuntime
    executor_id: str

    async def is_online(self, executor_id: str) -> bool:
        return executor_id == self.executor_id

    async def inventory(self, executor_id: str):
        if executor_id != self.executor_id:
            return None
        return build_executor_hello(
            self.executor.config, sessions=self.executor.sessions.inventory()
        )

    async def call(
        self,
        executor_id: str,
        op: str,
        args: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_s: float | None = None,
    ) -> ExecutorResult:
        _ = timeout_s
        if executor_id != self.executor_id:
            raise RuntimeError(f"executor {executor_id!r} is offline")
        command = ExecutorCommand(
            id=new_command_id(),
            op=op,
            session_id=session_id,
            args=args or {},
        )
        installation = install_runtime_services(self.executor.services)
        try:
            with use_state_store(self.executor.services.state_store):
                try:
                    value = await self.executor._execute_protocol_command(
                        command
                    )
                except Exception as exc:
                    return ExecutorResult(
                        id=command.id,
                        ok=False,
                        error=operation_error_from_exception(exc),
                    )
                return ExecutorResult(
                    id=command.id,
                    ok=True,
                    result=_JSON_VALUE.validate_python(to_jsonable(value)),
                )
        finally:
            installation.close()


def build_paired_control_harness(
    settings: Settings,
    *,
    executor_state_dir: Path | None = None,
) -> PairedControlHarness:
    """Build one control plus an eligible executor without a local-control fallback."""
    control = build_control_runtime(settings)
    executor_settings = settings.model_copy(
        update={
            "state_dir": executor_state_dir
            or settings.state_dir.parent / f"{settings.state_dir.name}-executor"
        }
    )
    executor = build_executor_runtime(
        executor_settings, enable_control_connection=False
    )
    executor_id = str(new_executor_id())
    credential = new_executor_credential()
    control.control_state.start()
    control.control_state.put_executor(
        ExecutorTrustRecord(
            executor_id=executor_id,
            name="test-executor",
            credential_verifier=executor_credential_verifier(credential),
            created_at=1.0,
        )
    )
    harness = PairedControlHarness(control, executor, executor_id)
    transport = cast(Any, control.executor_transport)
    transport.is_online = harness.is_online
    transport.inventory = harness.inventory
    transport.call = harness.call
    return harness


def build_paired_mcp(settings: Settings):
    """Build the public MCP adapter over one test-only paired executor."""
    from workgate.control.mcp.app import build_mcp

    harness = build_paired_control_harness(settings)
    return build_mcp(runtime=harness.control), harness


def build_paired_http_app(settings: Settings):
    """Build the public REST adapter over one test-only paired executor."""
    from workgate.control.http.app import build_http_app

    harness = build_paired_control_harness(settings)
    return build_http_app(runtime=harness.control), harness


def _mcp_content(response: Any, index: int = 0) -> Any:
    first = response[0]
    if isinstance(first, list):
        return first[index]
    return response[index]


def mcp_text(response: Any, index: int = 0) -> str:
    """Return text from a FastMCP call_tool response in tests."""
    return str(_mcp_content(response, index).text)


def nested_mcp_text(
    response: Any, index: int = 0, nested_index: int = 0
) -> str:
    """Return text from nested FastMCP responses produced by fetch tests."""
    content = _mcp_content(response, index)
    if isinstance(content, list):
        return str(content[nested_index].text)
    return str(content.text)


def mcp_structured(response: Any) -> dict[str, Any]:
    """Return structured content from a FastMCP structured-output response."""
    assert isinstance(response, tuple)
    assert isinstance(response[1], dict)
    return response[1]


def python_shell_command(source: str) -> str:
    """Return one shell-safe Python command for POSIX shells and cmd.exe."""
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    wrapper = f"import base64;exec(base64.b64decode('{encoded}'))"
    argv = [sys.executable, "-c", wrapper]
    return (
        subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    )
