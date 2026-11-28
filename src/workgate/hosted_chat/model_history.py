"""Canonical terminal model suffix, adapted from upstream v6.0.3 message_dto.py."""

import copy


def completed_model_steps(
    history: list[dict], messages: list[dict]
) -> list[dict]:
    """Take only this run's complete ordered model exchanges.

    The admitted prefix is exact, not a guessed count of display messages.
    A completed foreground turn may not contain orphaned tool declarations or
    results. Never reconstruct their content from UI previews.
    """
    start = len(history)
    if messages[:start] != history:
        raise ValueError("Canonical model history prefix changed")
    steps = messages[start:]
    pending = set()
    for item in steps:
        role = item.get("role")
        if role == "tool":
            ident = item.get("tool_call_id")
            if not isinstance(ident, str) or ident not in pending:
                raise ValueError("Canonical model step has no tool declaration")
            pending.remove(ident)
            continue
        if pending:
            raise ValueError("Canonical model tool batch is incomplete")
        if role == "assistant":
            calls = item.get("tool_calls", [])
            if not isinstance(calls, list):
                raise ValueError("Invalid canonical tool batch")
            for call in calls:
                ident = call.get("id") if isinstance(call, dict) else None
                if not isinstance(ident, str) or not ident or ident in pending:
                    raise ValueError("Invalid canonical tool call identity")
                pending.add(ident)
        elif role != "user" or item.get("source") != "steer":
            raise ValueError("Unexpected message in canonical model suffix")
    if pending:
        raise ValueError("Canonical model tool batch is incomplete")
    return copy.deepcopy(steps)
