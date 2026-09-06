"""Remote worker-side tool dispatch and process loop."""

import asyncio
import json
import math
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from ..agent_bridge.redaction import _redact_text
from ..errors import tool_error_payload
from ..remote.constants import REMOTE_API_PREFIX
from ..remote.tool_specs import REMOTE_WORKER_TOOL_NAMES
from ..version import version_info
from . import runtime as worker_runtime
from .identity import (
    delete_worker_identity as _delete_persisted_worker_identity,
)
from .identity import (
    load_worker_identity as _load_persisted_worker_identity,
)
from .identity import (
    read_worker_identity as _read_persisted_worker_identity,
)
from .identity import (
    write_worker_identity as _write_persisted_worker_identity,
)
from .profiles import update_worker_profile
from .serialization import to_jsonable
from .state import (
    activate_worker_profile,
    activate_worker_runtime,
    active_worker_profile_id,
    worker_launcher_path,
    worker_profile_dir,
    worker_state_dir,
)


class WorkerHttpError(RuntimeError):
    """HTTP response error with a status code usable by retry policy."""

    def __init__(self, url: str, status_code: int, detail: str) -> None:
        self.url = url
        self.status_code = status_code
        self.detail = detail
        super().__init__(
            f"worker HTTP POST {_redact_text(url)} failed with {status_code}: "
            f"{_redact_text(detail)}"
        )


def _handled_remote_exception(exc: Exception) -> dict[str, Any]:
    """Convert local helper failures into serializable worker-side error payloads."""
    workspace_root = os.getenv("WORKGATE_WORKSPACE_ROOT") or "."
    data = tool_error_payload(exc, workspace_root=workspace_root)
    return {
        "ok": False,
        "error": str(data["error_type"]),
        "message": str(data["message"]),
        "data": data,
    }


WORKER_TOOL_NAMES = REMOTE_WORKER_TOOL_NAMES
_WORKER_CONNECT_TIMEOUT_S = 10.0
_WORKER_POLL_TIMEOUT_GRACE_S = 10.0


async def execute_worker_tool(tool: str, args: dict[str, Any]) -> Any:
    """Dispatch a remote-worker tool call through the canonical local handler."""
    if tool not in WORKER_TOOL_NAMES:
        raise ValueError(f"unsupported remote worker tool: {tool}")
    from .dispatch import execute_worker_tool as dispatch_worker_tool

    return await dispatch_worker_tool(tool, args)


def _parse_worker_http_json(
    url: str, status_code: int, response_body: str
) -> dict[str, Any]:
    """Validate one worker HTTP response and return its JSON object body."""
    if not 200 <= status_code < 300:
        detail = response_body.strip() or "<empty response body>"
        raise WorkerHttpError(url, status_code, detail)
    try:
        decoded = json.loads(response_body)
    except json.JSONDecodeError as exc:
        detail = response_body.strip() or "<empty response body>"
        raise RuntimeError(
            f"worker HTTP POST {_redact_text(url)} returned invalid JSON: "
            f"{_redact_text(detail)}"
        ) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(
            f"worker HTTP POST {_redact_text(url)} returned JSON "
            f"{type(decoded).__name__}, expected object"
        )
    return cast(dict[str, Any], decoded)


def _worker_post_json_with_curl(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float | None = None,
    connect_timeout: float | None = _WORKER_CONNECT_TIMEOUT_S,
) -> dict[str, Any]:
    """POST JSON through curl with separate connection and request deadlines."""
    curl = shutil.which("curl")
    if not curl:
        raise FileNotFoundError("curl is not available")
    status_marker = "\nWORKGATE_HTTP_STATUS:"
    command = [curl]
    if connect_timeout is not None:
        command.extend(["--connect-timeout", f"{connect_timeout:g}"])
    if timeout is not None:
        command.extend(["--max-time", f"{timeout:g}"])
    command.extend(
        [
            "-sS",
            "-L",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            "-w",
            f"{status_marker}%{{http_code}}",
        ]
    )
    for name, value in headers.items():
        command.extend(["-H", f"{name}: {value}"])
    command.append(url)
    completed = subprocess.run(
        command, input=body, capture_output=True, check=False
    )  # noqa: S603
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    response_body, marker, status_text = stdout.rpartition(status_marker)
    status_code = int(status_text) if marker and status_text.isdigit() else 0
    if completed.returncode != 0:
        detail_parts = [
            part for part in (stderr, response_body.strip()) if part
        ]
        detail = (
            "\n".join(detail_parts) or "curl exited without a response body"
        )
        raise RuntimeError(
            f"worker HTTP POST {_redact_text(url)} failed with curl exit "
            f"{completed.returncode} (HTTP {status_code}): {_redact_text(detail)}"
        )
    return _parse_worker_http_json(url, status_code, response_body)


