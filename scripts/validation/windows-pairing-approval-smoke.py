"""Native Windows CLI device pairing: actual owner approval and denial."""

import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx

if os.name != "nt":
    raise RuntimeError("This smoke needs native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tests.e2e.helpers import (  # noqa: E402
    free_tcp_port,
    isolated_process_env,
    isolated_xdg_env,
    start_control_process,
    stop_process,
    write_yaml_config,
)
from workgate.config.settings import Settings, configure_settings  # noqa: E402
from workgate.executor.profile import ExecutorProfileStore  # noqa: E402
from workgate.oauth.protocol.token_codec import issue_access_token  # noqa: E402
from workgate.persistence import FileStateStore  # noqa: E402


def _pair_one(
    root: Path,
    url: str,
    *,
    approve: bool,
    owner_headers: dict[str, str] | None = None,
    limited_headers: dict[str, str] | None = None,
    wrong_audience_headers: dict[str, str] | None = None,
) -> None:
    state = root / ("approved" if approve else "denied")
    state.mkdir()
    config = write_yaml_config(
        root / f"{'approve' if approve else 'deny'}.yaml",
        {"state_dir": str(state)},
    )
    output = root / f"{'approve' if approve else 'deny'}.out"
    errors = root / f"{'approve' if approve else 'deny'}.err"
    with (
        output.open("w", encoding="utf-8") as stdout,
        errors.open("w", encoding="utf-8") as stderr,
    ):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "workgate.main",
                "executor",
                "connect",
                url,
                "--name",
                "native-windows-test",
                "--config",
                str(config),
            ],
            env=isolated_process_env(),
            stdout=stdout,
            stderr=stderr,
        )
    try:
        deadline = time.monotonic() + 35
        code = None
        with httpx.Client(timeout=3, trust_env=False) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(
                        f"executor connect exited before owner approval: {errors.read_text(errors='replace')}"
                    )
                match = re.search(
                    r"^Code: (\S+)$",
                    output.read_text(encoding="utf-8"),
                    flags=re.MULTILINE,
                )
                if match is not None:
                    code = match.group(1)
                    break
                time.sleep(0.1)
            assert code, (
                "executor connect never displayed the pairing user code"
            )
            if owner_headers is not None:
                # Pairing challenge is public, but owner decision and lookup
                # must require a valid OAuth bearer with executor:use scope.
                assert (
                    client.get(
                        f"{url}/api/ui/pair", params={"code": code}
                    ).status_code
                    == 401
                )
                assert (
                    client.post(
                        f"{url}/api/ui/pair",
                        json={"user_code": code, "decision": "approve"},
                    ).status_code
                    == 401
                )
                assert limited_headers is not None
                assert (
                    client.get(
                        f"{url}/api/ui/pair",
                        params={"code": code},
                        headers=limited_headers,
                    ).status_code
                    == 403
                )
                assert (
                    client.post(
                        f"{url}/api/ui/pair",
                        headers=limited_headers,
                        json={"user_code": code, "decision": "approve"},
                    ).status_code
                    == 403
                )
                assert (
                    client.post(
                        f"{url}/api/ui/pair",
                        headers={"Authorization": "Bearer invalid-token"},
                        json={"user_code": code, "decision": "approve"},
                    ).status_code
                    == 401
                )
                assert wrong_audience_headers is not None
                assert (
                    client.post(
                        f"{url}/api/ui/pair",
                        headers=wrong_audience_headers,
                        json={"user_code": code, "decision": "approve"},
                    ).status_code
                    == 401
                )
            detail = client.get(
                f"{url}/api/ui/pair",
                params={"code": code},
                headers=owner_headers,
            )
            assert detail.status_code == 200, detail.text
            assert (
                detail.json()["data"]["requested_name"] == "native-windows-test"
            )
            decision = client.post(
                f"{url}/api/ui/pair",
                headers=owner_headers,
                json={
                    "user_code": code,
                    "decision": "approve" if approve else "deny",
                    "name": "native-approved" if approve else None,
                },
            )
            assert decision.status_code == 200, decision.text
            owner = decision.json()["data"]
            assert owner["status"] == ("approved" if approve else "denied")
        assert process.wait(timeout=35) == (0 if approve else 1), (
            errors.read_text(encoding="utf-8", errors="replace")
        )
        profile = ExecutorProfileStore(FileStateStore(lambda: state)).load()
        if approve:
            assert profile is not None and profile.control_url == url
            assert profile.executor_id == owner["executor_id"]
            assert (
                f"Paired executor: {profile.executor_id}"
                in output.read_text(encoding="utf-8")
            )
        else:
            assert owner["executor_id"] is None
            assert "pairing_denied" in errors.read_text(encoding="utf-8")
            assert profile is None, (
                "denied pairing wrote an executor credential"
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def _run_control_case(root: Path, *, oauth: bool) -> None:
    case_root = root / ("oauth" if oauth else "none")
    case_root.mkdir()
    port = free_tcp_port()
    url = f"http://127.0.0.1:{port}"
    config_values = {
        "mode": "http",
        "host": "127.0.0.1",
        "port": port,
        "base_url": url,
        "auth_mode": "oauth" if oauth else "none",
        "auth_bypass_localhost": False,
        "oauth_admin_pin": secrets.token_urlsafe(18),
        "state_dir": str(case_root / "control-state"),
        "data_dir": str(case_root / "control-data"),
        "agent_bridge_enabled": False,
    }
    control_config = write_yaml_config(
        case_root / "control.yaml", config_values
    )
    owner_headers = None
    limited_headers = None
    wrong_audience_headers = None
    if oauth:
        # The owner uses a signed, scoped bearer against the real Control.
        # Signing key/state are disposable and shared with the child process,
        # not stubbed in the request or replaced with localhost auth bypass.
        configure_settings(Settings(**config_values))
        resource = f"{url}/mcp"
        owner_headers = {
            "Authorization": "Bearer "
            + issue_access_token(
                client_id="windows-owner-smoke",
                scope="executor:use",
                resource=resource,
            )
        }
        limited_headers = {
            "Authorization": "Bearer "
            + issue_access_token(
                client_id="windows-limited-smoke",
                scope="shell:read",
                resource=resource,
            )
        }
        wrong_audience_headers = {
            "Authorization": "Bearer "
            + issue_access_token(
                client_id="windows-other-resource-smoke",
                scope="executor:use",
                resource=f"{url}/not-mcp",
            )
        }
    control = None
    try:
        control = start_control_process(
            control_config,
            env=isolated_xdg_env(case_root / "xdg"),
            stdout_path=case_root / "control.out",
            stderr_path=case_root / "control.err",
        )
        with httpx.Client(timeout=2, trust_env=False) as client:
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if control.poll() is not None:
                    raise AssertionError(
                        f"Control failed: {(case_root / 'control.err').read_text(errors='replace')}"
                    )
                try:
                    if client.get(f"{url}/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Control never became ready")
            if oauth:
                assert client.get(f"{url}/pair").status_code == 200
                assert client.get(f"{url}/api/ui/executors").status_code == 401
                assert (
                    client.get(
                        f"{url}/api/ui/executors", headers=limited_headers
                    ).status_code
                    == 403
                )
                assert (
                    client.get(
                        f"{url}/api/ui/executors", headers=owner_headers
                    ).status_code
                    == 200
                )
        _pair_one(
            case_root,
            url,
            approve=True,
            owner_headers=owner_headers,
            limited_headers=limited_headers,
            wrong_audience_headers=wrong_audience_headers,
        )
        _pair_one(
            case_root,
            url,
            approve=False,
            owner_headers=owner_headers,
            limited_headers=limited_headers,
            wrong_audience_headers=wrong_audience_headers,
        )
    finally:
        if control is not None:
            stop_process(control)


def main() -> None:
    root = Path(tempfile.gettempdir()) / f"workgate-pair-392-{uuid4().hex}"
    root.mkdir()
    try:
        _run_control_case(root, oauth=False)
        _run_control_case(root, oauth=True)
    finally:
        for attempt in range(40):
            try:
                shutil.rmtree(root)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.15)
    print(
        "Native Windows CLI pairing: unauthenticated denied, OAuth owner approved, "
        "insufficient scope rejected, deny leaves no profile: PASS"
    )


if __name__ == "__main__":
    main()
