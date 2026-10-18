"""Neither model config tool input nor raw inspection output enters Audit."""

from workgate.audit import core


def test_audit_config_tool_values_are_redacted(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        core, "audit", lambda event, **kwargs: recorded.append((event, kwargs))
    )
    secret = "audit-leak-sentinel-678239"
    for name in ("config_inspect", "config_save"):
        raw = {
            "executor_id": "executor-one",
            "revision": "c" * 64,
            "changes": {"oauth_admin_pin": {"op": "set", "value": secret}},
        }
        core.audit_tool_call_start(
            call_id="call", transport="mcp", tool=name, input=raw
        )
        core.audit_tool_call_end(
            call_id="call",
            transport="mcp",
            tool=name,
            ok=True,
            duration_ms=4,
            output={
                "fields": [
                    {"name": "oauth_admin_pin", "effective_value": secret}
                ]
            },
        )
    text = repr(recorded)
    assert secret not in text
    assert "oauth_admin_pin" in text  # Safe field names may be audited.
    assert "redacted" in text


def test_unrelated_mcp_audit_is_not_silently_redacted(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        core, "audit", lambda event, **kwargs: recorded.append((event, kwargs))
    )
    core.audit_tool_call_start(
        call_id="call",
        transport="mcp",
        tool="search",
        input={"query": "ordinary"},
    )
    assert "ordinary" in repr(recorded)