def _worker_post_json_with_urllib(
    url: str, body: bytes, headers: dict[str, str], timeout: float | None = None
) -> dict[str, Any]:
    """POST JSON using the standard library fallback."""
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            status_code = response.status
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        return _parse_worker_http_json(url, exc.code, response_body)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"worker HTTP request failed: {exc.reason}") from exc
    return _parse_worker_http_json(url, status_code, response_body)


def _worker_post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    connect_timeout: float | None = _WORKER_CONNECT_TIMEOUT_S,
) -> dict[str, Any]:
    """POST a JSON worker request with bounded connection and request time."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("worker server URL must use absolute HTTP(S)")
    body = json.dumps(payload).encode("utf-8")
    request_headers = headers or {}
    if shutil.which("curl"):
        return _worker_post_json_with_curl(
            url, body, request_headers, timeout, connect_timeout
        )
    if timeout is not None and parsed.path.endswith(
        f"{REMOTE_API_PREFIX}/poll"
    ):
        raise RuntimeError("curl is required for bounded worker poll requests")
    return _worker_post_json_with_urllib(url, body, request_headers, timeout)


_WORKER_RETRY_INITIAL_DELAY_S = 1.0
_WORKER_RETRY_MAX_DELAY_S = 30.0


def _worker_poll_request_timeout_s(data: dict[str, Any]) -> float | None:
    """Return a request timeout with transport grace from controller data."""
    if "poll_timeout_s" not in data:
        return None
    try:
        poll_timeout_s = float(data["poll_timeout_s"])
    except TypeError, ValueError:
        return None
    if not math.isfinite(poll_timeout_s) or poll_timeout_s <= 0:
        return None
    return poll_timeout_s + _WORKER_POLL_TIMEOUT_GRACE_S


def _worker_retry_delay(attempt: int) -> float:
    """Return exponential reconnect delay capped for long-running worker loops."""
    return min(
        _WORKER_RETRY_INITIAL_DELAY_S * (2 ** min(attempt, 5)),
        _WORKER_RETRY_MAX_DELAY_S,
    )


def _worker_log_retry(operation: str, exc: Exception, delay_s: float) -> None:
    """Print one credential-redacted retry status line for worker operators."""
    print(
        f"Status: {operation} failed: {_redact_text(str(exc))}. "
        f"Retrying in {delay_s:g}s...",
        file=sys.stderr,
        flush=True,
    )


def _worker_error_is_retryable(exc: Exception) -> bool:
    """Return whether a failed worker request should be retried."""
    if isinstance(exc, WorkerHttpError):
        return exc.status_code in {408, 425, 429} or exc.status_code >= 500
    return not isinstance(exc, ValueError)


async def _worker_post_json_forever(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    operation: str = "request",
) -> dict[str, Any]:
    """POST JSON until it succeeds, preserving remote workers across outages."""
    attempt = 0
    while True:
        try:
            return await asyncio.to_thread(
                _worker_post_json, url, payload, headers, timeout
            )
        except Exception as exc:
            if not _worker_error_is_retryable(exc):
                raise
            delay_s = _worker_retry_delay(attempt)
            attempt += 1
            _worker_log_retry(operation, exc, delay_s)
            await asyncio.sleep(delay_s)


def _worker_state_dir() -> Path:
    """Return the state directory used to persist this worker identity."""
    return worker_state_dir()


def _configure_worker_runtime_env(
    workdir: str, profile_id: str | None = None
) -> None:
    """Configure worker-local runtime paths before loading normal settings."""
    os.environ["WORKGATE_WORKSPACE_ROOT"] = workdir
    runtime_state = (
        worker_profile_dir(profile_id) / "state"
        if profile_id is not None
        else _worker_state_dir() / "runtime"
    )
    os.environ["WORKGATE_STATE_DIR"] = str(runtime_state)
    os.environ["WORKGATE_ALLOW_FULL_CONTROL"] = "true"


def _prepare_worker_runtime_settings(
    workdir: str | None, profile_id: str | None = None
) -> str:
    """Resolve worker paths before constructing a settings snapshot."""
    resolved_workdir = str(Path(workdir or os.getcwd()).expanduser().resolve())
    if profile_id is None:
        _configure_worker_runtime_env(resolved_workdir)
    else:
        _configure_worker_runtime_env(resolved_workdir, profile_id)
    from ..config.settings import clear_settings_cache

    clear_settings_cache()
    return resolved_workdir


def _read_worker_identity(
    server: str | None = None,
    requested_name: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any] | None:
    """Read a stored worker identity, optionally matching server and name."""
    return _read_persisted_worker_identity(server, requested_name, profile_id)


def load_worker_identity(profile_id: str | None = None) -> dict[str, Any]:
    """Load the complete stored identity required by ``worker run``."""
    return _load_persisted_worker_identity(profile_id)


def _write_worker_identity(
    data: dict[str, Any], profile_id: str | None = None
) -> None:
    """Persist a worker identity atomically with owner-only permissions."""
    _write_persisted_worker_identity(data, profile_id)


def _delete_worker_identity(profile_id: str | None = None) -> None:
    """Remove the stored worker identity after the control server rejects it."""
    _delete_persisted_worker_identity(profile_id)


def _worker_identity_rejected(exc: Exception) -> bool:
    """Return whether a resume failure means the persisted identity is invalid."""
    if isinstance(exc, WorkerHttpError) and exc.status_code == 401:
        return True
    message = str(exc).lower()
    return (
        "invalid worker identity" in message
        or "identity is no longer valid" in message
    )


async def _worker_resume_or_none(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float | None = None,
    profile_id: str | None = None,
) -> dict[str, Any] | None:
    """Try to resume a worker identity, retrying transient failures indefinitely."""
    attempt = 0
    while True:
        try:
            return await asyncio.to_thread(
                _worker_post_json, url, payload, headers, timeout
            )
        except Exception as exc:
            if _worker_identity_rejected(exc):
                print(
                    "Status: stored worker identity rejected; falling back to invite registration.",
                    file=sys.stderr,
                    flush=True,
                )
                _delete_worker_identity(profile_id)
                return None
            if not _worker_error_is_retryable(exc):
                raise
            delay_s = _worker_retry_delay(attempt)
            attempt += 1
            _worker_log_retry("resume", exc, delay_s)
            await asyncio.sleep(delay_s)


async def _worker_job_heartbeat_loop(
    task: asyncio.Task[Any],
    server: str,
    headers: dict[str, str],
    heartbeat_interval_s: float,
) -> None:
    """Refresh liveness until one worker job finishes or heartbeat becomes fatal."""
    interval = max(0.01, heartbeat_interval_s)
    while not task.done():
        await asyncio.sleep(interval)
        if task.done():
            return
        try:
            await asyncio.to_thread(
                _worker_post_json,
                f"{server}{REMOTE_API_PREFIX}/heartbeat",
                {},
                headers,
                30,
            )
        except Exception as exc:
            if not _worker_error_is_retryable(exc):
                return
            _worker_log_retry("heartbeat", exc, interval)


async def _execute_worker_job_with_heartbeat(
    job: dict[str, Any],
    server: str,
    headers: dict[str, str],
    heartbeat_interval_s: float,
    execute_tool: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
) -> Any:
    """Execute one job while independently refreshing control-side liveness."""
    execute = execute_tool or execute_worker_tool

    async def run_tool() -> Any:
        return await execute(job["tool"], dict(job.get("args") or {}))

    task = asyncio.create_task(run_tool())
    heartbeat = asyncio.create_task(
        _worker_job_heartbeat_loop(
            task,
            server,
            headers,
            heartbeat_interval_s,
        )
    )
    try:
        return await task
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def _submit_worker_result_with_heartbeat(
    result: dict[str, Any],
    server: str,
    headers: dict[str, str],
    heartbeat_interval_s: float,
) -> dict[str, Any]:
    """Retry one result submission while preserving worker liveness."""
    submission = asyncio.create_task(
        _worker_post_json_forever(
            f"{server}{REMOTE_API_PREFIX}/result",
            result,
            headers,
            30,
            "submit result",
        )
    )

    async def heartbeat_loop() -> None:
        interval = max(0.01, heartbeat_interval_s)
        while not submission.done():
            await asyncio.sleep(interval)
            if submission.done():
                return
            try:
                await asyncio.to_thread(
                    _worker_post_json,
                    f"{server}{REMOTE_API_PREFIX}/heartbeat",
                    {},
                    headers,
                    30,
                )
            except Exception as exc:
                if not _worker_error_is_retryable(exc):
                    return
                _worker_log_retry("heartbeat", exc, interval)

    heartbeat = asyncio.create_task(heartbeat_loop())
    try:
        return await submission
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


def worker_capabilities() -> list[str]:
    """List tool categories available in the worker environment."""
    return [
        "shell",
        "persistent_shell",
        "files",
        "search",
        "python",
        "transfer",
        "http-transfer-v1",
    ]


def worker_info(workdir: str, profile_id: str | None = None) -> dict[str, Any]:
    """Return worker identity, workspace, platform, Python, and capability metadata."""
    info = {
        "workgate_version": str(version_info().get("version") or ""),
        "hostname": socket.gethostname(),
        "user": os.getenv("USER") or os.getenv("USERNAME") or "unknown",
        "cwd": os.getcwd(),
        "workdir": workdir,
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    if profile_id is not None:
        info["profile_id"] = profile_id
        info["launcher_path"] = str(worker_launcher_path())
    return info


def _format_reconnect_command(argv: list[str], *, windows: bool) -> str:
    """Format one reconnect argv for the target platform's command shell."""
    return subprocess.list2cmdline(argv) if windows else shlex.join(argv)


