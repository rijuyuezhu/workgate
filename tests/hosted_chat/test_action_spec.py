"""Pinned Action parser vectors and bounded import admission."""

import json

import pytest

from workgate.hosted_chat import action_spec as action


def spec():
    return {
        "openapi": "3.0.3",
        "servers": [{"url": "https://example.org/v1"}],
        "paths": {
            "/echo/{item}": {
                "get": {
                    "operationId": "getItem",
                    "summary": "Get item",
                    "parameters": [
                        {
                            "name": "item",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {"type": "integer"},
                        },
                    ],
                }
            },
            "/post": {
                "post": {
                    "operationId": "createItem",
                    "x-strict": True,
                    "x-openai-isConsequential": True,
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "message": {"type": "string"}
                                    },
                                    "required": ["message"],
                                }
                            }
                        }
                    },
                }
            },
        },
    }


def test_pinned_operation_projection():
    ops = action.operations(action.parse_spec(json.dumps(spec())))
    assert list(ops) == ["getItem", "createItem"]
    assert ops["getItem"]["locations"] == {"item": "path", "limit": "query"}
    assert ops["getItem"]["parameters"]["required"] == ["item"]
    assert ops["createItem"]["parameters"]["required"] == ["message"]
    assert ops["createItem"]["content_type"] == "application/json"
    assert ops["createItem"]["parameters"]["additionalProperties"] is False
    assert ops["createItem"]["consequential"] is True


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "servers: []",
        "[]",
        "{",
        "servers: [{url: 1}]\npaths: {/x: {}}",
        "servers: [{url: https://example.org}]\npaths: {/x: {}}\ncycle: &a [*a]",
        "servers: [{url: https://example.org}]\npaths: {/x: {}}\nvalue: 2026-11-28",
        "servers: [{url: https://example.org}]\npaths: {/x: {}}\nvalue: .nan",
        "servers: [{url: https://example.org}]\npaths: {/x: {}}\n1: value",
    ],
)
def test_invalid_or_non_json_specs(raw):
    with pytest.raises(ValueError):
        action.parse_spec(raw)


def test_yaml_and_expansion_limit(monkeypatch):
    raw = "servers: [{url: https://example.org}]\npaths: {/x: {get: {operationId: ping}}}"
    assert list(action.operations(action.parse_spec(raw))) == ["ping"]
    monkeypatch.setattr(action, "MAX_SPEC_NODES", 50)
    expanded = (
        raw
        + "\na: &a [1, 2, 3, 4, 5]\nb: &b [*a, *a, *a, *a, *a]\nc: [*b, *b, *b, *b, *b]"
    )
    with pytest.raises(ValueError, match="complexity"):
        action.parse_spec(expanded)


def test_size_path_and_operation_limits(monkeypatch):
    raw = json.dumps(spec())
    monkeypatch.setattr(action, "MAX_SPEC_BYTES", len(raw) - 1)
    with pytest.raises(ValueError):
        action.parse_spec(raw)
    monkeypatch.setattr(action, "MAX_SPEC_BYTES", 512 * 1024)
    monkeypatch.setattr(action, "MAX_OPERATIONS", 1)
    with pytest.raises(ValueError, match="Too many OpenAPI operations"):
        action.operations(spec())
    value = spec()
    value["paths"] = {f"/{i}": {} for i in range(257)}
    with pytest.raises(ValueError, match="Too many OpenAPI paths"):
        action.parse_spec(json.dumps(value))


