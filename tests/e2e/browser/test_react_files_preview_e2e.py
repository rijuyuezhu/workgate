"""React Files preview renders real Executor data and escaped preview variants."""

import base64
import os
import re

import pytest
from playwright.sync_api import expect

from tests.e2e.browser.harness import BrowserHarness

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(150),
    pytest.mark.skipif(
        os.environ.get("WORKGATE_BROWSER_E2E") != "1", reason="Chromium E2E"
    ),
]


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/B9sAAAAASUVORK5CYII="
)


def test_react_files_preview_real_and_synthetic(
    browser_harness: BrowserHarness,
) -> None:
    h = browser_harness
    h.login()
    h.wait_executor_online()
    (h.executor_workspace / "preview-test.png").write_bytes(_PNG)
    (h.executor_workspace / "preview-test.bin").write_bytes(
        bytes.fromhex("00ff1020")
    )
    h.navigate("files")
    page = h.page
    assert page.evaluate(
        "typeof window.WorkgateReactFilesPreview?.mount === 'function'"
    )
    grid = page.locator("#file-list .wg-files-list")
    grid.get_by_text("notes.txt", exact=True).click()
    expect(page.locator("#file-preview-title")).to_have_text("notes.txt")
    expect(
        page.locator("#file-preview-body .file-preview-text")
    ).to_contain_text("executor browser fixture")
    grid.get_by_text("preview-test.png", exact=True).click()
    image = page.locator("#file-preview-body img.file-preview-image")
    expect(image).to_be_visible()
    expect(image).to_have_attribute(
        "src", re.compile(r"^data:image/.*;base64,")
    )
    assert image.evaluate("img => img.naturalWidth") == 1
    grid.get_by_text("preview-test.bin", exact=True).click()
    expect(
        page.locator("#file-preview-body .file-preview-text")
    ).to_contain_text("00ff1020")
    expect(page.locator("#file-preview-meta")).to_contain_text("hex")
    # Ordinary file editing must continue hiding the React host while retaining
    # the JS form and its conflict/save workflow.
    grid.get_by_text("notes.txt", exact=True).click()
    page.locator("#file-edit").click()
    expect(page.locator("#file-preview-body")).to_be_hidden()
    expect(page.locator("#file-editor-form")).to_be_visible()
    page.locator("#file-editor-cancel").click()
    expect(page.locator("#file-preview-body")).to_be_visible()

    # No backend, auth, or media fixtures are needed for these view contracts.
    page.evaluate("""() => {
      const host = document.createElement('div');
      host.id = 'react-file-preview-contract';
      document.getElementById('app-main').append(host);
      window.__previewCalls = [];
      window.__filePreview = window.WorkgateReactFilesPreview.mount(
        host, (dir, path) => window.__previewCalls.push([dir, path]));
      window.__filePreview.render({kind:'message', message:'<img src=x onerror=alert(1)>'});
    }""")
    contract = page.locator("#react-file-preview-contract")
    expect(contract).to_contain_text("<img src=x onerror=alert(1)>")
    assert contract.locator("img").count() == 0

    page.evaluate("""() => window.__filePreview.render({
      kind: 'directory', directory:'/safe', entries:[
        {name:'<script>unsafe</script>',path:'/safe/x',type:'dir'},
        {name:'data.txt',path:'/safe/data.txt',type:'file'},
        {name:'link',path:'/safe/link',type:'link'}
      ]
    })""")
    expect(contract.locator(".file-preview-entry")).to_have_count(3)
    expect(contract).to_contain_text("<script>unsafe</script>")
    assert contract.locator("script").count() == 0
    contract.locator(".file-preview-entry").first.click()
    assert page.evaluate("window.__previewCalls") == [["/safe", "/safe/x"]]
    page.evaluate(
        """() => window.__filePreview.render({kind:'directory',entries:[]})"""
    )
    expect(contract).to_contain_text("Directory is empty.")
    page.evaluate("""() => window.__filePreview.render({
      kind:'text', language:'python', source:'print(42)',
      tokens: window.WorkgateSyntax.tokenize('print(42)', 'python')
    })""")
    expect(contract.locator(".syntax-code.language-python")).to_have_text(
        "print(42)"
    )
    expect(contract.locator(".syntax-token.syntax-number")).to_have_text("42")
    page.evaluate("""() => window.__filePreview.render({
      kind:'text',language:'plain',source:'A <b>literal</b>'
    })""")
    expect(contract.locator("pre")).to_have_text("A <b>literal</b>")
    assert contract.locator("b").count() == 0
    h.assert_clean_browser()
