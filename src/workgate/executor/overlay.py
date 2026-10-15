"""Discover a usable Tailscale-compatible trusted overlay without changing the host."""

import ipaddress
import json
import subprocess

from ..protocol.executor import TrustedOverlay

_STATUS_TIMEOUT_S = 2


def detect_tailscale_overlay() -> TrustedOverlay | None:
    """Return the local overlay address and tailnet identity, or no capability."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json", "--peers=false"],
            capture_output=True,
            check=False,
            timeout=_STATUS_TIMEOUT_S,
        )
        if result.returncode != 0 or len(result.stdout) > 262_144:
            return None
        status = json.loads(result.stdout)
        if (
            not isinstance(status, dict)
            or status.get("BackendState") != "Running"
        ):
            return None
        tailnet = status.get("CurrentTailnet")
        local = status.get("Self")
        if not isinstance(tailnet, dict) or not isinstance(local, dict):
            return None
        if local.get("Online") is False:
            return None
        tailnet_id = tailnet.get("StableID")
        addresses = local.get("TailscaleIPs", ())
        if not isinstance(tailnet_id, str) or not tailnet_id:
            return None
        if not isinstance(addresses, list):
            return None
        for address in addresses:
            if not isinstance(address, str):
                continue
            ip = ipaddress.ip_address(address)
            if isinstance(
                ip, ipaddress.IPv4Address
            ) and ip in ipaddress.ip_network("100.64.0.0/10"):
                return TrustedOverlay(
                    provider="tailscale", tailnet_id=tailnet_id, ipv4=str(ip)
                )
    except OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired:
        return None
    return None
