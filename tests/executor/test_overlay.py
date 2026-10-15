"""Tailscale capability discovery must be optional, bounded and fail closed."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import workgate.executor.hello as hello_module
import workgate.executor.overlay as overlay_module
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.hello import build_executor_hello
from workgate.executor.overlay import detect_tailscale_overlay
from workgate.protocol.executor import TrustedOverlay


def _status(
    *,
    backend: str = "Running",
    tailnet: str | None = "tailnet-id",
    addresses: list[str] | None = None,
) -> bytes:
    return json.dumps(
        {
            "BackendState": backend,
            "CurrentTailnet": {"StableID": tailnet},
            "Self": {"TailscaleIPs": addresses or ["100.100.253.113"]},
        }
    ).encode()


def test_overlay_detects_tailscale_and_hello_advertises_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=_status())

    monkeypatch.setattr(overlay_module.subprocess, "run", run)
    overlay = detect_tailscale_overlay()
    assert overlay is not None
    assert overlay.provider == "tailscale"
    assert overlay.ipv4 == "100.100.253.113"
    assert overlay.tailnet_id == "tailnet-id"
    assert seen[0][0] == ["tailscale", "status", "--json", "--peers=false"]
    assert seen[0][1]["timeout"] == 2
    monkeypatch.setattr(
        hello_module, "browser_capability_available", lambda: False
    )
    monkeypatch.setattr(hello_module, "gui_capability_available", lambda: False)
    hello = build_executor_hello(
        resolve_executor_config(Settings(default_workdir=tmp_path)),
        overlay=overlay,
    )
    assert hello.overlay == overlay
    assert hello.model_dump()["overlay"]["tailnet_id"] == "tailnet-id"


@pytest.mark.parametrize(
    "status",
    [
        _status(backend="Stopped"),
        _status(tailnet=None),
        _status(addresses=["192.168.1.2"]),
        _status(addresses=["not-an-ip"]),
        b"invalid-json",
        b"null",
        b"{}",
        b'{"BackendState":"Running","CurrentTailnet":null,"Self":null}',
    ],
)
def test_overlay_unusable_status_is_not_advertised(monkeypatch, status):
    monkeypatch.setattr(
        overlay_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=status),
    )
    assert detect_tailscale_overlay() is None


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError(), subprocess.TimeoutExpired("tailscale", 2)],
)
def test_overlay_missing_or_timed_out_is_not_advertised(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(overlay_module.subprocess, "run", fail)
    assert detect_tailscale_overlay() is None


def test_overlay_hello_rejects_non_cgnat_peer_address():
    with pytest.raises(ValueError, match="CGNAT"):
        TrustedOverlay(
            provider="tailscale", tailnet_id="tailnet-id", ipv4="127.0.0.1"
        )


def test_overlay_hello_rejects_invalid_ipv4():
    with pytest.raises(ValueError, match="invalid"):
        TrustedOverlay(
            provider="tailscale", tailnet_id="tailnet-id", ipv4="not-an-ip"
        )


def test_overlay_hello_accepts_cgnat_ipv4():
    result = TrustedOverlay(
        provider="tailscale", tailnet_id="tailnet-id", ipv4="100.64.1.9"
    )
    assert result.ipv4 == "100.64.1.9"
