import httpx
from playwright.sync_api import expect

from tests.browser.harness import BrowserHarness


def run_executor_admin(harness: BrowserHarness) -> None:
    page = harness.page

    started = httpx.post(
        f"{harness.base_url}/executor/v1/pair/start",
        json={
            "requested_name": "browser-executor",
            "metadata": {
                "hostname": "browser-laptop",
                "platform": "linux",
                "build": "browser-e2e",
            },
        },
        timeout=5,
    )
    assert started.status_code == 200
    pair = started.json()
    assert pair["verification_uri"] == f"{harness.base_url}/pair"

    page.goto(pair["verification_uri"], wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    expect(page.locator("#page-title")).to_have_text("Executors")
    expect(page.locator("#executors-panel")).to_be_visible()

    page.locator("#executor-pair-open").click()
    expect(page.locator("#executor-pair-dialog")).to_be_visible()
    page.locator("#executor-pair-code").fill(pair["user_code"].lower())
    page.locator("#executor-pair-form").get_by_role(
        "button", name="Inspect request"
    ).click()

    expect(page.locator("#executor-pair-review")).to_be_visible()
    expect(page.locator("#executor-pair-requested-name")).to_have_text(
        "browser-executor"
    )
    expect(page.locator("#executor-pair-hostname")).to_have_text(
        "browser-laptop"
    )
    expect(page.locator("#executor-pair-platform")).to_have_text("linux")
    expect(page.locator("#executor-pair-build")).to_have_text("browser-e2e")
    expect(page.locator("#executor-pair-existing")).to_have_text("None")
    page.locator("#executor-pair-name").fill("browser-approved")
    page.locator("#executor-pair-approve").click()

    expect(page.locator("#executor-pair-dialog")).to_be_hidden()
    expect(page.locator("#executor-list")).to_contain_text("browser-approved")
    expect(page.locator("#executor-detail-status")).to_have_text("offline")
    executor_id = page.locator("#executor-detail-id").inner_text()
    assert executor_id.startswith("exec_")

    page.locator("#executor-rename-open").click()
    expect(page.locator("#executor-rename-dialog")).to_be_visible()
    page.locator("#executor-rename-name").fill("browser-renamed")
    page.locator("#executor-rename-form").get_by_role(
        "button", name="Rename"
    ).click()
    expect(page.locator("#executor-rename-dialog")).to_be_hidden()
    expect(page.locator("#executor-list")).to_contain_text("browser-renamed")

    page.locator("#executor-revoke-open").click()
    expect(page.locator("#executor-revoke-dialog")).to_be_visible()
    expect(page.locator("#executor-revoke-name")).to_have_text(
        "browser-renamed"
    )
    page.locator("#executor-revoke-form").get_by_role(
        "button", name="Revoke executor"
    ).click()
    expect(page.locator("#executor-revoke-dialog")).to_be_hidden()
    expect(page.locator("#executor-detail-status")).to_have_text("revoked")
    expect(page.locator("#executor-revoked")).to_have_text("1")
