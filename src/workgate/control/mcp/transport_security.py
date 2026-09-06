"""Configure MCP transport security settings."""

from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings

from ...config.settings import Settings, get_settings


def _host_header_name(hostname: str) -> str:
    """Return a Host-header-safe hostname, adding IPv6 brackets when needed."""
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def _default_port_for_scheme(scheme: str) -> int | None:
    """Return the default network port for schemes accepted in base_url."""
    return {"http": 80, "https": 443}.get(scheme)


def _add_base_url_transport_allowlist(
    allowed_hosts: set[str], allowed_origins: set[str], base_url: str
) -> None:
    """Allow exactly the configured public origin and compatible default-port Host forms."""
    parsed = urlparse(base_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return

    try:
        port = parsed.port
    except ValueError:
        return

    host = _host_header_name(parsed.hostname.lower())
    default_port = _default_port_for_scheme(scheme)

    if port is None:
        allowed_hosts.add(host)
        if default_port is not None:
            allowed_hosts.add(f"{host}:{default_port}")
        allowed_origins.add(f"{scheme}://{host}")
        return

    allowed_hosts.add(f"{host}:{port}")
    if port == default_port:
        allowed_hosts.add(host)
        allowed_origins.add(f"{scheme}://{host}")
    else:
        allowed_origins.add(f"{scheme}://{host}:{port}")


def transport_security_settings(
    settings: Settings | None = None,
) -> TransportSecuritySettings:
    """Derive MCP transport security settings from the active server settings."""
    active_settings = settings if settings is not None else get_settings()
    allowed_hosts = {
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
    }
    allowed_origins = {
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "https://chatgpt.com",
        "https://chat.openai.com",
    }

    if active_settings.base_url:
        _add_base_url_transport_allowlist(
            allowed_hosts, allowed_origins, active_settings.base_url
        )

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(allowed_hosts),
        allowed_origins=sorted(allowed_origins),
    )
