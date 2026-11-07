"""Keep third-party workflow actions pinned to immutable commits."""

import re
from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_ACTION = re.compile(r"\buses:\s*(\S+)(?:\s+#\s*(\S+))?")
_SHA = re.compile(r"^[a-f0-9]{40}$")


def test_workflow_actions_use_full_shas_with_version_labels() -> None:
    refs = [
        (path.name, match.group(1), match.group(2))
        for path in _WORKFLOWS.glob("*.yml")
        for match in _ACTION.finditer(path.read_text())
    ]
    assert refs
    for workflow, ref, version in refs:
        if ref.startswith("./") or ref.startswith("docker://"):
            continue
        assert "@" in ref, (workflow, ref)
        assert _SHA.fullmatch(ref.rsplit("@", 1)[1]), (workflow, ref)
        assert version, (workflow, ref)
