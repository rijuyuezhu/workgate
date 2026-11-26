"""Source oracle and native-dispatch behavior for batched human decisions."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from workgate.hosted_chat.approval_runtime import (
    approval_policy_decision,
    build_approval_payload,
    execute_approval_batch,
    prepare_approval_batch,
    truncate_approval_result,
)
from workgate.hosted_chat.provider_common import ModelToolCall


def _oracle(script, cases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Original Node oracle runs in the required frontend CI job")
    fixture = (
        Path(__file__).parent.parent
        / "fixtures/hosted_chat/approval_runtime_reference.cjs"
    )
    program = (
        "const p=require(process.argv[1]); const fs=require('fs'); const cases=JSON.parse(fs.readFileSync(0,'utf8')); "
        + script
    )
    result = subprocess.run(
        [node, "-e", program, str(fixture.resolve())],
        input=json.dumps(cases),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_static_policy_matches_original_hook_precedence_and_literal_globs():
    policies = [
        None,
        {},
        {"enabled": False},
        {"enabled": True},
        {"enabled": True, "mode": "bypass"},
        {"enabled": True, "mode": "dontAsk"},
        {
            "enabled": True,
            "mode": "bypass",
            "deny": ["*"],
            "reason": "No {tool}/{tool}",
        },
        {
            "enabled": True,
            "allow": ["*"],
            "ask": ["mcp:*"],
            "deny": ["mcp:secret:*"],
            "reason": "Review {tool}",
        },
        {
            "enabled": True,
            "allow": ["file.read", "x?", "foo[1]", "(test)"],
            "mode": "dontAsk",
        },
        {"enabled": True, "allow": ["a*b"], "reason": ""},
    ]
    cases = [
        [policy, name]
        for policy in policies
        for name in [
            "file.read",
            "fileXread",
            "mcp:search:find",
            "mcp:secret:delete",
            "x?",
            "x1",
            "foo[1]",
            "(test)",
            "ab",
            "a\nb",
        ]
    ]
    expected = _oracle(
        """