def test_references_and_rejection():
    value = spec()
    value["components"] = {
        "parameters": {
            "item": {
                "name": "item",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        }
    }
    op = value["paths"]["/echo/{item}"]["get"]
    op["parameters"] = [{"$ref": "#/components/parameters/item"}]
    assert action.operations(value)["getItem"]["locations"] == {"item": "path"}
    op["parameters"] = [{"$ref": "https://evil.example/spec"}]
    with pytest.raises(ValueError, match="Unsupported"):
        action.operations(value)
    op["parameters"] = [{"$ref": "#/components/parameters/nope"}]
    with pytest.raises(ValueError, match="resolve"):
        action.operations(value)
    value["components"]["parameters"]["item"] = {
        "$ref": "#/components/parameters/item"
    }
    op["parameters"] = [{"$ref": "#/components/parameters/item"}]
    with pytest.raises(ValueError, match="Circular"):
        action.operations(value)


@pytest.mark.parametrize("name", ["slash/name", "x" * 129])
def test_invalid_names(name):
    value = spec()
    value["paths"]["/post"]["post"]["operationId"] = name
    with pytest.raises(ValueError, match="operationId"):
        action.operations(value)


def test_duplicate_and_fallback():
    value = spec()
    value["paths"]["/post"]["post"]["operationId"] = "getItem"
    with pytest.raises(ValueError, match="Duplicate"):
        action.operations(value)
    del value["paths"]["/post"]["post"]["operationId"]
    assert "post_post" in action.operations(value)


def test_pinned_names():
    assert action.encoded_domain("api.org") == "api---org"
    assert action.legacy_domain("api.org") == "api---org"
    assert len(action.encoded_domain("https://long-domain.example.org")) == 10
    assert len(action.legacy_domain("long-domain.example.org")) == 10
    assert action.tool_name("getItem", "api.org") == "getItem_action_api---org"
    assert action.is_action_tool("getItem_action_api---org")
    assert not action.is_action_tool("get_action_mcp_server")
    assert (
        action.normalize_action_tool_name("get_foo---bar_action_api---org")
        == "get_foo---bar_action_api_org"
    )
    assert action.normalize_action_tool_name("plain") == "plain"


@pytest.mark.parametrize(
    "domain,server",
    [
        ("https://example.org", "https://example.org/v1"),
        ("example.org", "https://example.org/v1"),
        ("127.0.0.1:1234", "http://127.0.0.1:1234"),
    ],
)
def test_matching_domains(domain, server):
    action.validate_domain(domain, server)


@pytest.mark.parametrize(
    "domain,server",
    [
        ("", "https://example.org"),
        ("ftp://example.org", "https://example.org"),
        ("example.org", "file:///etc/passwd"),
        ("example.org", "https://u:p@example.org"),
        ("https://example.org#fragment", "https://example.org"),
        ("example.org:444", "https://example.org"),
        ("example.org", "https://other.example"),
        ("http://example.org", "https://example.org"),
        ("example.org/path", "https://example.org"),
        ("example.org", "http://example.org"),
    ],
)
def test_invalid_domain(domain, server):
    with pytest.raises(ValueError):
        action.validate_domain(domain, server)


def test_yaml_merge_keys_rejected_before_expansion():
    raw = "servers: [{url: https://example.org}]\npaths: {/x: {}}\na: &a {x: 1}\nb: {<<: [*a, *a]}"
    with pytest.raises(ValueError, match="parsing"):
        action.parse_spec(raw)


@pytest.mark.parametrize(
    "mode",
    [
        "path",
        "params",
        "duplicate",
        "content",
        "entry",
        "properties",
        "required",
        "body_collision",
    ],
)
def test_ambiguous_or_malformed_operation_rejected(mode):
    value = spec()
    get = value["paths"]["/echo/{item}"]["get"]
    post = value["paths"]["/post"]["post"]
    content = post["requestBody"]["content"]
    schema = content["application/json"]["schema"]
    if mode == "path":
        value["paths"]["//external"] = {}
    elif mode == "params":
        get["parameters"] = 1
    elif mode == "duplicate":
        get["parameters"].append(get["parameters"][0])
    elif mode == "content":
        post["requestBody"]["content"] = 1
    elif mode == "entry":
        content["application/json"] = 1
    elif mode == "properties":
        schema["properties"] = 1
    elif mode == "required":
        schema["required"] = ["missing"]
    elif mode == "body_collision":
        post["parameters"] = [
            {"name": "message", "in": "query", "schema": {"type": "string"}}
        ]
    with pytest.raises(ValueError):
        action.operations(value)