def worker_reconnect_command(profile_id: str) -> str:
    """Return the credential-free command that restarts one local profile."""
    return _format_reconnect_command(
        [str(worker_launcher_path()), profile_id], windows=os.name == "nt"
    )


def _worker_poll_payload(
    worker_version: str,
    running_runtime: dict[str, str],
    poll_request_timeout_s: float | None,
) -> dict[str, Any]:
    """Build one poll report and advertise the worker-side deadline."""
    payload = worker_runtime.worker_poll_payload(
        worker_version, running_runtime
    )
    if poll_request_timeout_s is not None:
        payload["poll_timeout_s"] = max(
            0.001, poll_request_timeout_s - _WORKER_POLL_TIMEOUT_GRACE_S
        )
    return payload


async def _install_and_reexec_worker(
    instruction: dict[str, Any],
    *,
    server: str,
    worker_version: str,
) -> None:
    """Install one required runtime and replace the idle worker process."""
    installed = await asyncio.to_thread(
        worker_runtime.install_runtime,
        server,
        instruction,
        current_version=worker_version,
    )
    expected_digest = str(instruction.get("sha256") or "")
    expected_version = str(instruction.get("version") or "")
    if (
        installed.get("sha256") != expected_digest
        or installed.get("version") != expected_version
    ):
        raise RuntimeError(
            "installed worker runtime does not match instruction"
        )
    profile_id = active_worker_profile_id()
    activate_worker_runtime(expected_digest)
    if profile_id is not None:
        identity = load_worker_identity(profile_id)
        update_worker_profile(
            profile_id,
            runtime_sha256=expected_digest,
            runtime_version=expected_version,
            server=str(identity["server"]),
            name=str(identity["name"]),
            workdir=str(identity["workdir"]),
        )
    if os.getenv("WORKGATE_WORKER_MANAGED") == "1":
        from .service import ensure_launcher

        ensure_launcher(expected_digest, profile_id)
    print(
        f"Status: worker runtime {expected_version} ({expected_digest[:12]}) installed; restarting.",
        file=sys.stderr,
        flush=True,
    )
    worker_runtime.reexec_worker()
    raise RuntimeError("worker restart returned unexpectedly")


