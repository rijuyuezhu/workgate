import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient


def _configure_remote_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workgate.config.settings import clear_settings_cache
    from workgate.persistence import configure_state_store
    from workgate.tool_session import configure_tool_session_store

    configure_tool_session_store(None)
    configure_state_store(None)
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_REMOTE_POLL_TIMEOUT_S", "1")
    clear_settings_cache()


def _runtime_report(
    *, digest: str | None = None, version: str | None = None
) -> dict[str, Any]:
    from workgate.remote.bundle import worker_bundle_manifest
    from workgate.remote.constants import REMOTE_WORKER_RUNTIME_PROTOCOL_VERSION

    manifest = worker_bundle_manifest()
    return {
        "protocol_version": REMOTE_WORKER_RUNTIME_PROTOCOL_VERSION,
        "runtime_kind": "managed_bundle",
        "worker_version": version or str(manifest["bundle_version"]),
        "bundle_version": version or str(manifest["bundle_version"]),
        "bundle_sha256": digest or str(manifest["sha256"]),
    }


async def _registered_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from workgate.remote.manager import RemoteManager

    _configure_remote_state(tmp_path, monkeypatch)
    manager = RemoteManager()
    invite = await manager.create_invite(name="worker-a")
    registered = await manager.register_worker(
        {
            "invite": invite.code,
            "workdir": str(tmp_path),
            "capabilities": ["shell"],
            "info": {"hostname": "edge"},
            "runtime": _runtime_report(),
        }
    )
    return manager, registered


@pytest.mark.asyncio
async def test_enrollment_requires_managed_runtime_without_consuming_invite(
    tmp_path, monkeypatch
):
    from workgate.remote.manager import (
        RemoteManager,
        WorkerRuntimeCompatibilityError,
    )

    _configure_remote_state(tmp_path, monkeypatch)
    manager = RemoteManager()
    invite = await manager.create_invite(name="worker-a")

    with pytest.raises(
        WorkerRuntimeCompatibilityError, match="generated invite"
    ):
        await manager.register_worker({"invite": invite.code})
    assert invite.code in manager.invites

    with pytest.raises(
        WorkerRuntimeCompatibilityError, match="current generated invite"
    ):
        await manager.register_worker(
            {
                "invite": invite.code,
                "runtime": {**_runtime_report(), "protocol_version": 1},
            }
        )
    assert invite.code in manager.invites

    with pytest.raises(WorkerRuntimeCompatibilityError, match="source"):
        await manager.register_worker(
            {
                "invite": invite.code,
                "runtime": {
                    **_runtime_report(),
                    "runtime_kind": "unmanaged_source",
                    "bundle_sha256": "",
                },
            }
        )
    assert invite.code in manager.invites


@pytest.mark.asyncio
async def test_stale_managed_runtime_enrolls_only_for_upgrade(
    tmp_path, monkeypatch
):
    from workgate.remote.bundle import worker_bundle_manifest
    from workgate.remote.manager import RemoteManager

    _configure_remote_state(tmp_path, monkeypatch)
    manager = RemoteManager()
    invite = await manager.create_invite(name="worker-a")

    registered = await manager.register_worker(
        {
            "invite": invite.code,
            "runtime": _runtime_report(digest="0" * 64, version="3.9.0"),
        }
    )

    manifest = worker_bundle_manifest()
    assert registered["upgrade"] == {
        "required": True,
        "version": manifest["bundle_version"],
        "sha256": manifest["sha256"],
        "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
    }
    assert manager.workers["worker-a"].info["runtime_kind"] == (
        "managed_bundle"
    )
    assert manager.workers["worker-a"].info["worker_bundle_sha256"] == (
        "0" * 64
    )


@pytest.mark.asyncio
async def test_resume_rejects_pre_uv_runtime_protocol(tmp_path, monkeypatch):
    from workgate.remote.manager import WorkerRuntimeCompatibilityError

    manager, registered = await _registered_manager(tmp_path, monkeypatch)
    worker = manager.workers[registered["name"]]
    original_info = dict(worker.info)

    with pytest.raises(
        WorkerRuntimeCompatibilityError, match="current generated invite"
    ):
        await manager.resume_worker(
            registered["token"],
            {
                "workdir": str(tmp_path / "should-not-apply"),
                "capabilities": ["files"],
                "info": {"hostname": "stale-edge"},
                "runtime": {**_runtime_report(), "protocol_version": 1},
            },
        )

    assert worker.workdir == str(tmp_path)
    assert worker.info == original_info


@pytest.mark.asyncio
async def test_inventory_exposes_validated_reconnect_command(
    tmp_path, monkeypatch
):
    manager, registered = await _registered_manager(tmp_path, monkeypatch)
    worker = manager.workers[registered["name"]]
    worker.info.update(
        {
            "profile_id": "p_abcdefgh",
            "launcher_path": "/home/user/worker state/run",
        }
    )

    row = manager.list_machines().machines[0]
    reconnect = manager.reconnect_command(registered["name"])

    assert row.profile_id == "p_abcdefgh"
    assert row.reconnect_command == ("'/home/user/worker state/run' p_abcdefgh")
    assert reconnect.model_dump() == {
        "machine": registered["name"],
        "profile_id": "p_abcdefgh",
        "command": "'/home/user/worker state/run' p_abcdefgh",
    }
    assert "access" not in reconnect.command
    assert "invite" not in reconnect.command


@pytest.mark.asyncio
async def test_inventory_formats_windows_reconnect_command(
    tmp_path, monkeypatch
):
    manager, registered = await _registered_manager(tmp_path, monkeypatch)
    worker = manager.workers[registered["name"]]
    launcher = r"C:\Users\Worker Name\state\run.cmd"
    worker.info.update(
        {
            "profile_id": "p_abcdefgh",
            "launcher_path": launcher,
            "platform": "win32",
        }
    )
    expected = subprocess.list2cmdline([launcher, "p_abcdefgh"])

    row = manager.list_machines().machines[0]
    reconnect = manager.reconnect_command(registered["name"])

    assert row.reconnect_command == expected
    assert reconnect.command == expected
    assert "access" not in reconnect.command
    assert "invite" not in reconnect.command


@pytest.mark.asyncio
async def test_inventory_hides_invalid_reconnect_metadata(
    tmp_path, monkeypatch
):
    manager, registered = await _registered_manager(tmp_path, monkeypatch)
    worker = manager.workers[registered["name"]]
    worker.info.update(
        {
            "profile_id": "p_../escape",
            "launcher_path": "/tmp/run\nmalicious",
        }
    )

    row = manager.list_machines().machines[0]

    assert row.profile_id is None
    assert row.reconnect_command is None
    with pytest.raises(ValueError, match="no reconnect profile metadata"):
        manager.reconnect_command(registered["name"])


def _poll_report(*, digest: str, version: str = "3.9.1") -> dict[str, Any]:
    return {
        "protocol_version": 2,
        "runtime_kind": "managed_bundle",
        "worker_version": version,
        "bundle_version": version,
        "bundle_sha256": digest,
    }


@pytest.mark.asyncio
async def test_poll_mismatch_records_report_and_does_not_dequeue(
    tmp_path, monkeypatch
):
    from workgate.remote.bundle import worker_bundle_manifest

    manager, registered = await _registered_manager(tmp_path, monkeypatch)
    worker = manager.workers[registered["name"]]
    worker.queue.put_nowait({"id": "job-1", "tool": "read", "args": {}})

    result = await manager.poll(
        registered["token"], _poll_report(digest="0" * 64)
    )

    manifest = worker_bundle_manifest()
    assert result == {
        "job": None,
        "upgrade": {
            "required": True,
            "version": manifest["bundle_version"],
            "sha256": manifest["sha256"],
            "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
        },
        "poll_timeout_s": 1.0,
    }
    assert worker.queue.qsize() == 1
    assert worker.info["poll_protocol_version"] == 2
    assert worker.info["runtime_kind"] == "managed_bundle"
    assert worker.info["workgate_version"] == "3.9.1"
    assert worker.info["worker_bundle_sha256"] == "0" * 64
    persisted = json.loads(manager._registry_path().read_text(encoding="utf-8"))
    assert persisted["workers"][0]["info"]["poll_protocol_version"] == 2
    assert registered["token"] not in json.dumps(result)


