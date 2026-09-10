from playwright.sync_api import expect

from tests.browser.harness import BrowserHarness


def run_auth_dashboard(harness: BrowserHarness) -> None:
    harness.login()
    page = harness.page

    expect(page.locator("#version")).not_to_have_text("—")
    expect(page.locator("#dashboard-version")).not_to_have_text("—")
    expect(page.locator("#dashboard-platform")).not_to_have_text("—")
    expect(page.locator("#dashboard-python")).not_to_have_text("—")
    expect(page.locator("#dashboard-executor")).to_have_value(
        harness.executor_id
    )
    expect(page.locator("#dashboard-state")).to_contain_text(
        harness.executor_id
    )

    bootstrap = harness.api("GET", "/api/ui/bootstrap")
    assert bootstrap["status"] == 200
    data = bootstrap["payload"]["data"]
    assert data["ui"]["auth_mode"] == "oauth"
    assert data["executor_targets"][0]["executor_id"] == harness.executor_id
    assert data["executor_targets"][0]["name"] == "browser-loopback"
    assert data["executor_targets"][0]["status"] == "online"
    assert data["version"]

    dashboard = harness.api(
        "GET", f"/api/ui/dashboard?executor_id={harness.executor_id}"
    )
    assert dashboard["status"] == 200
    snapshot = dashboard["payload"]["data"]
    assert snapshot["executor_id"] == harness.executor_id
    assert snapshot["version"]["version"]
    assert snapshot["version"]["python"]
