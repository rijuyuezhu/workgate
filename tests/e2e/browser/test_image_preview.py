"""Browser-level image preview card and expiry fallback in a real Chromium."""

import base64
import json

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import PROJECT_ROOT, BrowserHarness

_HTML = PROJECT_ROOT / "src/workgate/control/mcp/image_preview.html"
_TOKEN = "A" * 43
_URL = f"https://files.example.test/download/{_TOKEN}"
_EXPIRED_URL = f"https://files.example.test/download/{'B' * 43}"
_PREFIXED_URL = f"https://files.example.test/workgate/download/{_TOKEN}"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/B9sAAAAASUVORK5CYII="
)


@pytest.mark.browser
@pytest.mark.timeout(60)
def test_image_preview_card_renders_and_falls_back(
    browser_harness: BrowserHarness,
):
    page = browser_harness.browser.new_page()
    try:
        page.route(
            "https://files.example.test/download/**",
            lambda route: route.fulfill(
                status=200, content_type="image/png", body=_PNG
            ),
        )
        html = _HTML.read_text(encoding="utf-8")
        marker = "  <script>\n  (() => {"
        assert marker in html
        mocked = (
            f"<script>window.openai = {{ toolOutput: {json.dumps({'url': _URL})} }};"
            "</script>\n"
        )
        page.set_content(html.replace(marker, mocked + marker, 1))
        expect(page.locator("#preview")).to_be_visible()
        assert (
            page.locator("#preview").evaluate("(img) => img.naturalWidth") == 1
        )
        assert page.locator("#open").get_attribute("href") == _URL
        expect(page.locator("#status")).to_be_hidden()

        page.route(
            "https://files.example.test/download/**",
            lambda route: route.fulfill(
                status=410,
                body='{"error":"expired"}',
                content_type="application/json",
            ),
        )
        # The standard MCP Apps tool-result notification updates the same card.
        page.evaluate(
            """(url) => window.postMessage({
                jsonrpc: "2.0", method: "ui/notifications/tool-result",
                params: { structuredContent: { url } }
            }, "*")""",
            _EXPIRED_URL,
        )
        expect(page.locator("#preview")).to_be_hidden()
        expect(page.locator("#status")).to_contain_text("expired")
        expect(page.locator("#open")).to_be_visible()
        assert page.locator("#open").get_attribute("href") == _EXPIRED_URL

        page.evaluate(
            """() => window.dispatchEvent(new CustomEvent("openai:set_globals", {
                detail: { globals: { toolOutput: {
                    url: "javascript:alert(1)"
                } } }
            }))"""
        )
        expect(page.locator("#status")).to_contain_text("Invalid image link")
        assert page.locator("#preview").is_hidden()
        assert page.locator("#open").is_hidden()

        page.route(
            "https://files.example.test/workgate/download/**",
            lambda route: route.fulfill(
                status=200, content_type="image/png", body=_PNG
            ),
        )
        page.evaluate(
            """(url) => window.postMessage({
                jsonrpc: "2.0", method: "ui/notifications/tool-result",
                params: { structuredContent: { url } }
            }, "*")""",
            _PREFIXED_URL,
        )
        expect(page.locator("#preview")).to_be_visible()
        assert page.locator("#open").get_attribute("href") == _PREFIXED_URL
    finally:
        page.close()
