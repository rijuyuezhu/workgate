import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e.browser.harness import BrowserHarness


@pytest.fixture
def browser_harness(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Iterator[BrowserHarness]:
    if os.environ.get("WORKGATE_BROWSER_E2E") != "1":
        pytest.skip(
            "real Chromium E2E is enabled by the dedicated browser launcher"
        )

    artifacts_root = Path(
        os.environ.get("WORKGATE_BROWSER_ARTIFACTS", "browser-artifacts")
    ).resolve()
    test_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", request.node.nodeid).strip("-")
    artifacts = artifacts_root / test_name
    with sync_playwright() as playwright:
        harness = BrowserHarness.start(tmp_path, artifacts, playwright)
        failed = True
        try:
            yield harness
            failed = False
        finally:
            harness.stop(failed=failed)
