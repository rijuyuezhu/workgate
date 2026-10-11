import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from workgate.utils.serialization import to_jsonable


class NestedModel(BaseModel):
    path: Path
    value: int | None = None


@dataclass(frozen=True)
class Wrapper:
    model: NestedModel
    tags: tuple[str, ...]
    optional: str | None = None


def test_to_jsonable_handles_models_dataclasses_and_nested_values():
    data = to_jsonable(
        {
            1: Wrapper(NestedModel(path=Path("demo.txt")), ("a", "b")),
            "skip": None,
        },
        exclude_none=True,
    )

    assert data == {"1": {"model": {"path": "demo.txt"}, "tags": ["a", "b"]}}
    json.dumps(data)
