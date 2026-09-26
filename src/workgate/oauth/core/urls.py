"""OAuth URL helpers."""

from urllib.parse import urlparse, urlunparse

from ...config.control import get_control_config


def base_url() -> str:
    """Return the canonical public base URL."""
    return get_control_config().resolved_base_url


def issuer_url() -> str:
    """Return the OAuth issuer URL."""
    settings = get_control_config()
    return (settings.oauth_issuer or base_url()).rstrip("/")


def resource_url() -> str:
    """Return the OAuth resource identifier for the MCP endpoint."""
    settings = get_control_config()
    if settings.oauth_resource:
        return settings.oauth_resource.rstrip("/")
    return f"{base_url()}/mcp"


def protected_resource_metadata_url() -> str:
    """Return the well-known metadata URL for the configured resource."""
    parsed = urlparse(resource_url())
    resource_path = "" if parsed.path == "/" else parsed.path
    metadata_path = "/.well-known/oauth-protected-resource" + resource_path
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            metadata_path,
            "",
            parsed.query,
            "",
        )
    )


def normalize_resource(value: str) -> str:
    """Normalize resource indicators for slash-tolerant comparisons."""
    return value.rstrip("/")
