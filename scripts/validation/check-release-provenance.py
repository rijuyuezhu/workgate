#!/usr/bin/env python3
"""Allow releases only from the current default-branch commit with green push CI."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any


def check_provenance(
    fetch: Callable[[str, dict[str, str] | None], dict[str, Any]],
    *,
    sha: str,
    tag: str,
    event: str,
) -> None:
    if event not in {"push", "workflow_dispatch"}:
        raise ValueError(f"unsupported release event: {event}")
    branch = fetch("", None)["default_branch"]
    main_sha = fetch(f"branches/{urllib.parse.quote(branch, safe='')}", None)[
        "commit"
    ]["sha"]
    if sha != main_sha:
        raise ValueError(
            f"release commit {sha} is not current {branch} HEAD ({main_sha})"
        )

    # Check the named tag, too: manual dispatch can otherwise publish a
    # different, already-existing tag while running on current main.
    tag_path = f"git/ref/tags/{urllib.parse.quote(tag, safe='')}"
    try:
        fetch(tag_path, None)
    except urllib.error.HTTPError as exc:
        if exc.code != 404 or event != "workflow_dispatch":
            raise
    else:
        tag_sha = fetch(f"commits/{urllib.parse.quote(tag, safe='')}", None)[
            "sha"
        ]
        if tag_sha != sha:
            raise ValueError(
                f"release tag {tag} points to {tag_sha}, not {sha}"
            )

    runs = fetch(
        "actions/workflows/ci.yml/runs",
        {"head_sha": sha, "branch": branch, "event": "push", "per_page": "100"},
    )["workflow_runs"]
    if not any(
        run["head_sha"] == sha
        and run["head_branch"] == branch
        and run["event"] == "push"
        and run["status"] == "completed"
        and run["conclusion"] == "success"
        for run in runs
    ):
        raise ValueError(f"no completed successful main-push CI run for {sha}")


def main() -> None:
    api_url = os.environ["GITHUB_API_URL"].rstrip("/")
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]

    def fetch(path: str, query: dict[str, str] | None) -> dict[str, Any]:
        url = f"{api_url}/repos/{repository}"
        if path:
            url += f"/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    try:
        check_provenance(
            fetch,
            sha=os.environ["GITHUB_SHA"],
            tag=os.environ["RELEASE_TAG"],
            event=os.environ["GITHUB_EVENT_NAME"],
        )
    except (ValueError, KeyError, urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"Release blocked: {exc}") from exc
    print(
        "Release provenance verified: current main and successful main-push CI"
    )


if __name__ == "__main__":
    main()