@pytest.mark.asyncio
async def test_poll_matching_digest_delivers_job(tmp_path, monkeypatch):
    from workgate.remote.bundle import worker_bundle_manifest

    manager, registered = await _registered_manager(tmp_path, monkeypatch)
    job = {"id": "job-1", "tool": "read", "args": {}}
    manager.workers[registered["name"]].queue.put_nowait(job)
    manifest = worker_bundle_manifest()

    result = await manager.poll(
        registered["token"],
        _poll_report(
            digest=str(manifest["sha256"]),
            version=str(manifest["bundle_version"]),
        ),
    )

    assert result["job"] == job
    assert result["upgrade"] == {
        "required": False,
        "version": manifest["bundle_version"],
        "sha256": manifest["sha256"],
        "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
    }


@pytest.mark.asyncio
async def test_poll_rejects_legacy_reports_without_dequeuing(
    tmp_path, monkeypatch
):
    from workgate.remote.manager import WorkerRuntimeCompatibilityError

    manager, registered = await _registered_manager(tmp_path, monkeypatch)
    worker = manager.workers[registered["name"]]
    worker.queue.put_nowait({"id": "legacy"})
    with pytest.raises(
        WorkerRuntimeCompatibilityError, match="report required"
    ):
        await manager.poll(registered["token"])
    with pytest.raises(WorkerRuntimeCompatibilityError, match="unsupported"):
        await manager.poll(registered["token"], {"protocol_version": 0})
    with pytest.raises(WorkerRuntimeCompatibilityError, match="unsupported"):
        await manager.poll(registered["token"], {"protocol_version": 1})
    assert worker.queue.qsize() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, message",
    [
        ({"protocol_version": True}, "must be an integer"),
        ({"protocol_version": 3}, "unsupported"),
        ({"protocol_version": 2}, "runtime_kind is required"),
        (
            {"protocol_version": 2, "runtime_kind": "unmanaged_source"},
            "source",
        ),
        (
            {"protocol_version": 2, "runtime_kind": "managed_bundle"},
            "worker_version is required",
        ),
        (
            {
                "protocol_version": 2,
                "runtime_kind": "managed_bundle",
                "worker_version": 3,
            },
            "worker_version must be a string",
        ),
        (
            {
                "protocol_version": 2,
                "runtime_kind": "managed_bundle",
                "worker_version": "3.9.1",
                "bundle_version": "3.9.1",
                "bundle_sha256": "bad",
            },
            "bundle_sha256 is invalid",
        ),
    ],
)
async def test_poll_rejects_malformed_reports(
    tmp_path, monkeypatch, payload, message
):
    manager, registered = await _registered_manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match=message):
        await manager.poll(registered["token"], payload)


def test_poll_endpoint_accepts_empty_body_and_rejects_non_object(monkeypatch):
    from workgate.remote import http

    calls = []

    class Manager:
        async def poll(self, token, payload):
            calls.append((token, payload))
            return {"job": None, "heartbeat": True}

    monkeypatch.setattr(http, "remote_manager", lambda: Manager())
    client = TestClient(Starlette(routes=http.remote_routes()))
    authorization = "B" + "earer worker-token"

    response = client.post(
        "/remote/poll", headers={"Authorization": authorization}
    )
    assert response.status_code == 200
    assert calls == [("worker-token", {})]

    malformed = client.post(
        "/remote/poll",
        content=b"[]",
        headers={"Authorization": authorization},
    )
    assert malformed.status_code == 400
    assert "JSON object" in malformed.json()["message"]


def test_runtime_compatibility_errors_use_conflict_status(monkeypatch):
    from workgate.remote import http
    from workgate.remote.manager import WorkerRuntimeCompatibilityError

    class Manager:
        async def register_worker(self, payload):
            raise WorkerRuntimeCompatibilityError("managed runtime required")

        async def resume_worker(self, token, payload):
            raise WorkerRuntimeCompatibilityError("managed runtime required")

        async def poll(self, token, payload):
            raise WorkerRuntimeCompatibilityError("managed runtime required")

    monkeypatch.setattr(http, "remote_manager", lambda: Manager())
    client = TestClient(Starlette(routes=http.remote_routes()))

    registered = client.post("/remote/register", json={})
    resumed = client.post("/remote/resume", json={})
    polled = client.post("/remote/poll", json={})

    assert registered.status_code == 409
    assert resumed.status_code == 409
    assert polled.status_code == 409
    assert registered.json()["error"] == "WorkerRuntimeCompatibilityError"
    assert resumed.json()["error"] == "WorkerRuntimeCompatibilityError"
    assert polled.json()["error"] == "WorkerRuntimeCompatibilityError"


@pytest.mark.asyncio
async def test_runtime_conflict_does_not_delete_stored_identity(
    tmp_path, monkeypatch
):
    from workgate.remote_worker import worker

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    identity = {
        "server": "https://controller.test",
        "name": "worker-a",
        "access": "secret-access",
        "workdir": str(tmp_path),
    }
    worker._write_worker_identity(identity)

    def reject(*args, **kwargs):
        raise worker.WorkerHttpError(
            "https://controller.test/remote/resume",
            409,
            "managed runtime required",
        )

    monkeypatch.setattr(worker, "_worker_post_json", reject)

    with pytest.raises(worker.WorkerHttpError) as exc_info:
        await worker._worker_resume_or_none(
            "https://controller.test/remote/resume",
            {},
            {},
        )

    assert exc_info.value.status_code == 409
    assert worker.load_worker_identity() == identity


def test_worker_bundle_manifest_is_deterministic_bounded_and_no_store():
    from workgate.remote import bundle
    from workgate.remote.http import remote_routes

    bundle.worker_bundle_bytes.cache_clear()
    bundle.worker_bundle_manifest.cache_clear()
    first = bundle.worker_bundle_bytes()
    bundle.worker_bundle_bytes.cache_clear()
    second = bundle.worker_bundle_bytes()
    manifest = bundle.worker_bundle_manifest()

    assert first == second
    assert manifest["sha256"] == hashlib.sha256(first).hexdigest()
    assert manifest["size"] == len(first)
    assert manifest["url"].endswith(f"?sha256={manifest['sha256']}")

    client = TestClient(Starlette(routes=remote_routes()))
    manifest_response = client.get("/remote/worker-bundle.tgz?manifest=1")
    bundle_response = client.get(str(manifest["url"]))
    assert manifest_response.status_code == 200
    assert manifest_response.headers["cache-control"] == "no-store"
    assert bundle_response.headers["cache-control"] == "no-store"
    assert bundle_response.content == first


