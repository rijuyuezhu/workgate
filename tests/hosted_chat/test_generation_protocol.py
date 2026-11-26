"""Differential tests against the pinned LibreChat protocol.js implementation."""

import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.requests import Request

from workgate.hosted_chat.generation_protocol import (
    GENERATION_PROTOCOL_HEADER,
    job_generation_protocol,
    negotiate_generation_protocol,
    parse_protocol_version,
    request_protocol,
    requested_generation_protocol,
)


def test_negotiation_matches_original_node_for_every_transport_combination():
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "Node is required only for the upstream differential oracle"
        )
    values = [None, 1, 2, "1", "2", 0, 3, "", "02", True, False, ["2"], {}]
    cases = [
        {
            "body": {"generationProtocolVersion": body},
            "query": {"generationProtocolVersion": query},
            "headers": {GENERATION_PROTOCOL_HEADER: header},
        }
        for body, query, header in itertools.product(values, repeat=3)
    ]
    fixture = Path(__file__).parent.parent / "fixtures/hosted_chat/protocol.cjs"
    script = """
const fs = require('fs'); const p = require(process.argv[1]);
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(cases.map(req => [
 p.getRequestedGenerationProtocol(req), p.negotiateNewGenerationProtocol(req),
 ...[undefined, {}, {generationProtocolVersion: 1}, {generationProtocolVersion: 2},
     {generationProtocolVersion: 'invalid'}].map(metadata =>
   p.negotiateExistingGenerationProtocol(req, {metadata}))
])));
"""
    expected = json.loads(
        subprocess.check_output(
            [node, "-e", script, str(fixture.resolve())],
            input=json.dumps(cases).encode(),
        )
    )
    actual = [
        [
            requested_generation_protocol(**case),
            negotiate_generation_protocol(**case),
            *[
                negotiate_generation_protocol(**case, metadata=metadata)
                for metadata in (
                    {},
                    {},
                    {"generationProtocolVersion": 1},
                    {"generationProtocolVersion": 2},
                    {"generationProtocolVersion": "invalid"},
                )
            ],
        ]
        for case in cases
    ]
    assert actual == expected
    assert parse_protocol_version(1.0) == 1
    assert parse_protocol_version(2.0) == 2
    assert parse_protocol_version(None) is None
    assert job_generation_protocol() == 1


def test_http_duplicate_and_conflicting_protocol_markers_cannot_upgrade():
    def request(query=b"", headers=()):
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "query_string": query,
                "headers": list(headers),
            }
        )

    assert request_protocol(request()) == 1
    assert request_protocol(request(), {"generationProtocolVersion": 2}) == 2
    assert (
        request_protocol(
            request(b"generationProtocolVersion=2"), stored_version=1
        )
        == 1
    )
    assert (
        request_protocol(
            request(b"generationProtocolVersion=2&generationProtocolVersion=2")
        )
        == 1
    )
    assert (
        request_protocol(
            request(headers=[(GENERATION_PROTOCOL_HEADER.encode(), b"2")])
        )
        == 2
    )
    assert (
        request_protocol(
            request(
                headers=[
                    (GENERATION_PROTOCOL_HEADER.encode(), b"2"),
                    (GENERATION_PROTOCOL_HEADER.encode(), b"1"),
                ]
            )
        )
        == 1
    )
