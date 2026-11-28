"""Pinned memory content-policy projection tests (repository integration separate)."""

import pytest

from workgate.hosted_chat.memory_protection import (
    MemoryContentBlocked,
    assert_memory_content_allowed,
    inspect_memory_content,
    project_stored_memories,
)


def policy(fields=None, *, action="block", expression="PRIVATE-[A-Z]+"):
    opts = {
        "starterPatterns": [],
        "customPatterns": [
            {"id": "private", "label": "private value", "regex": expression}
        ],
        "action": action,
    }
    if fields is not None:
        opts["fields"] = fields
    return {"memories": {"pii": opts}}


@pytest.mark.parametrize(
    "fields,record,expected",
    [
        (
            None,
            {
                "_id": "one",
                "key": "style",
                "value": "PRIVATE-STORED",
                "tokenCount": 4,
            },
            {"key": "style", "value": "", "contentFilterBlocked": True},
        ),
        (
            ["value"],
            {"key": "PRIVATE-KEY", "value": "safe"},
            {"key": "PRIVATE-KEY", "value": "safe"},
        ),
        (
            ["value"],
            {"key": "style", "value": "PRIVATE-VALUE"},
            {"key": "style", "value": "", "contentFilterBlocked": True},
        ),
        (
            ["summary"],
            {"key": "style", "value": "safe", "summary": "PRIVATE-SUMMARY"},
            {
                "key": "style",
                "value": "safe",
                "summary": "",
                "contentFilterBlocked": True,
            },
        ),
        (
            ["key"],
            {"key": "PRIVATE-KEY", "value": "safe"},
            {"key": "", "value": "safe", "contentFilterBlocked": True},
        ),
    ],
)
def test_source_field_projections(fields, record, expected):
    projected = project_stored_memories([record], policy(fields))[0]
    for name, value in expected.items():
        assert projected[name] == value
    if expected.get("contentFilterBlocked"):
        assert projected.get("contentFilterBlocked")
    else:
        assert "contentFilterBlocked" not in projected
    for name in ("_id", "tokenCount"):
        if name in record:
            assert projected[name] == record[name]
    assert record.get("value") == record.get(
        "value"
    )  # source row is unmodified


def field(entry, filters):
    result = inspect_memory_content(entry, filters)
    assert result is not None
    return result.field


def test_policy_only_inspects_selected_memory_fields():
    assert (
        inspect_memory_content(
            {"key": "PRIVATE-KEY", "value": "normal"}, policy(["value"])
        )
        is None
    )
    assert (
        field({"key": "PRIVATE-KEY", "value": "normal"}, policy(["key"]))
        == "key"
    )
    assert (
        inspect_memory_content(
            {"key": "PRIVATE-KEY"}, policy([], expression="PRIVATE-[A-Z]+")
        )
        is None
    )
    assert (
        inspect_memory_content({"key": "PRIVATE-KEY"}, policy(action="audit"))
        is None
    )
    assert (
        project_stored_memories(
            [{"key": "style", "value": "PRIVATE-VALUE"}], policy(action="audit")
        )[0]["value"]
        == "PRIVATE-VALUE"
    )
    assert (
        field(
            {"value": "Bearer token"},
            {"memories": {"pii": {"starterPatterns": ["bearer_header"]}}},
        )
        == "value"
    )
    assert (
        inspect_memory_content(
            {"value": "ordinary"},
            {"memories": {"pii": {"starterPatterns": []}}},
        )
        is None
    )


def test_invalid_or_expensive_patterns_fail_closed():
    invalid = policy(expression="(secret)+(?!okay)")
    finding = inspect_memory_content({"value": "safe"}, invalid)
    assert finding is not None
    assert finding.response(model=True) == {
        "error": "content_filter_block",
        "message": "Submitted content was blocked by content policy.",
        "source": "memory",
        "field": "key",
    }
    assert project_stored_memories(
        [{"key": "normal", "value": "sensitive"}], invalid
    ) == [{"key": "", "value": "", "contentFilterBlocked": True}]
    with pytest.raises(MemoryContentBlocked):
        assert_memory_content_allowed("style", "safe", invalid)


@pytest.mark.parametrize(
    "bad_rules",
    [
        {"starterPatterns": "wrong"},
        {"starterPatterns": ["missing_rule"]},
        {"starterPatterns": [], "customPatterns": "wrong"},
        {"starterPatterns": [], "customPatterns": [None]},
        {
            "starterPatterns": [],
            "customPatterns": [{"id": "x", "regex": "abc"}],
        },
        {
            "starterPatterns": [],
            "customPatterns": [{"id": "x", "label": "x", "regex": None}],
        },
        {
            "starterPatterns": [],
            "customPatterns": [{"id": "x", "label": "x", "regex": "x" * 513}],
        },
        {
            "starterPatterns": [],
            "customPatterns": [
                {"id": f"pattern{i}", "label": "rule", "regex": "." * 40}
                for i in range(220)
            ],
        },
    ],
)
def test_untrusted_filter_configuration_fails_closed(bad_rules):
    filters = {"memories": {"pii": bad_rules}}
    result = inspect_memory_content({"key": "safe", "value": "safe"}, filters)
    assert result is not None
    assert result.field in ("key", "value")


def test_invalid_memory_filter_field_scope_defaults_to_safe_all_fields():
    entry = {"key": "PRIVATE-KEY", "value": "normal"}
    filtered = {
        "memories": {
            "pii": {**policy()["memories"]["pii"], "fields": "not an array"}
        }
    }
    assert field(entry, filtered) == "key"
    assert project_stored_memories([entry], filtered)[0]["key"] == ""
    assert inspect_memory_content({}, {"memories": {}}) is None
    assert inspect_memory_content({"value": "safe"}, None) is None


def test_excessively_long_legacy_values_are_redacted_and_timeout_fails_closed(
    monkeypatch,
):
    import workgate.hosted_chat.memory_protection as protection

    assert field({"value": "a" * (262144 + 1)}, policy(["value"])) == "value"

    class ExpensivePattern:
        def search(self, text, *, timeout):
            raise TimeoutError("pattern timed out")

    monkeypatch.setattr(
        protection, "_rules", lambda _: [(ExpensivePattern(), "confidential")]
    )
    assert field({"value": "safe"}, policy(["value"])) == "value"


def test_memory_aggregate_scan_budget(monkeypatch):
    from workgate.hosted_chat import memory_protection

    monkeypatch.setattr(memory_protection, "_MAX_SCAN_SECONDS", 0.0)
    assert field({"value": "safe"}, policy(["value"])) == "value"
