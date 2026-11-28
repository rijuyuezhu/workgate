"""2,079 fixed inputs exported by the pinned original LibreChat/agents source."""

import copy
import json
from pathlib import Path

import pytest

from workgate.hosted_chat import compaction as cp

FIX = Path(__file__).parent / "fixtures"


def _apply(case):
    arg = copy.deepcopy(case["arg"])
    op = case["op"]
    try:
        if op == "getSummaryPartText":
            return cp.get_summary_part_text(arg)
        if op == "isUsableSummaryPart":
            return cp.is_usable_summary_part(arg)
        if op == "findCheckpointSummaryPart":
            return cp.find_checkpoint_summary_part(arg)
        if op == "resolveCheckpointMessage":
            return cp.resolve_checkpoint_message(arg)
        if op == "dropUnusableSummaryParts":
            return {
                "changed": cp.drop_unusable_summary_parts(arg),
                "message": arg,
            }
        if op == "stripUnusableSummaryParts":
            result = cp.strip_unusable_summary_parts(arg)
            return {"result": result, "unchanged": result is arg}
        if op == "markCompactionOutcome":
            cp.mark_compaction_outcome(
                arg["parts"], aborted=arg.get("aborted", False)
            )
            return arg["parts"]
        if op == "resolveFailedTurnContent":
            return cp.resolve_failed_turn_content(arg["body"], arg["error"])
        if op == "buildSummarizationInstruction":
            return cp.build_summarization_instruction(*arg)
        if op == "separateSummarizationParameters":
            return cp.separate_summarization_parameters(arg)
        if op == "buildSummaryCarrierText":
            return cp.build_summary_carrier_text(arg)
        raise AssertionError(op)
    except cp.CompactionFailed as exc:
        return {"error": str(exc), "code": exc.code}


CASES = json.loads((FIX / "compaction_cases.json").read_text(encoding="utf-8"))
REFERENCE = json.loads(
    (FIX / "compaction_vectors.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("index", range(2079))
def test_upstream_fixed_oracle(index):
    actual = _apply(CASES[index])
    assert actual == REFERENCE[index], f"case {index}: {CASES[index]}"