def test_worker_bundle_keeps_strict_runtime_allowlist():
    from workgate.remote.bundle import worker_bundle_bytes

    with tarfile.open(
        fileobj=io.BytesIO(worker_bundle_bytes()), mode="r:gz"
    ) as tar:
        names = set(tar.getnames())

    assert "workgate/remote_worker/runtime.py" in names
    assert "workgate/remote_worker/lifecycle.py" in names
    assert "workgate/remote_worker/profiles.py" in names
    assert "workgate/remote_worker/state.py" in names
    assert "workgate/remote_worker/worker.py" in names
    assert "workgate/executor/__init__.py" in names
    assert "workgate/executor/config.py" in names
    assert "workgate/executor/runtime.py" in names
    assert "workgate/executor/search_composition.py" in names
    assert not any(name.startswith("workgate/control/") for name in names)
    assert "workgate/audit/__init__.py" in names
    assert "workgate/audit/core.py" in names
    assert "workgate/audit/payloads.py" in names
    assert "workgate/ops/patch/__init__.py" in names
    assert "workgate/ops/patch/envelope.py" in names
    assert "workgate/version.py" in names
    assert "workgate/terminal/__init__.py" in names
    assert "workgate/terminal/conpty.py" in names
    assert "workgate/telemetry/__init__.py" in names
    assert "workgate/telemetry/system.py" in names
    assert "workgate/ui/__init__.py" in names
    assert "workgate/ui/contracts.py" in names
    assert "workgate/ui/dashboard.py" in names
    assert "workgate/terminal/bridge.py" in names
    assert "workgate/terminal/contracts.py" in names
    assert "workgate/terminal/tmux.py" in names
    assert "workgate/ops/agent.py" in names
    assert "workgate/agent_bridge/skills.py" in names
    assert "workgate/agent_bridge/sources.py" in names
    assert "workgate/agent_bridge/models.py" in names
    assert "workgate/jobs/reconciliation.py" in names
    assert "workgate/jobs/runner_bootstrap.py" in names
    assert "workgate/jobs/runtime.py" in names
    assert "workgate/persistence/__init__.py" in names
    assert "workgate/persistence/store.py" in names
    assert "workgate/agent_bridge/status.py" not in names
    assert "workgate/config/cli.py" not in names
    assert "workgate/jobs/cli.py" not in names
    assert not any(name.startswith("workgate/tools/") for name in names)
    assert "workgate/schemas/result_models/version.py" not in names
    assert "workgate/utils/path_locks.py" in names
    assert "workgate/utils/private_files.py" in names
    assert "workgate/utils/processes.py" in names
    assert "workgate/remote/manager.py" not in names
    assert "workgate/remote/http.py" not in names
    assert "workgate/remote/service.py" not in names
    assert not any(
        name.startswith("workgate/ui/static/")
        or "ui_static" in name
        or name.startswith("tests/")
        for name in names
    )


