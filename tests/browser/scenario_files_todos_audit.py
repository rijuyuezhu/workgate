import json
import time

from playwright.sync_api import Route, expect

from tests.browser.harness import BrowserHarness


def _file_entry(harness: BrowserHarness, path: str):
    return harness.page.locator(f'.file-entry[title="{path}"]')


def run_files_todos_audit(harness: BrowserHarness) -> None:
    page = harness.page
    harness.navigate("files")

    expect(_file_entry(harness, "notes.txt")).to_be_visible()
    _file_entry(harness, "notes.txt").click()
    expect(page.locator("#file-preview-body")).to_contain_text(
        "executor browser fixture"
    )
    page.locator("#file-edit").click()
    expect(page.locator("#file-editor-form")).to_be_visible()
    page.locator("#file-editor").fill("edited by Chromium\n")
    page.locator("#file-editor-form").get_by_role(
        "button", name="Save file"
    ).click()
    expect(page.locator("#file-state")).to_contain_text(
        f"Saved {harness.executor_id}:notes.txt"
    )
    assert harness.executor_workspace.joinpath("notes.txt").read_text() == (
        "edited by Chromium\n"
    )

    delayed = False

    def delay_stale_preview(route: Route) -> None:
        nonlocal delayed
        if "stale-executor.txt" in route.request.url and not delayed:
            delayed = True
            time.sleep(0.4)
        route.continue_()

    page.route("**/api/ui/files/preview**", delay_stale_preview)
    _file_entry(harness, "stale-executor.txt").click()
    _file_entry(harness, "notes.txt").click()
    expect(page.locator("#file-preview-body")).to_contain_text(
        "edited by Chromium"
    )
    page.wait_for_timeout(600)
    expect(page.locator("#file-preview-body")).not_to_contain_text(
        "stale executor preview"
    )
    page.unroute("**/api/ui/files/preview**", delay_stale_preview)

    _file_entry(harness, "copy-source.txt").click()
    page.once("dialog", lambda dialog: dialog.accept("copied.txt"))
    page.locator("#file-copy").click()
    expect(_file_entry(harness, "copied.txt")).to_be_visible()

    page.once("dialog", lambda dialog: dialog.accept("moved.txt"))
    page.locator("#file-move").click()
    expect(_file_entry(harness, "moved.txt")).to_be_visible()
    assert not harness.executor_workspace.joinpath("copied.txt").exists()

    page.once("dialog", lambda dialog: dialog.accept("renamed.txt"))
    page.locator("#file-rename").click()
    expect(_file_entry(harness, "renamed.txt")).to_be_visible()
    assert harness.executor_workspace.joinpath("renamed.txt").read_text() == (
        "copy source\n"
    )

    session = harness.api("POST", "/tools/session_start", body={"workdir": "."})
    assert session["status"] == 200
    session_id = session["payload"]["session_id"]
    snapshot_forbidden = 0

    def forbid_combined_snapshot(route: Route) -> None:
        nonlocal snapshot_forbidden
        snapshot_forbidden += 1
        route.fulfill(
            status=403,
            content_type="application/json",
            body=json.dumps(
                {
                    "ok": False,
                    "message": "Missing required OAuth scope: audit:read",
                }
            ),
        )

    page.route("**/api/ui/sessions/snapshot**", forbid_combined_snapshot)
    harness.navigate("sessions")
    page.locator("#session-refresh").click()
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "true")
    expect(page.locator("#todo-state")).to_contain_text("loaded 0 todos")
    assert snapshot_forbidden >= 1
    page.unroute("**/api/ui/sessions/snapshot**", forbid_combined_snapshot)
    session_entry_box = page.locator(
        "#session-list .session-entry"
    ).first.bounding_box()
    session_detail_box = page.locator(".session-detail-header").bounding_box()
    assert session_entry_box is not None and session_detail_box is not None
    assert abs(session_entry_box["y"] - session_detail_box["y"]) < 1

    availability_override = "missing_on_executor"

    def project_session_availability(route: Route) -> None:
        if "/api/ui/sessions/snapshot" in route.request.url:
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        for row in payload.get("data", {}).get("sessions", []):
            if row.get("session_id") == session_id:
                row["availability"] = availability_override
                row["active"] = True
        route.fulfill(response=response, json=payload)

    page.route("**/api/ui/sessions**", project_session_availability)
    page.locator("#session-refresh").click()
    expect(page.locator("#session-detail-status")).to_have_text(
        "Unavailable · missing on executor"
    )
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"] .session-entry-meta'
        )
    ).to_contain_text("missing on executor")
    expect(page.locator("#todo-refresh")).to_be_enabled()
    expect(page.locator("#todo-add")).to_be_enabled()
    expect(page.locator("#session-audit-refresh")).to_be_enabled()
    expect(page.locator("#session-terminate")).to_be_enabled()

    availability_override = "executor_offline"
    page.locator("#session-refresh").click()
    expect(page.locator("#session-detail-status")).to_have_text(
        "Unavailable · executor offline"
    )
    expect(page.locator("#todo-refresh")).to_be_enabled()
    expect(page.locator("#todo-add")).to_be_enabled()
    expect(page.locator("#session-audit-refresh")).to_be_enabled()
    expect(page.locator("#session-terminate")).to_be_disabled()

    page.unroute("**/api/ui/sessions**", project_session_availability)
    page.locator("#session-refresh").click()
    expect(page.locator("#session-detail-status")).to_have_text(
        "Active · responded within the last 5 hours"
    )
    expect(page.locator("#todo-add")).to_be_enabled()
    expect(page.locator("#session-audit-refresh")).to_be_enabled()
    expect(page.locator("#session-terminate")).to_be_enabled()

    page.locator("#todo-add").click()
    row = page.locator("#todo-list .todo-row").last
    row.locator("input").fill("verify browser todos")
    row.locator("select").nth(0).select_option("in_progress")
    row.locator("select").nth(1).select_option("high")

    page.locator("#todo-add").click()
    rows = page.locator("#todo-list .todo-row")
    expect(rows).to_have_count(2)
    second_row = rows.last
    second_row.locator("input").fill("remove browser todo")
    for selector, index in [
        ("input", 0),
        ("select", 0),
        ("select", 1),
        ("button", 0),
    ]:
        first_box = rows.nth(0).locator(selector).nth(index).bounding_box()
        second_box = rows.nth(1).locator(selector).nth(index).bounding_box()
        assert first_box is not None and second_box is not None
        assert abs(first_box["x"] - second_box["x"]) < 1
    second_row.get_by_role("button", name="Remove").click()
    expect(rows).to_have_count(1)

    page.locator("#todo-save").click()
    expect(page.locator("#todo-state")).to_contain_text(f"Saved {session_id}")
    todos = harness.api("GET", f"/api/ui/todos?session_id={session_id}")
    assert todos["status"] == 200
    assert todos["payload"]["data"]["todos"][0]["content"] == (
        "verify browser todos"
    )

    audited_write = harness.api(
        "POST",
        "/tools/write_file",
        body={
            "session_id": session_id,
            "path": "audit-scope.txt",
            "content": "scope protected\n" + "payload-e2e-" * 2_000,
            "overwrite": True,
        },
    )
    assert audited_write["status"] == 200
    audited_link = harness.api(
        "POST",
        "/tools/file_link/create",
        body={
            "session_id": session_id,
            "path": "audit-scope.txt",
            "ttl_s": 60,
            "filename": None,
        },
    )
    assert audited_link["status"] == 200

    page.locator("#session-audit-operation").select_option("files")
    page.locator("#session-audit-search").fill("write_file")
    page.locator("#session-audit-refresh").click()
    expect(
        page.locator("#session-audit-list .audit-entry").first
    ).to_be_visible()
    page.locator("#session-audit-list .audit-entry").first.click()
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel")
    ).to_have_count(2)
    session_list_box = page.locator("#session-list").bounding_box()
    session_workspace_box = page.locator(".session-workspace").bounding_box()
    assert session_list_box is not None and session_workspace_box is not None
    session_list_bottom = session_list_box["y"] + session_list_box["height"]
    session_workspace_bottom = (
        session_workspace_box["y"] + session_workspace_box["height"]
    )
    assert abs(session_list_bottom - session_workspace_bottom) < 1
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel").nth(0)
    ).to_contain_text("Call request")
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel").nth(1)
    ).to_contain_text("Call result")
    expect(page.locator("#session-audit-detail-body")).to_contain_text(
        "audit-scope.txt"
    )
    expect(page.locator("#session-audit-detail-body")).to_contain_text(
        "payload-e2e-"
    )
    request_body = page.locator(
        "#session-audit-detail-body .audit-call-panel-body"
    ).nth(0)
    expect(request_body).to_have_attribute("tabindex", "0")
    scroll_extent = request_body.evaluate(
        "element => ({height: element.clientHeight, scrollHeight: element.scrollHeight})"
    )
    assert scroll_extent["scrollHeight"] > scroll_extent["height"]
    request_body.focus()
    expect(request_body).to_be_focused()
    request_body.press("PageDown")
    scroll_deadline = time.monotonic() + 2
    while time.monotonic() < scroll_deadline:
        if request_body.evaluate("element => element.scrollTop") > 0:
            break
        page.wait_for_timeout(50)
    else:
        raise AssertionError(
            "focused audit request body did not scroll on PageDown"
        )
    detail_style = page.locator(
        "#session-audit-detail-body .audit-detail-json"
    ).first.evaluate(
        """element => {
          const style = getComputedStyle(element);
          return {
            minHeight: style.minHeight,
            borderTopWidth: style.borderTopWidth,
            paddingTop: style.paddingTop,
          };
        }"""
    )
    assert detail_style == {
        "minHeight": "0px",
        "borderTopWidth": "0px",
        "paddingTop": "0px",
    }

    page.locator("#session-audit-operation").select_option("")
    page.locator("#session-audit-search").fill("create_file_link")
    page.locator("#session-audit-refresh").click()
    expect(
        page.locator("#session-audit-list .audit-entry").first
    ).to_be_visible()
    page.locator("#session-audit-list .audit-entry").first.click()
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel").nth(0)
    ).to_contain_text('"filename": null')
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel").nth(1)
    ).to_contain_text("related_events")
    expect(
        page.locator("#session-audit-detail-body .audit-call-panel").nth(1)
    ).to_contain_text("download_link_created")

    harness.navigate("audit")
    page.locator("#audit-operation").select_option("files")
    page.locator("#audit-search").fill("write_file")
    page.locator("#audit-refresh").click()
    expect(page.locator("#audit-list .audit-entry").first).to_be_visible()
    page.locator("#audit-list .audit-entry").first.click()
    expect(page.locator("#audit-detail-body .audit-call-panel")).to_have_count(
        2
    )
    expect(
        page.locator("#audit-detail-body .audit-call-panel").nth(0)
    ).to_contain_text("Call request")
    expect(
        page.locator("#audit-detail-body .audit-call-panel").nth(1)
    ).to_contain_text("Call result")
    expect(page.locator("#audit-detail-body")).to_contain_text(
        "audit-scope.txt"
    )

    empty_write = harness.api(
        "POST",
        "/tools/write_file",
        body={
            "session_id": session_id,
            "path": "empty-audit.txt",
            "content": "",
            "overwrite": True,
        },
    )
    assert empty_write["status"] == 200
    page.locator("#audit-search").fill("write_file")
    page.locator("#audit-refresh").click()
    expect(page.locator("#audit-list .audit-entry").first).to_be_visible()
    page.locator("#audit-list .audit-entry").first.click()
    request_text = page.locator("#audit-detail-body .audit-call-panel").nth(0)
    expect(request_text).to_contain_text("empty-audit.txt")
    expect(request_text).to_contain_text('"content": ""')

    page.locator("#audit-operation").select_option("")
    page.locator("#audit-search").fill("oauth_client_approved")
    page.locator("#audit-refresh").click()
    expect(page.locator("#audit-list .audit-entry").first).to_be_visible()
    page.locator("#audit-list .audit-entry").first.click()
    expect(
        page.locator("#audit-detail-body .audit-call-panel").nth(1)
    ).to_contain_text("client_id")
    expect(
        page.locator("#audit-detail-body .audit-call-panel").nth(1)
    ).not_to_contain_text("No output recorded")

    full_token = harness.api_token
    assert isinstance(full_token, str) and full_token
    read_only_token = harness.issue_token("audit:read shell:read remote:use")
    harness.set_token(read_only_token)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    harness.navigate("sessions")
    page.locator("#session-audit-operation").select_option("files")
    page.locator("#session-audit-search").fill("write_file")
    page.locator("#session-audit-refresh").click()
    expect(
        page.locator("#session-audit-list .audit-entry").first
    ).to_be_visible()
    page.locator("#session-audit-list .audit-entry").first.click()
    expect(page.locator("#session-audit-detail-meta")).to_have_text(
        "Details unavailable"
    )
    expect(page.locator("#session-audit-detail-body")).to_contain_text(
        "shell:write"
    )
    harness.console_errors = [
        line for line in harness.console_errors if "403 (Forbidden)" not in line
    ]

    harness.set_token(full_token)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#connection-state")).to_have_text("Connected")
    harness.navigate("sessions")
    expect(
        page.locator(
            f'#session-list .session-entry[data-session-id="{session_id}"]'
        )
    ).to_have_attribute("aria-current", "true")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#session-terminate").click()
    expect(page.locator("#session-detail-status")).to_contain_text(
        "Ended · executor confirmed session absence"
    )
    blocked = harness.api(
        "POST",
        "/tools/read",
        body={"session_id": session_id, "path": "notes.txt"},
    )
    assert blocked["status"] == 400
    assert blocked["payload"]["error"] == "validation_error"
    assert "is ended" in blocked["payload"]["message"]
    harness.console_errors = [
        line
        for line in harness.console_errors
        if "400 (Bad Request)" not in line
    ]