async def _enroll_or_resume_worker(
    server: str,
    invite: str,
    name: str | None = None,
    workdir: str | None = None,
    profile_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Enroll or resume one identity and persist it without entering the poll loop."""
    profile_id = activate_worker_profile(profile_id)
    resolved_workdir = _prepare_worker_runtime_settings(workdir, profile_id)
    server = server.rstrip("/")
    register_payload = {
        "invite": invite,
        "name": name,
        "workdir": resolved_workdir,
        "capabilities": worker_capabilities(),
        "info": worker_info(resolved_workdir, profile_id),
        "runtime": worker_runtime.worker_runtime_report(
            str(version_info().get("version") or "")
        ),
    }
    identity = (
        _read_worker_identity(server, name)
        if profile_id is None
        else _read_worker_identity(server, name, profile_id)
    )
    body: dict[str, Any] | None = None
    access = ""
    if identity:
        access = str(identity["access"])
        resume_payload = {**register_payload, "name": str(identity["name"])}
        resume_headers = {"Author" + "ization": "B" + "earer " + access}
        resume_url = f"{server}{REMOTE_API_PREFIX}/resume"
        if profile_id is None:
            body = await _worker_resume_or_none(
                resume_url,
                resume_payload,
                resume_headers,
                30,
            )
        else:
            body = await _worker_resume_or_none(
                resume_url,
                resume_payload,
                resume_headers,
                30,
                profile_id,
            )
    if body is None:
        if not invite:
            raise ValueError(
                "an invite is required when no resumable worker identity exists"
            )
        body = await _worker_post_json_forever(
            f"{server}{REMOTE_API_PREFIX}/register",
            register_payload,
            None,
            30,
            "register",
        )
        if not body.get("ok"):
            raise RuntimeError(body.get("message") or body)
        data = body["data"]
        access = data["to" + "ken"]
        machine_name = data["name"]
    else:
        if not body.get("ok"):
            raise RuntimeError(body.get("message") or body)
        data = body["data"]
        machine_name = data["name"]
    stored = {
        "server": server,
        "name": machine_name,
        "access": access,
        "workdir": resolved_workdir,
    }
    if profile_id is not None:
        stored["profile_id"] = profile_id
        _write_worker_identity(stored, profile_id)
        runtime_identity = worker_runtime.current_runtime_identity()
        runtime_digest = str(runtime_identity.get("sha256") or "")
        if runtime_digest:
            update_worker_profile(
                profile_id,
                runtime_sha256=runtime_digest,
                runtime_version=str(
                    runtime_identity.get("bundle_version") or ""
                ),
                server=server,
                name=machine_name,
                workdir=resolved_workdir,
            )
    else:
        _write_worker_identity(stored)
    return stored, data


async def enroll_worker(
    server: str,
    invite: str,
    name: str | None = None,
    workdir: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Enroll or resume one worker identity and exit without polling."""
    from .lifecycle import worker_run_lock

    profile_id = activate_worker_profile(profile_id)
    lock = (
        worker_run_lock() if profile_id is None else worker_run_lock(profile_id)
    )
    with lock:
        if profile_id is None:
            identity, _data = await _enroll_or_resume_worker(
                server, invite, name, workdir
            )
        else:
            identity, _data = await _enroll_or_resume_worker(
                server, invite, name, workdir, profile_id
            )
    return identity


async def run_worker(
    server: str,
    invite: str,
    name: str | None = None,
    workdir: str | None = None,
    profile_id: str | None = None,
) -> None:
    """Run one worker process while holding its lifecycle lock."""
    from ..config.settings import Settings
    from ..executor.runtime import build_executor_runtime
    from .lifecycle import worker_run_lock

    profile_id = activate_worker_profile(profile_id)
    resolved_workdir = _prepare_worker_runtime_settings(workdir, profile_id)
    runtime = build_executor_runtime(Settings())
    lock = (
        worker_run_lock() if profile_id is None else worker_run_lock(profile_id)
    )
    with lock:
        async with runtime.lifespan():
            if profile_id is None:
                await _run_worker_locked(
                    server,
                    invite,
                    name,
                    resolved_workdir,
                    execute_tool=runtime.dispatcher.execute,
                )
            else:
                await _run_worker_locked(
                    server,
                    invite,
                    name,
                    resolved_workdir,
                    profile_id,
                    execute_tool=runtime.dispatcher.execute,
                )


async def run_stored_worker(profile_id: str | None = None) -> None:
    """Run using only the private persisted identity."""
    profile_id = activate_worker_profile(profile_id)
    identity = (
        load_worker_identity()
        if profile_id is None
        else load_worker_identity(profile_id)
    )
    run_args = (
        str(identity["server"]),
        "",
        str(identity["name"]),
        str(identity["workdir"]),
    )
    if profile_id is None:
        await run_worker(*run_args)
    else:
        await run_worker(*run_args, profile_id)


async def _run_worker_locked(
    server: str,
    invite: str,
    name: str | None = None,
    workdir: str | None = None,
    profile_id: str | None = None,
    execute_tool: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
) -> None:
    """Enroll or resume, then poll and execute jobs under the worker lock."""
    identity, data = await _enroll_or_resume_worker(
        server, invite, name, workdir, profile_id
    )
    server = str(identity["server"])
    machine_name = str(identity["name"])
    access = str(identity["access"])
    workdir = str(identity["workdir"])
    heartbeat_interval_s = float(data.get("heartbeat_interval_s") or 15)
    poll_request_timeout_s = _worker_poll_request_timeout_s(data)
    upgrade = data.get("upgrade")
    if isinstance(upgrade, dict) and upgrade.get("required") is True:
        worker_version = str(version_info().get("version") or "")
        attempt = 0
        while True:
            try:
                await _install_and_reexec_worker(
                    upgrade,
                    server=server,
                    worker_version=worker_version,
                )
                raise RuntimeError("worker upgrade returned unexpectedly")
            except SystemExit:
                raise
            except Exception as exc:
                delay_s = _worker_retry_delay(attempt)
                attempt += 1
                _worker_log_retry("upgrade", exc, delay_s)
                await asyncio.sleep(delay_s)
    print("workgate worker")
    print(f"Server:  {server}")
    print(f"Name:    {machine_name}")
    print(f"Workdir: {workdir}")
    print("Status: connected")
    if profile_id is not None:
        print(f"Reconnect: {worker_reconnect_command(profile_id)}")
    print(
        "Keep this process running while ChatGPT should access this machine. Press Ctrl-C to disconnect.",
        flush=True,
    )
    headers = {"Author" + "ization": "B" + "earer " + access}
    worker_version = str(version_info().get("version") or "")
    running_runtime = worker_runtime.current_runtime_identity()
    upgrade_attempt = 0
    while True:
        poll_body = await _worker_post_json_forever(
            f"{server}{REMOTE_API_PREFIX}/poll",
            _worker_poll_payload(
                worker_version, running_runtime, poll_request_timeout_s
            ),
            headers,
            poll_request_timeout_s,
            "poll",
        )
        payload = poll_body.get("data", {})
        if not isinstance(payload, dict):
            continue
        updated_poll_request_timeout_s = _worker_poll_request_timeout_s(payload)
        if updated_poll_request_timeout_s is not None:
            poll_request_timeout_s = updated_poll_request_timeout_s
        upgrade = payload.get("upgrade")
        if isinstance(upgrade, dict) and upgrade.get("required") is True:
            try:
                await _install_and_reexec_worker(
                    upgrade,
                    server=server,
                    worker_version=worker_version,
                )
            except SystemExit:
                raise
            except Exception as exc:
                delay_s = _worker_retry_delay(upgrade_attempt)
                upgrade_attempt += 1
                _worker_log_retry("upgrade", exc, delay_s)
                await asyncio.sleep(delay_s)
            continue
        upgrade_attempt = 0
        job = payload.get("job")
        if not job:
            continue
        expires_at = float(job.get("expires_at") or 0)
        if expires_at and expires_at < time.time():
            out = {
                "job_id": job.get("id"),
                "ok": False,
                "error": "TimeoutError",
                "message": "remote job expired before execution",
            }
            await _submit_worker_result_with_heartbeat(
                out,
                server,
                headers,
                heartbeat_interval_s,
            )
            continue
        try:
            result = await _execute_worker_job_with_heartbeat(
                job,
                server,
                headers,
                heartbeat_interval_s,
                execute_tool,
            )
            out = {"job_id": job["id"], "ok": True, "data": to_jsonable(result)}
        except Exception as exc:
            out = {"job_id": job.get("id"), **_handled_remote_exception(exc)}
        await _submit_worker_result_with_heartbeat(
            out,
            server,
            headers,
            heartbeat_interval_s,
        )
