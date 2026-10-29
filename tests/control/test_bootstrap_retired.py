"""Former frozen binary download paths must fail closed, never supply code."""

from starlette.applications import Starlette
from starlette.testclient import TestClient

from workgate.control.executor_bootstrap import executor_bootstrap_routes


def test_retired_control_binary_bootstrap_routes_are_bounded_and_not_scripts():
    client = TestClient(Starlette(routes=executor_bootstrap_routes()))
    for url in (
        "/executor/v1/bootstrap",
        "/executor/v1/bootstrap.ps1",
        "/executor/v1/bootstrap/windows-x86_64/sha256",
        "/executor/v1/bootstrap/linux-x86_64/archive",
        "/executor/v1/bootstrap/macos-aarch64/archive",
    ):
        response = client.get(url)
        assert response.status_code == 410
        assert response.headers["cache-control"] == "no-store"
        assert "source + uv" in response.text
        assert "identity-preserving" in response.text
        assert "tar -x" not in response.text
        assert "workgate.exe" not in response.text
