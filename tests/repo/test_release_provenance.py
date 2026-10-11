"""Release authorization from main SHA, tag identity and completed main CI."""

import runpy
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

check_provenance = runpy.run_path(
    str(
        Path(__file__).parents[2]
        / "scripts/validation/check-release-provenance.py"
    )
)["check_provenance"]

SHA = "a" * 40
OLD = "b" * 40
TAG = "v5.0.0"


def fake_api(
    *,
    main: str = SHA,
    tagged: str | None = SHA,
    runs: list[dict[str, str]] | None = None,
    branch: str = "main",
):
    if runs is None:
        runs = [
            {
                "head_sha": SHA,
                "head_branch": branch,
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]

    def fetch(path: str, query: dict[str, str] | None):
        if not path:
            return {"default_branch": branch}
        if path == f"branches/{branch}":
            return {"commit": {"sha": main}}
        if path == f"git/ref/tags/{TAG}":
            if tagged is None:
                raise urllib.error.HTTPError(
                    path, 404, "Not Found", Message(), None
                )
            return {"ref": f"refs/tags/{TAG}"}
        if path == f"commits/{TAG}":
            return {"sha": tagged}
        if path == "actions/workflows/ci.yml/runs":
            assert query == {
                "head_sha": SHA,
                "branch": branch,
                "event": "push",
                "per_page": "100",
            }
            return {"workflow_runs": runs}
        raise AssertionError(f"unexpected GitHub API request: {path}")

    return fetch


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_release_accepts_current_main_with_successful_ci(event):
    check_provenance(fake_api(), sha=SHA, tag=TAG, event=event)


def test_release_accepts_missing_tag_only_for_manual_dispatch():
    check_provenance(
        fake_api(tagged=None), sha=SHA, tag=TAG, event="workflow_dispatch"
    )
    with pytest.raises(urllib.error.HTTPError):
        check_provenance(fake_api(tagged=None), sha=SHA, tag=TAG, event="push")


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_release_rejects_stale_or_side_branch_commit(event):
    with pytest.raises(ValueError, match="not current"):
        check_provenance(fake_api(main=OLD), sha=SHA, tag=TAG, event=event)


def test_release_rejects_manual_dispatch_of_existing_stale_tag():
    with pytest.raises(ValueError, match="tag.*points"):
        check_provenance(
            fake_api(tagged=OLD), sha=SHA, tag=TAG, event="workflow_dispatch"
        )


@pytest.mark.parametrize(
    "status,conclusion",
    [
        ("in_progress", None),
        ("completed", "failure"),
        ("completed", "cancelled"),
    ],
)
def test_release_rejects_missing_green_ci(status, conclusion):
    runs = [
        {
            "head_sha": SHA,
            "head_branch": "main",
            "event": "push",
            "status": status,
            "conclusion": conclusion,
        }
    ]
    with pytest.raises(ValueError, match="no completed successful"):
        check_provenance(fake_api(runs=runs), sha=SHA, tag=TAG, event="push")


def test_release_rejects_unrelated_green_runs():
    for wrong in (
        {"head_sha": OLD},
        {"head_branch": "feature"},
        {"event": "workflow_dispatch"},
    ):
        run = {
            "head_sha": SHA,
            "head_branch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            **wrong,
        }
        with pytest.raises(ValueError, match="no completed successful"):
            check_provenance(
                fake_api(runs=[run]), sha=SHA, tag=TAG, event="push"
            )


def test_release_rejects_unknown_event():
    with pytest.raises(ValueError, match="unsupported"):
        check_provenance(fake_api(), sha=SHA, tag=TAG, event="pull_request")


def test_release_accepts_renamed_default_branch():
    check_provenance(
        fake_api(branch="release-main"), sha=SHA, tag=TAG, event="push"
    )