(async()=>{ const out=[]; for (const [config, toolName] of cases) {
 out.push(config?.enabled === true ? await p.createToolPolicyHook(config)({toolName}) : null);
} process.stdout.write(JSON.stringify(out)); })();
""",
        cases,
    )
    assert [approval_policy_decision(*case) for case in cases] == expected


def test_result_truncation_matches_original_utf16_newline_and_small_budget_behavior():
    contents = [
        "short",
        "a" * 1000,
        "😀" * 600,
        ("header\n" + "a" * 40 + "\n") * 80,
    ]
    cases = [
        [content, budget]
        for content in contents
        for budget in [0, 1, 2, 5, 65, 110, 250, 500, 5000]
    ]
    expected = _oracle(
        "process.stdout.write(JSON.stringify(cases.map(([s,n])=>p.truncateToolResultContent(s,n))));",
        cases,
    )
    assert [truncate_approval_result(*case) for case in cases] == expected


def test_payload_matches_original_librechat_default_and_explicit_respond():
    calls = [
        ModelToolCall("a", "file_read", {"path": "one"}),
        ModelToolCall("b", "mcp_tool_call", {"name": "server:read"}),
    ]
    for overrides in [None, {"file_read": ["approve", "respond"]}]:
        inputs = [
            {
                "name": call.name,
                "arguments": call.arguments,
                "tool_call_id": call.call_id,
            }
            for call in calls
        ]
        expected = _oracle(
            "process.stdout.write(JSON.stringify(cases.map(([c,d])=>p.buildToolApprovalPayload(c,d))));",
            [[inputs, overrides]],
        )[0]
        assert (
            build_approval_payload(calls, decisions_by_tool_name=overrides)
            == expected
        )


@pytest.mark.asyncio
async def test_mixed_batch_preserves_ids_edits_arguments_and_skips_synthetic_dispatch():
    calls = [ModelToolCall(str(i), "tool", {"value": i}) for i in range(4)]
    payload = build_approval_payload(
        calls,
        decisions_by_tool_name={
            "tool": ["approve", "reject", "edit", "respond"]
        },
        descriptions={"1": "Policy reason"},
    )
    batch = prepare_approval_batch(
        payload,
        [
            {
                "tool_call_id": "3",
                "decision": "respond",
                "responseText": "Human result",
            },
            {
                "tool_call_id": "2",
                "decision": "edit",
                "editedArguments": {"value": 99},
            },
            {"tool_call_id": "0", "decision": "approve"},
            {"tool_call_id": "1", "decision": "reject"},
        ],
    )
    payload["action_requests"][0]["arguments"]["value"] = (
        "tampered after preparation"
    )
    invoked, completed = [], []

    async def dispatch(call):
        invoked.append((call.call_id, call.arguments))
        return "Native result"

    async def checkpoint(result):
        completed.append(result.call.call_id)

    results = await execute_approval_batch(
        batch, dispatch=dispatch, on_completed=checkpoint
    )
    assert invoked == [("0", {"value": 0}), ("2", {"value": 99})]
    assert completed == ["0", "1", "2", "3"]
    assert [result.content for result in results] == [
        "Native result",
        "Blocked: Policy reason",
        "Native result",
        "Human result",
    ]
    assert [result.status for result in results] == [
        "success",
        "error",
        "success",
        "success",
    ]
    assert results[3].history_message() == {
        "role": "tool",
        "tool_call_id": "3",
        "content": "Human result",
    }


@pytest.mark.parametrize(
    "decisions,error",
    [
        ([], ValueError),
        (
            [
                {
                    "tool_call_id": "a",
                    "decision": "respond",
                    "responseText": "manual",
                }
            ],
            PermissionError,
        ),
        (
            [{"tool_call_id": "a", "decision": "edit", "editedArguments": []}],
            ValueError,
        ),
        ([{"tool_call_id": [], "decision": "approve"}], ValueError),
        ([None], ValueError),
    ],
)
def test_invalid_batch_is_rejected_before_any_execution_or_claim(
    decisions, error
):
    payload = build_approval_payload(
        [ModelToolCall("a", "file_read", {"path": "x"})]
    )
    with pytest.raises(error):
        prepare_approval_batch(payload, decisions)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(type="other"),
        lambda p: p.update(action_requests=None),
        lambda p: p.update(action_requests=[None]),
        lambda p: p["review_configs"][0].update(action_name="different"),
        lambda p: p["action_requests"][0].update(arguments=[]),
        lambda p: p["action_requests"][0].update(description=3),
        lambda p: p["review_configs"][0].update(allowed_decisions=["unknown"]),
        lambda p: p["action_requests"].append(p["action_requests"][0]),
    ],
)
def test_review_evidence_shape_cannot_change_tool_identity(mutate):
    payload = build_approval_payload(
        [ModelToolCall("a", "file_read", {"path": "x"})]
    )
    mutate(payload)
    with pytest.raises(ValueError):
        prepare_approval_batch(
            payload, [{"tool_call_id": "a", "decision": "approve"}]
        )


@pytest.mark.asyncio
async def test_rejection_reason_and_cancellation_preserve_completed_checkpoint_ids():
    calls = [ModelToolCall("a", "tool", {}), ModelToolCall("b", "tool", {})]
    payload = build_approval_payload(calls)
    blocked = prepare_approval_batch(
        payload,
        [
            {"tool_call_id": "a", "decision": "reject", "reason": ""},
            {"tool_call_id": "b", "decision": "reject"},
        ],
    )

    async def forbidden(call):
        raise AssertionError("Rejected calls must never execute")

    results = await execute_approval_batch(blocked, dispatch=forbidden)
    assert [r.content for r in results] == [
        "Blocked: ",
        "Blocked: Rejected by user",
    ]
    approved = prepare_approval_batch(
        payload,
        [{"tool_call_id": c.call_id, "decision": "approve"} for c in calls],
    )
    finished = []

    async def dispatch(call):
        if call.call_id == "b":
            raise asyncio.CancelledError
        return "done"

    async def checkpoint(result):
        finished.append(result.call.call_id)

    with pytest.raises(asyncio.CancelledError):
        await execute_approval_batch(
            approved, dispatch=dispatch, on_completed=checkpoint
        )
    assert finished == ["a"]


@pytest.mark.asyncio
async def test_edited_batch_preflight_and_dispatch_rechecks_permissions():
    calls = [
        ModelToolCall("a", "file_write", {"path": "first"}),
        ModelToolCall("b", "file_write", {"path": "second"}),
    ]
    payload = build_approval_payload(calls)
    with pytest.raises(ValueError):
        prepare_approval_batch(
            payload,
            [
                {"tool_call_id": "a", "decision": "approve"},
                {
                    "tool_call_id": "b",
                    "decision": "edit",
                    "editedArguments": {"bad": float("nan")},
                },
            ],
        )
    batch = prepare_approval_batch(
        payload,
        [
            {
                "tool_call_id": "a",
                "decision": "edit",
                "editedArguments": {"path": "edited"},
            },
            {"tool_call_id": "b", "decision": "approve"},
        ],
    )
    invoked = []

    async def dispatch(call):
        invoked.append(call.arguments["path"])
        if call.call_id == "b":
            raise PermissionError("revoked")
        call.arguments["path"] = "mutated"
        return "done"

    completed = []

    async def save(result):
        completed.append(result.call.arguments["path"])

    with pytest.raises(PermissionError, match="revoked"):
        await execute_approval_batch(
            batch, dispatch=dispatch, on_completed=save
        )
    assert invoked == ["edited", "second"]
    assert completed == ["edited"]
    assert batch.entries[0].tool_call().arguments == {"path": "edited"}
