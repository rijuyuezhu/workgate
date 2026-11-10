"""Opt-in React/SVAR Files preview over real authenticated Control/Executor."""

import os

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(120),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


def test_react_files_preview_reuses_authenticated_files_api(
    browser_harness: BrowserHarness,
) -> None:
    harness = browser_harness
    harness.login()
    harness.wait_executor_online()
    page = harness.page
    workspace = harness.executor_workspace
    directory = workspace / "react-subdir"
    directory.mkdir()
    (directory / "inside.txt").write_text(
        "from React directory", encoding="utf-8"
    )

    # The standard UI does not load the React prototype on ordinary navigation.
    assert page.evaluate("typeof window.WorkgateReactFiles") == "undefined"

    page.goto(
        f"{harness.base_url}/ui?files_view=react#files",
        wait_until="domcontentloaded",
    )
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#file-list.react-files-list")).to_be_visible()
    prototype = page.locator(".wg-react-files-prototype")
    expect(prototype).to_be_visible(timeout=10000)
    expect(prototype).to_contain_text("notes.txt", timeout=10000)

    # Selecting uses WorkGate's existing preview and authenticated request path.
    prototype.get_by_text("notes.txt", exact=True).click()
    expect(page.locator("#file-preview-title")).to_have_text("notes.txt")
    expect(page.locator("#file-preview-body")).to_contain_text(
        "executor browser fixture", timeout=10000
    )

    # SVAR folder navigation drives the same WorkGate file path/controller.
    prototype.get_by_role("grid").get_by_text(
        "react-subdir", exact=True
    ).dblclick()
    expect(page.locator("#file-breadcrumbs")).to_contain_text("react-subdir")
    expect(prototype).to_contain_text("inside.txt", timeout=10000)
    prototype.get_by_text("inside.txt", exact=True).click()
    expect(page.locator("#file-preview-body")).to_contain_text(
        "from React directory", timeout=10000
    )

    # Existing download tickets work for files reached via SVAR navigation.
    expect(page.locator("#file-download")).to_be_enabled()
    with page.expect_download() as receipt:
        page.locator("#file-download").click()
    assert receipt.value.suggested_filename == "inside.txt"

    # Existing transactional upload is still handled by the Files controller.
    upload = harness.root / "uploaded-proto.txt"
    upload.write_text("React uses the existing upload API", encoding="utf-8")
    page.locator("#file-upload-input").set_input_files(str(upload))
    expect(prototype).to_contain_text("uploaded-proto.txt", timeout=10000)
    assert (
        directory / upload.name
    ).read_text() == "React uses the existing upload API"

    # No extra browser authentication/session state is introduced by the island.
    assert page.evaluate(
        "typeof window.WorkgateReactFiles.mount === 'function'"
    )
    assert page.evaluate(
        """() => Boolean(document.querySelector('link[href*="react_files_bundle.css"]'))"""
    )

    # Switching back to the normal URL uses the original Files view and no
    # optional React bundle. This is a prototype, not a replacement release.
    page.goto(f"{harness.base_url}/ui#files", wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#file-list .file-entry").first).to_be_visible()
    assert page.evaluate("typeof window.WorkgateReactFiles") == "undefined"

    # Keep the original mobile list → detail → back navigation.
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(
        f"{harness.base_url}/ui?files_view=react#files",
        wait_until="domcontentloaded",
    )
    expect(page.locator("#file-panel")).to_have_attribute(
        "data-mobile-step", "list"
    )
    expect(page.locator(".wg-react-files-prototype")).to_be_visible()
    expect(page.locator(".wg-react-files-prototype")).to_contain_text(
        "copy-source.txt", timeout=10000
    )
    page.locator(".wg-react-files-prototype").get_by_text(
        "copy-source.txt", exact=True
    ).click()
    expect(page.locator("#file-panel")).to_have_attribute(
        "data-mobile-step", "detail"
    )
    expect(page.locator("#file-preview-body")).to_contain_text("copy source")
    page.locator(".mobile-subpage-bar:visible .mobile-subpage-back").click()
    expect(page.locator("#file-panel")).to_have_attribute(
        "data-mobile-step", "list"
    )

    # The component also responds when a browser is resized without reloading.
    page.set_viewport_size({"width": 1440, "height": 1000})
    expect(
        page.locator(".wg-react-files-prototype")
        .get_by_role("grid")
        .get_by_text("notes.txt", exact=True)
    ).to_be_visible(timeout=10000)

    harness.assert_clean_browser()