def test_worker_bundle_imports_without_checkout_fallback(tmp_path):
    from workgate.remote.bundle import worker_bundle_bytes

    archive = tmp_path / "worker.tgz"
    runtime = tmp_path / "runtime"
    archive.write_bytes(worker_bundle_bytes())
    with tarfile.open(archive, mode="r:gz") as tar:
        tar.extractall(runtime, filter="data")

    paths = sysconfig.get_paths()
    dependencies = sorted({paths["purelib"], paths["platlib"]})
    modules = [
        "workgate.remote_worker.__main__",
        "workgate.audit",
        "workgate.telemetry.system",
        "workgate.ui.dashboard",
        "workgate.terminal.bridge",
        "workgate.terminal.tmux",
        "workgate.ops.session",
        "workgate.persistence",
        "workgate.persistence.store",
        "workgate.ops.agent",
        "workgate.agent_bridge.sources",
        "workgate.schemas.result_models.agent",
        "workgate.ops.shell",
        "workgate.jobs.reconciliation",
        "workgate.jobs.runtime",
        "workgate.ops.todo",
        "workgate.ops.files",
        "workgate.ops.read",
        "workgate.ops.search",
        "workgate.ops.secret_scan",
        "workgate.ops.transfer",
    ]
    code = """
import importlib
import json
import sys

runtime, dependencies, modules = sys.argv[1:]
sys.path[:] = [runtime, *json.loads(dependencies), *[
    item for item in sys.path
    if item and "site-packages" not in item and "workgate" not in item
]]
for name in json.loads(modules):
    importlib.import_module(name)
print("worker-bundle-imports-ok")
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            code,
            str(runtime),
            json.dumps(dependencies),
            json.dumps(modules),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "worker-bundle-imports-ok"


def test_worker_bundle_job_runner_executes_without_checkout_or_site(tmp_path):
    from workgate.remote.bundle import worker_bundle_bytes

    archive = tmp_path / "worker.tgz"
    runtime = tmp_path / "runtime"
    archive.write_bytes(worker_bundle_bytes())
    with tarfile.open(archive, mode="r:gz") as tar:
        tar.extractall(runtime, filter="data")

    command_path = tmp_path / "command.txt"
    log_path = tmp_path / "job.log"
    status_path = tmp_path / "status.json"
    if os.name == "nt":
        shell = os.environ.get("COMSPEC") or "cmd.exe"
        command = "echo worker-bundle-runner-ok"
    else:
        shell = "/bin/sh"
        command = "printf 'worker-bundle-runner-ok\\n'"
    command_path.write_text(command, encoding="utf-8")

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    bootstrap = runtime / "workgate" / "jobs" / "runner_bootstrap.py"
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            str(bootstrap),
            "--command-file",
            str(command_path),
            "--log-file",
            str(log_path),
            "--status-file",
            str(status_path),
            "--cwd",
            str(tmp_path),
            "--shell",
            shell,
            "--max-log-bytes",
            "1024",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert log_path.read_text(encoding="utf-8").strip() == (
        "worker-bundle-runner-ok"
    )
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["exit_code"] == 0
    assert status["error"] is None
    assert status["output_bytes"] > 0


def _runtime_archive_bytes(
    *,
    extra_members: list[tarfile.TarInfo] | None = None,
    overrides: dict[str, bytes] | None = None,
) -> bytes:
    from workgate.remote_worker import runtime

    replacements = overrides or {}
    project_root = Path(__file__).resolve().parents[1]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relative in runtime._REQUIRED_RUNTIME_FILES:
            if relative in replacements:
                payload = replacements[relative]
            elif relative in {"pyproject.toml", "uv.lock"}:
                payload = (project_root / relative).read_bytes()
            else:
                payload = f"# {relative}\n".encode()
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
        for relative, payload in replacements.items():
            if relative in runtime._REQUIRED_RUNTIME_FILES:
                continue
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
        for info in extra_members or []:
            data = b"x" if info.isreg() else None
            if data is not None:
                info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


@pytest.mark.parametrize("kind", ["traversal", "symlink", "hardlink"])
def test_safe_extract_rejects_unsafe_archive_members(tmp_path, kind):
    from workgate.remote_worker import runtime

    if kind == "traversal":
        member = tarfile.TarInfo("../escape.py")
    elif kind == "symlink":
        member = tarfile.TarInfo("workgate/link.py")
        member.type = tarfile.SYMTYPE
        member.linkname = "/tmp/target"
    else:
        member = tarfile.TarInfo("workgate/hard.py")
        member.type = tarfile.LNKTYPE
        member.linkname = "workgate/__init__.py"
    archive = tmp_path / "worker.tgz"
    archive.write_bytes(_runtime_archive_bytes(extra_members=[member]))

    with pytest.raises(ValueError, match="unsafe|unsupported"):
        runtime.safe_extract_bundle(archive, tmp_path / "runtime")
    assert not (tmp_path / "escape.py").exists()


def test_fetch_bytes_bypasses_cache_and_rejects_cross_origin(monkeypatch):
    from workgate.remote_worker import runtime

    captured = {}

    class Response:
        headers = {"Content-Length": "2"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return "https://controller.test/bundle"

        def read(self, size):
            captured["read_size"] = size
            return b"ok"

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *handlers: Opener()
    )
    result = runtime._fetch_bytes(
        "https://controller.test/bundle",
        server="https://controller.test",
        timeout=12,
        max_bytes=2,
    )
    request = captured["request"]
    assert result == b"ok"
    assert request.get_header("Cache-control") == "no-cache"
    assert request.get_header("Pragma") == "no-cache"
    assert request.get_header("User-agent") == (
        f"workgate-worker/{runtime.__version__}"
    )
    assert captured["read_size"] == 3

    with pytest.raises(ValueError, match="controller origin"):
        runtime._fetch_bytes(
            "https://attacker.test/bundle",
            server="https://controller.test",
            timeout=12,
            max_bytes=2,
        )


def test_worker_update_redirect_preserves_user_agent_and_origin():
    from workgate.remote_worker import runtime

    user_agent = f"workgate-worker/{runtime.__version__}"
    request = urllib.request.Request(
        "https://controller.test/manifest",
        headers={"User-Agent": user_agent},
    )
    handler = runtime._SameOriginRedirectHandler("https://controller.test")

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "/bundle",
    )
    assert redirected is not None
    assert redirected.full_url == "https://controller.test/bundle"
    assert redirected.get_header("User-agent") == user_agent

    with pytest.raises(ValueError, match="controller origin"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.test/bundle",
        )


def test_manifest_requires_matching_version_digest_and_same_origin(monkeypatch):
    from workgate.remote_worker import runtime

    digest = "a" * 64

    def payload(manifest):
        return json.dumps(manifest).encode()

    monkeypatch.setattr(
        runtime,
        "_fetch_bytes",
        lambda *args, **kwargs: payload(
            {
                "schema_version": 1,
                "bundle_version": "3.9.1",
                "sha256": digest,
                "size": 10,
                "url": f"/remote/worker-bundle.tgz?sha256={digest}",
            }
        ),
    )
    manifest = runtime.fetch_manifest(
        "https://controller.test",
        manifest_path="/remote/worker-bundle.tgz?manifest=1",
        expected_version="3.9.1",
        expected_digest=digest,
    )
    assert manifest["url"].startswith("https://controller.test/")

    with pytest.raises(ValueError, match="does not match"):
        runtime.fetch_manifest(
            "https://controller.test",
            manifest_path="/remote/worker-bundle.tgz?manifest=1",
            expected_version="3.9.2",
            expected_digest=digest,
        )

    monkeypatch.setattr(
        runtime,
        "_fetch_bytes",
        lambda *args, **kwargs: payload(
            {
                "schema_version": 1,
                "bundle_version": "3.9.1",
                "sha256": digest,
                "size": 10,
                "url": f"https://attacker.test/bundle?sha256={digest}",
            }
        ),
    )
    with pytest.raises(ValueError, match="controller origin"):
        runtime.fetch_manifest(
            "https://controller.test",
            manifest_path="/remote/worker-bundle.tgz?manifest=1",
            expected_version="3.9.1",
            expected_digest=digest,
        )


def _mock_runtime_download(monkeypatch, payload: bytes, version: str):
    from workgate.remote_worker import runtime

    def fake_sync_runtime_environment(runtime_path):
        python = runtime.runtime_python_path(runtime_path)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"")

    monkeypatch.setattr(
        runtime, "_sync_runtime_environment", fake_sync_runtime_environment
    )
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        runtime,
        "fetch_manifest",
        lambda *args, **kwargs: {
            "schema_version": 1,
            "bundle_version": version,
            "sha256": digest,
            "size": len(payload),
            "url": f"https://controller.test/bundle?sha256={digest}",
        },
    )
    monkeypatch.setattr(
        runtime, "_fetch_bytes", lambda *args, **kwargs: payload
    )
    return digest


def test_install_runtime_is_transactional_and_short_circuits(
    tmp_path, monkeypatch
):
    from workgate.remote_worker import runtime

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    payload = _runtime_archive_bytes()
    digest = _mock_runtime_download(monkeypatch, payload, "3.9.1")
    instruction = {
        "version": "3.9.1",
        "sha256": digest,
        "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
    }

    installed = runtime.install_runtime(
        "https://controller.test", instruction, current_version="3.9.0"
    )
    assert installed["updated"] is True
    assert runtime.runtime_identity(digest) == {
        "sha256": digest,
        "bundle_version": "3.9.1",
    }
    assert (
        runtime.worker_runtime_dir_for_digest(digest)
        / "workgate/remote_worker/worker.py"
    ).is_file()

    monkeypatch.setattr(
        runtime,
        "fetch_manifest",
        lambda *args, **kwargs: pytest.fail("downloaded"),
    )
    same = runtime.install_runtime(
        "https://controller.test", instruction, current_version="3.9.1"
    )
    assert same["updated"] is False


def test_worker_runtime_sync_uses_own_uv_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workgate.remote_worker import runtime

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("requires uv to verify the managed worker environment")
    managed = tmp_path / "runtime"
    managed.mkdir()
    project_root = Path(__file__).resolve().parents[1]
    shutil.copy2(project_root / "pyproject.toml", managed / "pyproject.toml")
    shutil.copy2(project_root / "uv.lock", managed / "uv.lock")
    monkeypatch.setattr(runtime, "worker_uv_path", lambda: Path(uv))

    runtime._sync_runtime_environment(managed)

    python = runtime.runtime_python_path(managed)
    assert python.is_file()
    completed = subprocess.run(
        [
            str(python),
            "-c",
            "import pathspec, pydantic, pydantic_settings, yaml",
        ],
        cwd=managed,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_current_runtime_identity_requires_executing_managed_module(
    tmp_path, monkeypatch
):
    from workgate.remote_worker import runtime
    from workgate.remote_worker.state import WORKER_RUNTIME_DIGEST_ENV

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    payload = _runtime_archive_bytes()
    digest = _mock_runtime_download(monkeypatch, payload, "3.9.1")
    runtime.install_runtime(
        "https://controller.test",
        {
            "version": "3.9.1",
            "sha256": digest,
            "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
        },
        current_version="3.9.0",
    )
    monkeypatch.setenv(WORKER_RUNTIME_DIGEST_ENV, digest)
    selected_module = (
        runtime.worker_runtime_dir_for_digest(digest)
        / runtime._RUNTIME_MODULE_PATH
    )
    selected_module.parent.mkdir(parents=True, exist_ok=True)
    selected_module.write_text("# managed runtime module\n", encoding="utf-8")

    assert runtime.runtime_identity(digest)["sha256"] == digest
    assert runtime.current_runtime_identity() == {
        "sha256": "",
        "bundle_version": "",
    }

    monkeypatch.setattr(runtime, "__file__", str(selected_module))
    assert runtime.current_runtime_identity() == {
        "sha256": digest,
        "bundle_version": "3.9.1",
    }


def test_install_failure_restores_old_runtime_and_metadata(
    tmp_path, monkeypatch
):
    from workgate.remote_worker import runtime

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    payload = _runtime_archive_bytes()
    digest = _mock_runtime_download(monkeypatch, payload, "3.9.1")
    old_runtime = runtime.worker_runtime_dir_for_digest(digest)
    old_runtime.mkdir(parents=True)
    (old_runtime / "old.txt").write_text("old", encoding="utf-8")
    old_metadata = b'{"old": true}'
    runtime.runtime_metadata_path(digest).write_bytes(old_metadata)
    monkeypatch.setattr(
        runtime,
        "_write_runtime_metadata",
        lambda data, digest=None: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        runtime.install_runtime(
            "https://controller.test",
            {
                "version": "3.9.1",
                "sha256": digest,
                "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
            },
            current_version="3.9.0",
        )

    assert (old_runtime / "old.txt").read_text(encoding="utf-8") == "old"
    assert runtime.runtime_metadata_path(digest).read_bytes() == old_metadata
    assert not list((tmp_path / "runtimes").glob("*.previous.*"))


def test_install_rejects_digest_mismatch_and_downgrade(tmp_path, monkeypatch):
    from workgate.remote_worker import runtime

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    payload = _runtime_archive_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    corrupted = payload[:-1] + bytes([payload[-1] ^ 1])
    monkeypatch.setattr(
        runtime,
        "fetch_manifest",
        lambda *args, **kwargs: {
            "schema_version": 1,
            "bundle_version": "3.9.1",
            "sha256": digest,
            "size": len(payload),
            "url": f"https://controller.test/bundle?sha256={digest}",
        },
    )
    monkeypatch.setattr(
        runtime, "_fetch_bytes", lambda *args, **kwargs: corrupted
    )
    with pytest.raises(ValueError, match="checksum"):
        runtime.install_runtime(
            "https://controller.test",
            {
                "version": "3.9.1",
                "sha256": digest,
                "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
            },
            current_version="3.9.0",
        )

    with pytest.raises(ValueError, match="downgrade"):
        runtime.install_runtime(
            "https://controller.test",
            {
                "version": "3.9.0",
                "sha256": "b" * 64,
                "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
            },
            current_version="3.9.1",
        )


def test_reexec_argv_and_pythonpath_are_safe(tmp_path, monkeypatch):
    from workgate.remote_worker import runtime

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    runtime_dir = runtime.worker_runtime_dir()
    runtime_python = runtime.runtime_python_path(runtime_dir)
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"")
    runtime_path = str(runtime_dir)
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(["/old", runtime_path, "/old"]),
    )
    environment = runtime.reexec_environment()
    assert environment["PYTHONPATH"] == runtime_path

    argv = runtime.worker_reexec_argv()
    assert argv == [
        str(runtime_python),
        "-m",
        "workgate.remote_worker",
        "run",
    ]
    assert "--invite" not in argv
    assert "token" not in " ".join(argv).lower()


def test_reexec_uses_selected_runtime_cwd_on_all_platforms(
    tmp_path, monkeypatch
):
    from workgate.remote_worker import runtime

    old_digest = "a" * 64
    new_digest = "b" * 64
    old_runtime = tmp_path / "runtimes" / old_digest
    new_runtime = tmp_path / "runtimes" / new_digest
    old_runtime.mkdir(parents=True)
    new_runtime.mkdir(parents=True)
    runtime_python = runtime.runtime_python_path(new_runtime)
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"")
    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_WORKER_RUNTIME_SHA256", new_digest)
    monkeypatch.chdir(old_runtime)

    captured = {}
    monkeypatch.setattr(runtime.sys, "platform", "linux")

    def execve(executable, argv, env):
        captured.update(
            executable=executable,
            argv=argv,
            env=env,
            cwd=Path.cwd(),
        )

    monkeypatch.setattr(runtime.os, "execve", execve)
    runtime.reexec_worker()
    assert captured["argv"][0] == str(runtime_python)
    assert captured["cwd"] == new_runtime
    assert captured["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(
        new_runtime
    )
    assert Path.cwd() == old_runtime

    spawned = {}
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(
        runtime.subprocess,
        "Popen",
        lambda argv, **kwargs: spawned.update(argv=argv, kwargs=kwargs),
    )
    with pytest.raises(SystemExit) as exc_info:
        runtime.reexec_worker()
    assert exc_info.value.code == 0
    assert spawned["kwargs"]["cwd"] == new_runtime
    assert spawned["kwargs"]["close_fds"] is False


@pytest.mark.asyncio
async def test_worker_processes_enrollment_upgrade_before_poll(
    monkeypatch, tmp_path
):
    import workgate.remote_worker.worker as worker

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        worker, "_configure_worker_runtime_env", lambda workdir: None
    )
    monkeypatch.setattr(
        worker,
        "_read_worker_identity",
        lambda server, name: {
            "server": server,
            "name": "worker-a",
            "access": "secret-access",
        },
    )
    monkeypatch.setattr(worker, "_write_worker_identity", lambda data: None)

    async def resume(*args, **kwargs):
        return {
            "ok": True,
            "data": {
                "name": "worker-a",
                "upgrade": {
                    "required": True,
                    "version": "3.9.2",
                    "sha256": "a" * 64,
                    "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
                },
            },
        }

    async def unexpected_poll(*args, **kwargs):
        raise AssertionError("poll started before enrollment upgrade")

    async def upgrade(*args, **kwargs):
        raise SystemExit(0)

    monkeypatch.setattr(worker, "_worker_resume_or_none", resume)
    monkeypatch.setattr(worker, "_worker_post_json_forever", unexpected_poll)
    monkeypatch.setattr(worker, "_install_and_reexec_worker", upgrade)

    with pytest.raises(SystemExit):
        await worker.run_worker(
            "https://controller.test", "", "worker-a", str(tmp_path)
        )


@pytest.mark.asyncio
async def test_worker_processes_required_upgrade_before_job(
    monkeypatch, tmp_path
):
    import workgate.remote_worker.worker as worker

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        worker, "_configure_worker_runtime_env", lambda workdir: None
    )
    monkeypatch.setattr(
        worker,
        "_read_worker_identity",
        lambda server, name: {
            "server": server,
            "name": "worker-a",
            "access": "secret-access",
        },
    )
    monkeypatch.setattr(worker, "_write_worker_identity", lambda data: None)

    async def resume(*args, **kwargs):
        return {
            "ok": True,
            "data": {"name": "worker-a", "heartbeat_interval_s": 15},
        }

    poll_payloads = []

    async def post(
        url, payload, headers=None, timeout=None, operation="request"
    ):
        if url.endswith("/poll"):
            poll_payloads.append(payload)
            return {
                "ok": True,
                "data": {
                    "upgrade": {
                        "required": True,
                        "version": "3.9.1",
                        "sha256": "a" * 64,
                        "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
                    },
                    "job": {"id": "must-not-run", "tool": "read", "args": {}},
                },
            }
        raise AssertionError(url)

    async def upgrade(*args, **kwargs):
        raise SystemExit(0)

    monkeypatch.setattr(worker, "_worker_resume_or_none", resume)
    monkeypatch.setattr(worker, "_worker_post_json_forever", post)
    monkeypatch.setattr(worker, "_install_and_reexec_worker", upgrade)
    monkeypatch.setattr(
        worker,
        "execute_worker_tool",
        lambda *args, **kwargs: pytest.fail("job executed before upgrade"),
    )

    with pytest.raises(SystemExit):
        await worker.run_worker(
            "https://controller.test", "", "worker-a", str(tmp_path)
        )
    assert poll_payloads[0]["protocol_version"] == 2
    assert poll_payloads[0]["runtime_kind"] in {
        "managed_bundle",
        "unmanaged_source",
    }
    assert poll_payloads[0]["worker_version"]
    assert "secret-access" not in json.dumps(poll_payloads)


@pytest.mark.asyncio
async def test_upgrade_retry_is_capped_and_resets_after_successful_poll(
    monkeypatch, tmp_path
):
    import workgate.remote_worker.worker as worker

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        worker, "_configure_worker_runtime_env", lambda workdir: None
    )
    monkeypatch.setattr(
        worker,
        "_read_worker_identity",
        lambda server, name: {
            "server": server,
            "name": "worker-a",
            "access": "access",
        },
    )
    monkeypatch.setattr(worker, "_write_worker_identity", lambda data: None)

    async def resume(*args, **kwargs):
        return {"ok": True, "data": {"name": "worker-a"}}

    responses = iter(
        [
            {"upgrade": {"required": True}},
            {"upgrade": {"required": False}, "job": None},
            {"upgrade": {"required": True}},
            {"upgrade": {"required": True}},
        ]
    )

    async def post(
        url, payload, headers=None, timeout=None, operation="request"
    ):
        return {"ok": True, "data": next(responses)}

    attempts = 0

    async def upgrade(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            raise SystemExit(0)
        raise RuntimeError("broken package")

    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(worker, "_worker_resume_or_none", resume)
    monkeypatch.setattr(worker, "_worker_post_json_forever", post)
    monkeypatch.setattr(worker, "_install_and_reexec_worker", upgrade)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    with pytest.raises(SystemExit):
        await worker.run_worker(
            "https://controller.test", "", "worker-a", str(tmp_path)
        )
    assert sleeps == [1.0, 1.0]
    assert worker._worker_retry_delay(100) == 30.0


def test_join_script_installs_persistent_verified_runtime():
    from importlib import resources

    script = (
        resources.files("workgate.remote")
        .joinpath("join_worker.sh")
        .read_text(encoding="utf-8")
    )
    assert 'RUNTIME_DIR="$DATA_DIR/runtimes/$RUNTIME_DIGEST"' in script
    assert 'RUNTIME_METADATA="$RUNTIME_DIR/runtime.json"' in script
    assert 'name = "run.cmd" if os.name == "nt" else "run"' in script
    assert "select_launcher_path" in script
    assert "prepare_profile" in script
    assert "configure_runtime" in script
    assert "runtime_is_installed" in script
    assert "write_profile_metadata" in script
    assert "install_launcher" in script
    assert "from workgate.remote_worker.profile_launcher import" in script
    assert "ensure_profile_launcher(sys.argv[1])" in script
    assert "Reusing worker runtime" in script
    assert "?manifest=1" in script
    assert "Cache-Control: no-cache" in script
    assert "sha256" in script
    assert "member.isreg()" in script
    assert "os.replace(staging, runtime)" in script
    assert 'export WORKGATE_WORKER_STATE_DIR="$STATE_DIR"' in script
    assert 'export WORKGATE_WORKER_DATA_DIR="$DATA_DIR"' in script
    assert 'export WORKGATE_WORKER_RUNTIME_SHA256="$RUNTIME_DIGEST"' in script
    assert 'export PYTHONPATH="$RUNTIME_DIR"' in script
    assert "${PYTHONPATH:+:$PYTHONPATH}" not in script
    assert 'PYTHONPATH="$RUNTIME_DIR" \\' in script
    assert (
        'STATE_DIR="$(prepare_private_persistent_dir "$STATE_DIR" "worker state")"'
        in script
    )
    assert (
        'DATA_DIR="$(prepare_private_persistent_dir "$DATA_DIR" "worker data")"'
        in script
    )
    assert (
        'prepare_private_persistent_dir "$STATE_DIR/profiles" "worker profiles"'
        in script
    )
    assert (
        'prepare_private_persistent_dir "$PROFILE_DIR" "worker profile"'
        in script
    )
    assert '"workgate/app_paths.py"' in script
    assert 'cd "$RUNTIME_DIR"' in script
    assert 'ARGS=(connect --server "$SERVER" --invite "$INVITE"' in script
    assert '--profile "$PROFILE_ID"' in script
    assert '"$PROFILE_DIR/worker.log"' in script
    assert '"runtime_sha256": digest' in script
    assert "--persist" not in script
    assert "ARGS=(--server" not in script
    assert 'rm -rf "$RUNTIME_DIR"' not in script


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_join_script_private_persistent_dir_rejects_symlink_and_repairs_mode(
    tmp_path: Path,
) -> None:
    from importlib import resources

    script = (
        resources.files("workgate.remote")
        .joinpath("join_worker.sh")
        .read_text(encoding="utf-8")
        .replace("__REMOTE_SERVER__", "https://controller.test")
        .replace("__REMOTE_WORKER_BUNDLE_PATH__", "/remote/worker-bundle.tgz")
    )
    prefix = script.split("parse_args() {", maxsplit=1)[0]

    target = tmp_path / "target"
    target.mkdir()
    state_link = tmp_path / "state-link"
    state_link.symlink_to(target, target_is_directory=True)
    rejected = subprocess.run(
        [
            "bash",
            "-c",
            prefix
            + '\nprepare_private_persistent_dir "$TEST_DIR" "worker state"\n',
        ],
        env={**os.environ, "TEST_DIR": str(state_link)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "worker state directory is unsafe" in rejected.stderr

    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o755)
    private_dir.chmod(0o755)
    repaired = subprocess.run(
        [
            "bash",
            "-c",
            prefix
            + '\nprepare_private_persistent_dir "$TEST_DIR" "worker data"\n',
        ],
        env={**os.environ, "TEST_DIR": str(private_dir)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert repaired.returncode == 0, repaired.stderr
    assert Path(repaired.stdout.strip()) == private_dir.resolve()
    assert private_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("windows_env", "expected_base"),
    [
        (
            {"LOCALAPPDATA": "C:/Users/Test/AppData/Local"},
            "C:/Users/Test/AppData/Local",
        ),
        (
            {"USERPROFILE": "C:/Users/Test"},
            "C:/Users/Test/AppData/Local",
        ),
    ],
)
def test_join_script_uses_native_windows_worker_namespaces(
    tmp_path: Path,
    windows_env: dict[str, str],
    expected_base: str,
) -> None:
    from importlib import resources

    script = (
        resources.files("workgate.remote")
        .joinpath("join_worker.sh")
        .read_text(encoding="utf-8")
        .replace("__REMOTE_SERVER__", "https://controller.test")
        .replace(
            "__REMOTE_WORKER_BUNDLE_PATH__",
            "/remote/worker-bundle.tgz",
        )
    )
    prefix = script.split("parse_args() {", maxsplit=1)[0]
    probe = (
        prefix
        + """\
