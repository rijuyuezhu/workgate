"""Malformed graph human interrupts must fail before any action is published."""

from dataclasses import replace
from typing import cast
from unittest.mock import Mock

import pytest

from workgate.hosted_chat.graph_human_request import GraphHumanRequest
from workgate.hosted_chat.graph_member_scope import GraphMemberScope
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.provider_common import ModelToolCall


@pytest.fixture
def request_edge():
    scope = GraphMemberScope(
        "run:graph:foreground",
        "root",
        1,
        "member",
        "12345678-1234-4234-8234-123456789abc",
    )
    return GraphHumanRequest(
        scope, "checkpoint", [ModelToolCall("call", "read", {})], False
    )


@pytest.mark.parametrize("identity", ["", None, 42])
def test_request_requires_exact_checkpoint(request_edge, identity):
    with pytest.raises(ValueError, match="exact checkpoint"):
        replace(request_edge, checkpoint_id=identity)


@pytest.mark.parametrize(
    "question,calls", [(None, 1), (1, 1), (False, 0), (True, 2)]
)
def test_request_requires_valid_proposal(request_edge, question, calls):
    proposals = [ModelToolCall(str(i), "read", {}) for i in range(calls)]
    with pytest.raises(ValueError, match="Invalid graph human action proposal"):
        replace(request_edge, question=question, calls=proposals)


def test_saved_member_edge_required(request_edge):
    checkpoint = cast(OwnedLoopCheckpoint, Mock(spec=OwnedLoopCheckpoint))
    checkpoint.active = {"configurable": {}}
    checkpoint.scope = request_edge.scope
    with pytest.raises(ValueError, match="saved member tool edge"):
        GraphHumanRequest.from_checkpoint(checkpoint, request_edge.calls)
    checkpoint.active = {"configurable": {"checkpoint_id": "checkpoint"}}
    checkpoint.scope = None
    with pytest.raises(ValueError, match="saved member tool edge"):
        GraphHumanRequest.from_checkpoint(checkpoint, request_edge.calls)
    checkpoint.scope = request_edge.scope
    assert (
        GraphHumanRequest.from_checkpoint(checkpoint, request_edge.calls)
        == request_edge
    )


def test_parse_rejects_malformed_or_unrecognized_identity(request_edge):
    value = request_edge.value()
    assert GraphHumanRequest.parse(value) == request_edge
    with pytest.raises(ValueError, match="Invalid graph human request"):
        GraphHumanRequest.parse({**value, "extra": True})
    with pytest.raises(ValueError, match="Invalid graph human request"):
        GraphHumanRequest.parse(cast(dict, []))
    for invalid in ({"checkpoint_id": None}, {"question": "yes"}):
        with pytest.raises(
            ValueError, match="Invalid graph human request identity"
        ):
            GraphHumanRequest.parse({**value, **invalid})
