"""Optional provenance for the authenticated Workgate diagnostics.

Only report build fields backed by deployment metadata or the actual source
checkout. A commit timestamp is not a build timestamp.
"""

import os
import re
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            },
        )
    except OSError, UnicodeError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


@lru_cache(maxsize=1)
def _checkout_info() -> dict[str, str]:
    root = _source_root()
    # Do not accidentally report metadata from a containing, unrelated repo.
    if not (root / ".git").exists():
        return {}
    commit = _git_value(root, "rev-parse", "--verify", "HEAD")
    if not commit or not re.fullmatch(
        r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", commit
    ):
        return {}
    result = {"commit": commit}
    branch = _git_value(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch:
        result["branch"] = branch
    return result


def normalize_build_info(values: dict[str, object]) -> dict[str, str]:
    """Accept only bounded provenance fields, not arbitrary metadata."""
    info: dict[str, str] = {}
    commit = values.get("commit")
    if isinstance(commit, str) and re.fullmatch(
        r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", commit
    ):
        info["commit"] = commit
    branch = values.get("branch")
    if (
        isinstance(branch, str)
        and branch
        and len(branch.encode("utf-8", errors="surrogatepass")) <= 256
        and not branch.startswith("/")
        and not re.search(r"[\s\x00-\x1f\x7f\ud800-\udfff:\\]", branch)
    ):
        info["branch"] = branch
    date = values.get("build_date")
    if isinstance(date, str) and len(date) <= 64:
        try:
            parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                info["build_date"] = parsed.isoformat()
    return info


def build_info() -> dict[str, str]:
    """Return available source or explicitly supplied deployment provenance."""
    checkout = _checkout_info()
    commit = os.environ.get("WORKGATE_BUILD_COMMIT")
    branch = os.environ.get("WORKGATE_BUILD_BRANCH")
    if commit is None or commit == checkout.get("commit"):
        commit = commit or checkout.get("commit")
        branch = branch or checkout.get("branch")
    return normalize_build_info(
        {
            "commit": commit,
            "branch": branch,
            "build_date": os.environ.get("WORKGATE_BUILD_DATE"),
        }
    )