uname() { printf 'MINGW64_NT-10.0\\n'; }
system_tmpdir_is_suitable() { [ "$1" = 'C:/Temp' ]; }
configure_app_dirs
normalize_system_tmpdir
printf '%s\\n%s\\n%s\\n' "$STATE_DIR" "$DATA_DIR" "$SYSTEM_TMPDIR"
"""
    )

    if os.name == "nt":
        bash = None
        git = shutil.which("git")
        if git:
            git_bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if git_bash.is_file():
                bash = str(git_bash)
    else:
        bash = shutil.which("bash")
    if not bash:
        pytest.skip("requires bash to exercise the bootstrap path policy")

    env = os.environ.copy()
    for name in (
        "LOCALAPPDATA",
        "USERPROFILE",
        "WORKGATE_WORKER_STATE_DIR",
        "WORKGATE_WORKER_DATA_DIR",
        "TMPDIR",
        "TEMP",
        "TMP",
    ):
        env.pop(name, None)
    env.update(windows_env)
    env["TEMP"] = "C:/Temp"

    completed = subprocess.run(
        [bash, "-s"],
        input=probe,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        f"{expected_base}/workgate/state/worker",
        f"{expected_base}/workgate/data/worker",
        "C:/Temp",
    ]


def test_join_script_windows_temp_candidates_follow_cpython_order(
    tmp_path: Path,
) -> None:
    from importlib import resources

    script = (
        resources.files("workgate.remote")
        .joinpath("join_worker.sh")
        .read_text(encoding="utf-8")
        .replace("__REMOTE_SERVER__", "https://controller.test")
        .replace(
            "__REMOTE_WORKER_BUNDLE_PATH__",
            "/remote/worker-bundle.tgz",
        )
    )
    prefix = script.split("parse_args() {", maxsplit=1)[0]
    attempts = tmp_path / "attempts.txt"
    probe = (
        prefix
        + """\
