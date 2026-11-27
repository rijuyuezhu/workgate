"""Differential checks against original pinned LibreChat HITL functions."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from workgate.hosted_chat.ask_user_tools import (
    ASK_USER_MANIFEST,
    ASK_USER_QUESTION_DEFINITION,
    ASK_USER_TOOLS,
    ask_user_requested,
    build_ask_user_payload,
    resolve_ask_user_question_resume,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "hosted_chat"


def test_provider_definition_is_exact_original_source_export():
    original = json.loads(
        (FIXTURES / "ask_user_definition.json").read_text(encoding="utf-8")
    )
    assert original == ASK_USER_QUESTION_DEFINITION
    assert ASK_USER_TOOLS[0]["function"] == {
        "name": original["name"],
        "description": original["description"],
        "parameters": original["schema"],
    }


def test_resume_validation_matches_original_node_functions():
    node = shutil.which("node")
    if not node:
        pytest.skip(
            "Node is a development-only differential oracle; required in React CI"
        )
    cases = []
    for payload in [
        {},
        {"questions": None},
        {"questions": []},
        {"questions": [{"id": "a"}, {"id": "b"}]},
        {"questions": [{"id": "a"}, {"id": "a"}]},
        {"questions": [{"id": "0bad"}]},
        {"questions": [{"id": "a"}] * 5},
    ]:
        for body in [
            {},
            {"answer": ""},
            {"answer": " "},
            {"answer": "ok"},
            {"answer": "x" * 16001},
            {"answer": "😀" * 8001},
            {"answers": []},
            {"answers": {"a": "one"}},
            {"answers": {"b": "two", "a": "one"}},
            {"answers": {"a": "", "b": "two"}},
            {"answers": {"a": True, "b": "two"}},
            {"answers": {"a": "x" * 16001, "b": "two"}},
            {"answers": {"a": "one", "b": "two", "unknown": "three"}},
        ]:
            cases.append([payload, body])
    script = """
const fs = require('node:fs');
const oracle = require(process.argv[1]);
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(cases.map(([p,b]) => oracle.resolveAskUserQuestionResume(p,b))));
"""
    result = subprocess.run(
        [node, "-e", script, str(FIXTURES / "hitl_reference.cjs")],
        input=json.dumps(cases),
        text=True,
        capture_output=True,
        check=True,
    )
    assert [
        resolve_ask_user_question_resume(*case) for case in cases
    ] == json.loads(result.stdout)


def test_question_schema_limits_strip_unknown_keys_and_batch_fallback():
    payload = build_ask_user_payload(
        {
            "questions": [
                {
                    "id": "target",
                    "header": "Where",
                    "question": "Choose",
                    "description": "Context",
                    "extra": "strip",
                    "options": [
                        {"label": "x" * 280, "value": "v" * 500, "extra": True}
                    ],
                    "multiSelect": True,
                },
                {"id": "format", "question": "Format?"},
            ]
        },
        "call",
    )
    assert payload["tool_call_id"] == "call"
    assert "extra" not in payload["questions"][0]
    assert (
        "id" not in payload["question"] and "header" not in payload["question"]
    )
    assert payload["questions"][0]["options"][0] == {
        "label": "x" * 280,
        "value": "v" * 500,
    }
    assert build_ask_user_payload(
        {"question": "legacy", "questions": []}, "old"
    )["question"] == {"question": "legacy"}
    assert (
        len(
            build_ask_user_payload(
                {"questions": [{"id": "a", "question": "😀" * 1000}]}, "c"
            )["questions"]
        )
        == 1
    )
    invalid = [
        None,
        {},
        {"questions": []},
        {"questions": [{}]},
        {"questions": [None]},
        {"questions": [{"id": "bad id", "question": "q"}]},
        {"questions": [{"id": "a", "question": "q"}] * 2},
        {"questions": [{"id": "a", "question": ""}]},
        {"questions": [{"id": "a", "question": "😀" * 1001}]},
        {"questions": [{"id": "a", "question": "q", "header": ""}]},
        {"questions": [{"id": "a", "question": "q", "multiSelect": 1}]},
        {"questions": [{"id": "a", "question": "q", "options": "invalid"}]},
        {"questions": [{"id": "a", "question": "q", "options": [None]}]},
        {
            "questions": [
                {
                    "id": "a",
                    "question": "q",
                    "options": [{"label": "x" * 281, "value": "v"}],
                }
            ]
        },
        {"question": None, "questions": []},
    ]
    for arguments in invalid:
        with pytest.raises(ValueError):
            build_ask_user_payload(arguments, "call")


def test_manifest_and_explicit_saved_or_ephemeral_opt_in():
    expected = json.loads(
        (FIXTURES / "ask_user_manifest.json").read_text(encoding="utf-8")
    )
    assert expected == ASK_USER_MANIFEST
    assert not ask_user_requested(
        {"tools": []}, {"ephemeralAgent": {"ask_user_question": True}}
    )
    assert ask_user_requested({"tools": ["ask_user_question"]}, {})
    assert not ask_user_requested(
        None, {"ephemeralAgent": {"ask_user_question": "true"}}
    )
    assert not ask_user_requested(None, {})
    assert ask_user_requested(
        None, {"ephemeralAgent": {"ask_user_question": True}}
    )
