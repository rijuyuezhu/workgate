"""Redacted public status projection for Agent Bridge registries."""

from typing import Any

from .auth import manager_auth_status, manager_redaction_maps
from .models import AgentCapabilityRegistry
from .redaction import _redact_text, redact_configured_values


def registry_config_status(
    registry: AgentCapabilityRegistry,
) -> dict[str, Any]:
    """Return a redacted status payload for diagnostics and public tools."""
    return {
        "config_dir": str(registry.config_dir),
        "config_path": str(registry.config_path),
        "manifest_status": registry.manifest_status,
        "manifest_errors": registry.manifest_errors,
        "skills": {
            "count": len(registry.skills),
            "warnings": registry.skill_warnings,
            "sources": [
                source.public_row() for source in registry.skill_sources
            ],
        },
        "mcp_servers": {
            name: {
                "type": record.config.type,
                "enabled": record.config.enabled,
                "available": record.available,
                "tool_count": len(record.tools),
                "error": (
                    _redact_text(
                        redact_configured_values(
                            record.error,
                            *manager_redaction_maps(
                                registry.client_manager,
                                name,
                                record.config,
                            ),
                        )
                    )
                    if record.error
                    else None
                ),
                "env": {str(key): "<redacted>" for key in record.config.env},
                "headers": {
                    str(key): "<redacted>" for key in record.config.headers
                },
                "auth": manager_auth_status(
                    registry.client_manager,
                    name,
                    record.config,
                ),
            }
            for name, record in registry.mcp_servers.items()
        },
    }