uname() { printf 'MINGW64_NT-10.0\\n'; }
system_tmpdir_is_suitable() {
  printf '%s\\n' "$1" >> "$ATTEMPTS"
  [ "$1" = 'C:/Users/Test/AppData/Local/Temp' ]
}
normalize_system_tmpdir
printf '%s\\n' "$SYSTEM_TMPDIR"
"""
    )

    if os.name == "nt":
        bash = None
        git = shutil.which("git")
        if git:
            git_bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if git_bash.is_file():
                bash = str(git_bash)
    else:
        bash = shutil.which("bash")
    if not bash:
        pytest.skip("requires bash to exercise the bootstrap path policy")

    env = os.environ.copy()
    env.update(
        {
            "TMPDIR": "C:/env-tmpdir",
            "TEMP": "C:/env-temp",
            "TMP": "C:/env-tmp",
            "USERPROFILE": "C:/Users/Test",
            "SYSTEMROOT": "C:/Windows",
            "ATTEMPTS": str(attempts),
        }
    )
    completed = subprocess.run(
        [bash, "-s"],
        input=probe,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "C:/Users/Test/AppData/Local/Temp"
    assert attempts.read_text(encoding="utf-8").splitlines() == [
        "C:/env-tmpdir",
        "C:/env-temp",
        "C:/env-tmp",
        "C:/Users/Test/AppData/Local/Temp",
    ]


@pytest.mark.skipif(
    os.name != "nt",
    reason="compares Git Bash bootstrap fallback with native Windows CPython",
)
def test_join_script_windows_temp_fallback_matches_cpython(
    tmp_path: Path,
) -> None:
    from importlib import resources

    git = shutil.which("git")
    if not git:
        pytest.skip("requires Git Bash to exercise Windows bootstrap fallback")
    git_bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
    if not git_bash.is_file():
        pytest.skip("requires Git Bash to exercise Windows bootstrap fallback")

    script = (
        resources.files("workgate.remote")
        .joinpath("join_worker.sh")
        .read_text(encoding="utf-8")
        .replace("__REMOTE_SERVER__", "https://controller.test")
        .replace(
            "__REMOTE_WORKER_BUNDLE_PATH__",
            "/remote/worker-bundle.tgz",
        )
    )
    prefix = script.split("parse_args() {", maxsplit=1)[0]
    probe = (
        prefix
        + """\
normalize_system_tmpdir
create_bootstrap_tmpdir
printf '%s\\n%s\\n' "$SYSTEM_TMPDIR" "$TMPDIR"
rm -rf "$TMPDIR"
"""
    )
    invalid = tmp_path / "does-not-exist"
    env = os.environ.copy()
    env["TMPDIR"] = str(invalid)
    env.pop("TEMP", None)
    env.pop("TMP", None)

    shell = subprocess.run(
        [str(git_bash), "-s"],
        input=probe,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    python = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )

    assert shell.returncode == 0, shell.stderr
    system_tmp, bootstrap_tmp = shell.stdout.splitlines()
    expected_tmp = Path(python.stdout.strip()).resolve()
    assert Path(system_tmp).resolve() == expected_tmp
    assert Path(bootstrap_tmp).parent.resolve() == expected_tmp


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX TMPDIR semantics")
def test_join_script_normalizes_relative_system_tmpdir(tmp_path: Path) -> None:
    from importlib import resources

    script = (
        resources.files("workgate.remote")
        .joinpath("join_worker.sh")
        .read_text(encoding="utf-8")
        .replace("__REMOTE_SERVER__", "https://controller.test")
        .replace(
            "__REMOTE_WORKER_BUNDLE_PATH__",
            "/remote/worker-bundle.tgz",
        )
    )
    prefix = script.split("parse_args() {", maxsplit=1)[0]
    relative_tmp = tmp_path / "relative-tmp"
    relative_tmp.mkdir()
    probe = (
        prefix
        + "\nnormalize_system_tmpdir\nprintf '%s\\n' \"$SYSTEM_TMPDIR\"\n"
    )

    completed = subprocess.run(
        ["bash", "-s"],
        input=probe,
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": "relative-tmp"},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()) == relative_tmp.resolve()


@pytest.mark.skipif(
    os.name == "nt", reason="exercises POSIX temp probe semantics"
)
def test_join_script_temp_suitability_requires_a_real_create_probe(
    tmp_path: Path,
) -> None:
    from importlib import resources

    script = (
        resources.files("workgate.remote")
        .joinpath("join_worker.sh")
        .read_text(encoding="utf-8")
        .replace("__REMOTE_SERVER__", "https://controller.test")
        .replace(
            "__REMOTE_WORKER_BUNDLE_PATH__",
            "/remote/worker-bundle.tgz",
        )
    )
    prefix = script.split("parse_args() {", maxsplit=1)[0]
    rejected = tmp_path / "rejected"
    accepted = tmp_path / "accepted"
    rejected.mkdir()
    accepted.mkdir()
    probe = (
        prefix
        + """\
mktemp() {
  case "$1" in
    "$REJECTED"/*) return 1 ;;
    *) command mktemp "$@" ;;
  esac
}
normalize_system_tmpdir
printf '%s\\n' "$SYSTEM_TMPDIR"
"""
    )
    env = os.environ.copy()
    env.update(
        {
            "TMPDIR": str(rejected),
            "TEMP": str(accepted),
            "REJECTED": str(rejected),
        }
    )
    env.pop("TMP", None)

    completed = subprocess.run(
        ["bash", "-s"],
        input=probe,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()).resolve() == accepted.resolve()


@pytest.mark.skipif(
    os.name == "nt", reason="exercises POSIX temp fallback semantics"
)
def test_join_script_falls_back_from_unsuitable_tmpdir_like_python(
    tmp_path: Path,
) -> None:
    from importlib import resources

    script = (
        resources.files("workgate.remote")
        .joinpath("join_worker.sh")
        .read_text(encoding="utf-8")
        .replace("__REMOTE_SERVER__", "https://controller.test")
        .replace(
            "__REMOTE_WORKER_BUNDLE_PATH__",
            "/remote/worker-bundle.tgz",
        )
    )
    prefix = script.split("parse_args() {", maxsplit=1)[0]
    invalid = tmp_path / "does-not-exist"
    env = os.environ.copy()
    env.update(
        {
            "TMPDIR": str(invalid),
            "XDG_RUNTIME_DIR": str(invalid),
        }
    )
    env.pop("TEMP", None)
    env.pop("TMP", None)
    probe = (
        prefix
        + """\
normalize_system_tmpdir
create_bootstrap_tmpdir
printf '%s\\n%s\\n' "$SYSTEM_TMPDIR" "$TMPDIR"
rm -rf "$TMPDIR"
"""
    )

    shell = subprocess.run(
        ["bash", "-s"],
        input=probe,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    python = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )

    assert shell.returncode == 0, shell.stderr
    system_tmp, bootstrap_tmp = shell.stdout.splitlines()
    expected_tmp = Path(python.stdout.strip()).resolve()
    assert Path(system_tmp).resolve() == expected_tmp
    assert Path(bootstrap_tmp).parent.resolve() == expected_tmp


@pytest.mark.skipif(
    os.name == "nt",
    reason="executes the POSIX curl-to-bash bootstrap under a real shell",
)
def test_join_script_uses_verified_runtime_and_absolute_relative_state(
    tmp_path,
):
    from importlib import resources

    script = (
        resources.files("workgate.remote")
        .joinpath("join_worker.sh")
        .read_text(encoding="utf-8")
        .replace("__REMOTE_SERVER__", "https://controller.test")
        .replace(
            "__REMOTE_WORKER_BUNDLE_PATH__",
            "/remote/worker-bundle.tgz",
        )
    )
    join_path = tmp_path / "join.sh"
    join_path.write_text(script, encoding="utf-8")
    join_path.chmod(0o700)

    launcher_module = b"""\
import os
from pathlib import Path


def ensure_profile_launcher(_python=None):
    path = Path(os.environ["WORKGATE_WORKER_STATE_DIR"]) / "run"
    path.write_text("#!/bin/sh\\n", encoding="utf-8")
    return path, True
"""

    def probe_module(source: str) -> bytes:
        return f"""\
import json
import os
import sys
from pathlib import Path

Path(os.environ["BOOTSTRAP_PROBE"]).write_text(
    json.dumps(
        {{
            "source": {source!r},
            "cwd": str(Path.cwd()),
            "argv": sys.argv[1:],
            "state_dir": os.environ.get("WORKGATE_WORKER_STATE_DIR"),
            "runtime_digest": os.environ.get(
                "WORKGATE_WORKER_RUNTIME_SHA256"
            ),
        }}
    ),
    encoding="utf-8",
)
""".encode()

    bundle = _runtime_archive_bytes(
        overrides={
            "workgate/remote_worker/__main__.py": probe_module("managed"),
            "workgate/remote_worker/profile_launcher.py": (launcher_module),
        }
    )
    digest = hashlib.sha256(bundle).hexdigest()
    bundle_path = tmp_path / "worker.tgz"
    manifest_path = tmp_path / "manifest.json"
    bundle_path.write_bytes(bundle)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_version": "9.9.9",
                "sha256": digest,
                "size": len(bundle),
                "url": f"/remote/worker-bundle.tgz?sha256={digest}",
            }
        ),
        encoding="utf-8",
    )

    checkout = tmp_path / "checkout"
    checkout_package = checkout / "workgate" / "remote_worker"
    checkout_package.mkdir(parents=True)
    (checkout / "workgate" / "__init__.py").write_text("", encoding="utf-8")
    (checkout_package / "__init__.py").write_text("", encoding="utf-8")
    (checkout_package / "__main__.py").write_bytes(probe_module("checkout"))
    (checkout_package / "profile_launcher.py").write_bytes(launcher_module)
    workspace = checkout / "workspace"
    workspace.mkdir()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import shutil\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "output = args[args.index('-o') + 1]\n"
        "url = next(item for item in args if item.startswith('http'))\n"
        "source = os.environ['FAKE_MANIFEST'] if 'manifest=1' in url else os.environ['FAKE_BUNDLE']\n"
        "shutil.copyfile(source, output)\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)

    probe_path = tmp_path / "probe.json"
    environment = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COVERAGE_", "COV_CORE_"))
        },
        "PATH": os.pathsep.join((str(bin_dir), os.environ["PATH"])),
        "WORKGATE_WORKER_STATE_DIR": "worker-state",
        "FAKE_MANIFEST": str(manifest_path),
        "FAKE_BUNDLE": str(bundle_path),
        "BOOTSTRAP_PROBE": str(probe_path),
    }
    completed = subprocess.run(
        [
            "bash",
            str(join_path),
            "--invite",
            "fixture-invite",
            "--profile",
            "p_abcdefgh",
            "--workdir",
            "workspace",
        ],
        cwd=checkout,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    state_dir = (checkout / "worker-state").resolve()
    runtime_dir = state_dir / "runtimes" / digest
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    assert probe == {
        "source": "managed",
        "cwd": str(runtime_dir),
        "argv": [
            "connect",
            "--server",
            "https://controller.test",
            "--invite",
            "fixture-invite",
            "--workdir",
            str(workspace.resolve()),
            "--profile",
            "p_abcdefgh",
        ],
        "state_dir": str(state_dir),
        "runtime_digest": digest,
    }
    assert (state_dir / "profiles/p_abcdefgh/profile.json").is_file()
    assert (state_dir / "run").is_file()
    runtime_python = runtime_dir / ".venv" / "bin" / "python"
    assert runtime_python.is_file()
    dependencies = subprocess.run(
        [
            str(runtime_python),
            "-c",
            "import pathspec, pydantic, pydantic_settings, yaml",
        ],
        cwd=runtime_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dependencies.returncode == 0, dependencies.stderr


@pytest.mark.skipif(
    os.name == "nt",
    reason="executes the POSIX curl-to-bash bootstrap under a real shell",
)
def test_join_script_reuses_one_digest_runtime_across_profiles(tmp_path):
    from importlib import resources

    from workgate.remote.bundle import (
        worker_bundle_bytes,
        worker_bundle_manifest,
    )

    script = (
        resources.files("workgate.remote")
        .joinpath("join_worker.sh")
        .read_text(encoding="utf-8")
        .replace("__REMOTE_SERVER__", "https://controller.test")
        .replace(
            "__REMOTE_WORKER_BUNDLE_PATH__",
            "/remote/worker-bundle.tgz",
        )
    )
    original_tail = '\n  start_worker\n}\n\nmain "$@"'
    assert original_tail in script
    script = script.replace(
        original_tail,
        '\n  echo bootstrap-complete\n}\n\nmain "$@"',
    )
    join_path = tmp_path / "join.sh"
    join_path.write_text(script, encoding="utf-8")
    join_path.chmod(0o700)

    bundle = worker_bundle_bytes()
    manifest = worker_bundle_manifest()
    bundle_path = tmp_path / "worker.tgz"
    manifest_path = tmp_path / "manifest.json"
    bundle_path.write_bytes(bundle)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    requests_path = tmp_path / "curl-requests.txt"
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env python\n"
        "import os\n"
        "import shutil\n"
        "import sys\n"
        "import time\n"
        "args = sys.argv[1:]\n"
        "output = args[args.index('-o') + 1]\n"
        "url = next(item for item in args if item.startswith('http'))\n"
        "with open(os.environ['FAKE_CURL_REQUESTS'], 'a', encoding='utf-8') as log:\n"
        "    log.write(url + '\\n')\n"
        "source = os.environ['FAKE_MANIFEST'] if 'manifest=1' in url else os.environ['FAKE_BUNDLE']\n"
        "if 'manifest=1' not in url:\n"
        "    time.sleep(0.25)\n"
        "shutil.copyfile(source, output)\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)

    state_dir = tmp_path / "state"
    environment = {
        **os.environ,
        "PATH": os.pathsep.join((str(bin_dir), os.environ["PATH"])),
        "WORKGATE_WORKER_STATE_DIR": str(state_dir),
        "FAKE_CURL_REQUESTS": str(requests_path),
        "FAKE_MANIFEST": str(manifest_path),
        "FAKE_BUNDLE": str(bundle_path),
    }
    processes = [
        subprocess.Popen(
            [
                "bash",
                str(join_path),
                "--invite",
                "fixture-invite",
                "--profile",
                profile_id,
                "--workdir",
                str(tmp_path),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for profile_id in ("p_abcdefgh", "p_ijklmnop")
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        assert "bootstrap-complete" in stdout

    requests = requests_path.read_text(encoding="utf-8").splitlines()
    assert sum("manifest=1" in request for request in requests) == 2
    assert sum("sha256=" in request for request in requests) == 1
    digest = str(manifest["sha256"])
    runtime = state_dir / "runtimes" / digest
    assert (runtime / "runtime.json").is_file()
    assert (
        len(
            [
                path
                for path in (state_dir / "runtimes").iterdir()
                if path.is_dir()
            ]
        )
        == 1
    )
    for profile_id in ("p_abcdefgh", "p_ijklmnop"):
        profile = json.loads(
            (state_dir / "profiles" / profile_id / "profile.json").read_text(
                encoding="utf-8"
            )
        )
        assert profile["runtime_sha256"] == digest
    assert (state_dir / "run").stat().st_mode & 0o111


def test_bundle_rejects_stale_digest_and_retry_logs_redact_secrets(capsys):
    from workgate.remote import bundle
    from workgate.remote.http import remote_routes
    from workgate.remote_worker import worker

    client = TestClient(Starlette(routes=remote_routes()))
    response = client.get("/remote/worker-bundle.tgz?sha256=" + "0" * 64)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"

    fixture_value = "fixture-value"
    error = RuntimeError(
        "download https://controller.test/bundle?"
        + "to"
        + "ken="
        + fixture_value
        + " failed; "
        + "Author"
        + "ization: B"
        + "earer "
        + fixture_value
    )
    worker._worker_log_retry("upgrade", error, 1)
    stderr = capsys.readouterr().err
    assert fixture_value not in stderr
    assert "<redacted>" in stderr
    assert bundle.worker_bundle_manifest()["sha256"] not in response.text


def test_fetch_latest_manifest_revalidates_stable_identity(monkeypatch):
    from workgate.remote_worker import runtime

    digest = "a" * 64
    initial = json.dumps({"bundle_version": "3.9.1", "sha256": digest}).encode()
    monkeypatch.setattr(
        runtime, "_fetch_bytes", lambda *args, **kwargs: initial
    )
    calls = []

    def fetch_manifest(server, **kwargs):
        calls.append((server, kwargs))
        return {"bundle_version": "3.9.1", "sha256": digest}

    monkeypatch.setattr(runtime, "fetch_manifest", fetch_manifest)

    result = runtime.fetch_latest_manifest("https://controller.test")

    assert result["sha256"] == digest
    assert calls == [
        (
            "https://controller.test",
            {
                "manifest_path": runtime.REMOTE_WORKER_MANIFEST_PATH,
                "expected_version": "3.9.1",
                "expected_digest": digest,
            },
        )
    ]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "manifest JSON"),
        (b"[]", "invalid remote worker manifest"),
        (
            json.dumps({"bundle_version": "", "sha256": "bad"}).encode(),
            "invalid identity",
        ),
    ],
)
def test_fetch_latest_manifest_rejects_invalid_bootstrap_identity(
    monkeypatch, payload, message
):
    from workgate.remote_worker import runtime

    monkeypatch.setattr(
        runtime, "_fetch_bytes", lambda *args, **kwargs: payload
    )
    monkeypatch.setattr(
        runtime,
        "fetch_manifest",
        lambda *args, **kwargs: pytest.fail("revalidated invalid identity"),
    )

    with pytest.raises(ValueError, match=message):
        runtime.fetch_latest_manifest("https://controller.test")


def test_install_runtime_force_reinstalls_matching_digest(
    tmp_path, monkeypatch
):
    from workgate.remote_worker import runtime

    monkeypatch.setenv("WORKGATE_WORKER_STATE_DIR", str(tmp_path))
    payload = _runtime_archive_bytes()
    digest = _mock_runtime_download(monkeypatch, payload, "3.9.1")
    instruction = {
        "version": "3.9.1",
        "sha256": digest,
        "manifest_path": "/remote/worker-bundle.tgz?manifest=1",
    }
    runtime.install_runtime(
        "https://controller.test", instruction, current_version="3.9.0"
    )

    second = runtime.install_runtime(
        "https://controller.test",
        instruction,
        current_version="3.9.1",
        force=True,
    )

    assert second["updated"] is True
    assert second["sha256"] == digest
